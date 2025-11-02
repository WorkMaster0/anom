#!/usr/bin/env python3
"""
monolith_demo.py

Самодостатній демонстраційний моноліт:
- Симулятор ринкових даних (async background producer, імітує WebSocket потік)
- REST заглушка (fetch historical klines з локальної симуляції)
- In-memory cache + TTL
- Простий async rate-limiter (tokens per minute)
- Сигнальний движок: EMA/RSI/MACD/ATR + candle patterns (pure functions)
- Побудова графіків (mplfinance) в окремому потоці
- FastAPI сервер з HTML дашбордом, API ендпоінтами та /metrics (Prometheus)
- Візуальний дашборд: список сигналів, графіки, керування агресією

ЦЕ БЕЗПЕЧНИЙ ДЕМО — НІЧОГО НЕ ПІДКЛЮЧАЄТЬСЯ ДО БІРЖІ І НЕ ВІДПРАВЛЯЄ ОРДЕРИ.
"""

import os
import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
import ta

from fastapi import FastAPI, BackgroundTasks, Request, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from prometheus_client import make_asgi_app, Counter, Gauge

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("monolith-demo")

# ---------------- config ----------------
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "8000"))
SYMBOLS = os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT").split(",")
DATA_INTERVAL_MIN = int(os.getenv("DATA_INTERVAL_MIN", "15"))   # candle interval in minutes (for simulation)
KLINES_HISTORY = int(os.getenv("KLINES_HISTORY", "500"))        # how many candles to keep per symbol
PLOT_DPI = int(os.getenv("PLOT_DPI", "120"))
AGGRESSION = float(os.getenv("AGGRESSION", "0.3"))              # 0..1 demo parameter
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "1200"))  # demo limiter (requests per minute)

# ---------------- prometheus metrics ----------------
SCAN_COUNTER = Counter("demo_scans_total", "Total demo scans")
SIGNAL_COUNTER = Counter("demo_signals_total", "Total demo signals")
LAST_SCAN_GAUGE = Gauge("demo_last_scan_unix", "Last demo scan timestamp")

# ---------------- threadpool for plotting ----------------
_plot_executor = ThreadPoolExecutor(max_workers=2)

# ---------------- in-memory stores ----------------
_klines_store: Dict[str, pd.DataFrame] = {}
_store_lock = asyncio.Lock()
_signals_store: Dict[str, Dict] = {}  # symbol -> latest signal dict
_signals_history: List[Dict] = []
_cache_ttl: Dict[str, float] = {}  # key -> expiry timestamp for cache entries

# rate limiter (token-bucket style, simple)
_rate_lock = asyncio.Lock()
_rate_tokens = RATE_LIMIT_PER_MIN
_rate_last_refill = time.time()

def _refill_tokens():
    global _rate_tokens, _rate_last_refill
    now = time.time()
    elapsed = now - _rate_last_refill
    if elapsed <= 0:
        return
    # refill proportionally per minute
    add = (elapsed / 60.0) * RATE_LIMIT_PER_MIN
    if add >= 1:
        _rate_tokens = min(RATE_LIMIT_PER_MIN, _rate_tokens + add)
        _rate_last_refill = now

async def consume_rate_token():
    async with _rate_lock:
        _refill_tokens()
        if _rate_tokens >= 1:
            _rate_tokens -= 1
            return True
        return False

# ---------------- simulated market data producer ----------------
async def _simulate_symbol(symbol: str, interval_min: int = DATA_INTERVAL_MIN, history: int = KLINES_HISTORY):
    """
    Simulate OHLCV series for a symbol and store in _klines_store.
    This runs indefinitely, appending new candles at interval.
    """
    # initial seed dataframe: generate a random walk price series
    base_price = float(100.0 + random.random() * 5000.0)  # random base
    # create a datetime index for the past `history` intervals
    now = datetime.now(timezone.utc)
    times = [now - timedelta(minutes=interval_min * (history - i)) for i in range(history)]
    prices = [base_price]
    for i in range(1, history):
        drift = random.gauss(0, 0.002)
        prices.append(max(0.0001, prices[-1] * (1 + drift)))
    df = pd.DataFrame(index=pd.to_datetime(times))
    df.index.name = "open_time"
    df["open"] = prices
    df["high"] = df["open"] * (1 + np.abs(np.random.normal(0, 0.002, size=history)))
    df["low"] = df["open"] * (1 - np.abs(np.random.normal(0, 0.002, size=history)))
    df["close"] = df["open"] * (1 + np.random.normal(0, 0.001, size=history))
    df["volume"] = np.random.randint(1, 1000, size=history)
    async with _store_lock:
        _klines_store[symbol] = df.copy()
    logger.info("Initialized simulated series for %s (rows=%d)", symbol, len(df))

    # live update loop
    while True:
        await asyncio.sleep(interval_min * 60)  # wait for the interval
        try:
            async with _store_lock:
                df = _klines_store.get(symbol)
                last_close = float(df["close"].iloc[-1])
                # simulate new candle
                drift = random.gauss(0, 0.003) * (1 + AGGRESSION)  # aggression nudges volatility
                new_open = last_close
                new_close = max(0.0001, new_open * (1 + drift))
                high = max(new_open, new_close) * (1 + abs(random.gauss(0, 0.001)))
                low = min(new_open, new_close) * (1 - abs(random.gauss(0, 0.001)))
                vol = max(1, int(np.random.poisson(200 * (1 + AGGRESSION))))
                new_time = df.index[-1] + pd.Timedelta(minutes=interval_min)
                new_row = pd.DataFrame([{
                    "open": new_open, "high": high, "low": low, "close": new_close, "volume": vol
                }], index=[pd.to_datetime(new_time)])
                new_row.index.name = "open_time"
                df = pd.concat([df, new_row])
                df = df.tail(history)
                _klines_store[symbol] = df
            # maybe emit a "tick" log occasionally
            if random.random() < 0.05:
                logger.debug("Simulated new candle for %s @ %s", symbol, new_time)
        except Exception:
            logger.exception("Simulation loop error for %s", symbol)

# ---------------- helper: fetch klines (simulated REST fallback) ----------------
async def fetch_klines(symbol: str, limit: int = KLINES_HISTORY) -> Optional[pd.DataFrame]:
    """
    Simulated REST fetch. Uses in-memory store. Honors a simple cache TTL to emulate real-world caching.
    """
    key = f"klines:{symbol}:{limit}"
    now = time.time()
    # check cache TTL
    expiry = _cache_ttl.get(key)
    if expiry and expiry > now:
        # return cached copy if exists
        async with _store_lock:
            df = _klines_store.get(symbol)
            return df.tail(limit).copy() if df is not None else None
    # consume rate token (simulate global rate limiting)
    ok = await consume_rate_token()
    if not ok:
        # emulate rate limit response by returning None
        logger.warning("Rate limit hit (simulated). Returning None for %s", symbol)
        return None
    # simulate network delay
    await asyncio.sleep(0.05)
    async with _store_lock:
        df = _klines_store.get(symbol)
        if df is None:
            return None
        # store cache ttl
        _cache_ttl[key] = now + 5.0  # small TTL
        return df.tail(limit).copy()

# ---------------- feature engineering and signal detection ----------------
def apply_all_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().dropna()
    if df.empty:
        return df
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_shadow"] = df[["close", "open"]].min(axis=1) - df["low"]
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > 1.5 * df["vol_ma20"]
    try:
        df["ema9"] = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
        df["ema21"] = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()
        df["rsi14"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
        macd = ta.trend.MACD(df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["atr14"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
        bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
        df["bb_high"] = bb.bollinger_hband()
        df["bb_low"] = bb.bollinger_lband()
        # support/resistance using rolling min/max
        df["support"] = df["low"].rolling(20).min()
        df["resistance"] = df["high"].rolling(20).max()
    except Exception:
        logger.exception("TA calculation failed")
    return df

def detect_signal_v2(df: pd.DataFrame) -> Tuple[str, List[str], bool, pd.Series, float]:
    """
    Pure function: returns (action, votes, pretop, last_row, confidence)
    action in {"LONG","SHORT","WATCH"}
    """
    df = apply_all_features(df)
    if df.empty or len(df) < 20:
        last = df.iloc[-1] if len(df) else pd.Series()
        return "WATCH", [], False, last, 0.0
    last = df.iloc[-1]
    prev = df.iloc[-2]
    votes = []
    confidence = 0.45

    # candle patterns
    if last["lower_shadow"] > 2 * max(abs(last["body"]), 1e-9) and last["body"] > 0:
        votes.append("hammer_bull"); confidence += 0.08
    if last["upper_shadow"] > 2 * max(abs(last["body"]), 1e-9) and last["body"] < 0:
        votes.append("shooting_star"); confidence += 0.08

    # engulfing
    if last["body"] > 0 and prev["body"] < 0 and last["close"] > prev["open"] and last["open"] < prev["close"]:
        votes.append("bullish_engulfing"); confidence += 0.06
    if last["body"] < 0 and prev["body"] > 0 and last["close"] < prev["open"] and last["open"] > prev["close"]:
        votes.append("bearish_engulfing"); confidence += 0.06

    # doji
    if abs(last["body"]) < 0.12 * max(last["range"], 1e-9):
        votes.append("doji"); confidence += 0.03

    # 3 candles
    if len(df) >= 3:
        if all(df["close"].iloc[-i] > df["open"].iloc[-i] for i in range(1, 4)):
            votes.append("3_green"); confidence += 0.03
        if all(df["close"].iloc[-i] < df["open"].iloc[-i] for i in range(1, 4)):
            votes.append("3_red"); confidence += 0.03

    # volume
    if last.get("vol_spike", False):
        votes.append("volume_spike"); confidence += 0.04
    if last["volume"] > 2 * df["vol_ma20"].iloc[-1]:
        votes.append("climax_volume"); confidence += 0.04

    # structure flips
    if prev["close"] > prev.get("resistance", 0) and last["close"] < last.get("resistance", 0):
        votes.append("fake_breakout_short"); confidence += 0.04
    if prev["close"] < prev.get("support", 0) and last["close"] > last.get("support", 0):
        votes.append("fake_breakout_long"); confidence += 0.04
    if prev["close"] < prev.get("resistance", 0) and last["close"] > last.get("resistance", 0):
        votes.append("resistance_flip_support"); confidence += 0.04
    if prev["close"] > prev.get("support", 0) and last["close"] < last.get("support", 0):
        votes.append("support_flip_resistance"); confidence += 0.04

    # retest / liquidity
    if last.get("support") and abs(last["close"] - last["support"]) / max(last["support"], 1e-9) < 0.003 and last["body"] > 0:
        votes.append("support_retest"); confidence += 0.03
    if last.get("resistance") and abs(last["close"] - last["resistance"]) / max(last["resistance"], 1e-9) < 0.003 and last["body"] < 0:
        votes.append("resistance_retest"); confidence += 0.03
    if last["low"] < last.get("support", 0) and last["close"] > last.get("support", 0):
        votes.append("liquidity_grab_long"); confidence += 0.03
    if last["high"] > last.get("resistance", 0) and last["close"] < last.get("resistance", 0):
        votes.append("liquidity_grab_short"); confidence += 0.03

    # trend via EMA
    trend_val = df["ema21"].iloc[-1] if "ema21" in df.columns else df["close"].rolling(20).mean().iloc[-1]
    if last["close"] > trend_val:
        votes.append("above_trend"); confidence += 0.02
    else:
        votes.append("below_trend"); confidence += 0.02

    # RSI/MACD momentum
    if "rsi14" in df.columns and last["rsi14"] < 30:
        votes.append("rsi_oversold"); confidence += 0.03
    if "rsi14" in df.columns and last["rsi14"] > 70:
        votes.append("rsi_overbought"); confidence += 0.03
    if "macd" in df.columns and "macd_signal" in df.columns:
        if last["macd"] > last["macd_signal"] and df["macd"].iloc[-2] <= df["macd_signal"].iloc[-2]:
            votes.append("macd_cross_up"); confidence += 0.03
        if last["macd"] < last["macd_signal"] and df["macd"].iloc[-2] >= df["macd_signal"].iloc[-2]:
            votes.append("macd_cross_down"); confidence += 0.03

    # pretop detection
    pretop = False
    if len(df) >= 10 and (last["close"] - df["close"].iloc[-10]) / max(df["close"].iloc[-10], 1e-9) > 0.10:
        pretop = True
        votes.append("pretop"); confidence += 0.06

    # Action (S/R proximity)
    near_resistance = last.get("resistance") and last["close"] >= last["resistance"] * 0.985
    near_support = last.get("support") and last["close"] <= last["support"] * 1.015
    action = "WATCH"
    if near_resistance:
        action = "SHORT"
    elif near_support:
        action = "LONG"

    # aggression nudge
    if AGGRESSION > 0:
        if random.random() < 0.02 + 0.10 * AGGRESSION:
            confidence += 0.08 * AGGRESSION
            votes.append("random_nudge")
        if action == "WATCH" and AGGRESSION > 0.6:
            if last["close"] >= last.get("resistance", 0) * 0.97:
                action = "SHORT"; votes.append("aggressive_near_res")
            if last["close"] <= last.get("support", 1e18) * 1.03:
                action = "LONG"; votes.append("aggressive_near_sup")

    confidence = max(0.0, min(1.0, confidence))
    return action, votes, pretop, last, confidence

# ---------------- plotting helper ----------------
def _plot_sync(df: pd.DataFrame, symbol: str, action: str, entry=None, sl=None, tp1=None, tp2=None, tp3=None) -> bytes:
    df_tail = df.tail(200).copy()
    N = len(df_tail)
    if N == 0:
        return b""
    def line(v): return [v] * N if v is not None else None
    addplots = []
    if tp1 is not None: addplots.append(mpf.make_addplot(line(tp1), linestyle="--"))
    if tp2 is not None: addplots.append(mpf.make_addplot(line(tp2), linestyle="--"))
    if tp3 is not None: addplots.append(mpf.make_addplot(line(tp3), linestyle="--"))
    if sl is not None: addplots.append(mpf.make_addplot(line(sl), linestyle="--"))
    if entry is not None: addplots.append(mpf.make_addplot(line(entry), linestyle="--"))

    fig, ax = mpf.plot(df_tail, type="candle", style="yahoo", addplot=addplots, returnfig=True, title=f"{symbol} {action}")
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=PLOT_DPI)
    buf.seek(0)
    plt.close(fig)
    return buf.read()

async def plot_bytes(df: pd.DataFrame, symbol: str, action: str, entry=None, sl=None, tp1=None, tp2=None, tp3=None) -> bytes:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_plot_executor, _plot_sync, df, symbol, action, entry, sl, tp1, tp2, tp3)

# ---------------- orchestration: analyze symbol and emit signal to in-memory store ----------------
async def analyze_and_maybe_signal(symbol: str):
    df = await fetch_klines(symbol, limit=KLINES_HISTORY)
    if df is None or len(df) < 40:
        return
    action, votes, pretop, last, confidence = detect_signal_v2(df)
    if action == "WATCH":
        return
    # compute example entry/sl/tps
    entry = None; sl = None; tp1 = None; tp2 = None; tp3 = None
    try:
        if action == "LONG":
            entry = float(last.get("support") or last["close"]) * 1.001
            sl = entry - max(1.5 * float(last.get("atr14", 0.0)), entry * 0.01)
            tp1 = entry + (float(last.get("resistance", entry)) - entry) * 0.33
            tp2 = entry + (float(last.get("resistance", entry)) - entry) * 0.66
            tp3 = float(last.get("resistance") or entry * 1.06)
        elif action == "SHORT":
            entry = float(last.get("resistance") or last["close"]) * 0.999
            sl = entry + max(1.5 * float(last.get("atr14", 0.0)), entry * 0.01)
            tp1 = entry - (entry - float(last.get("support", entry))) * 0.33
            tp2 = entry - (entry - float(last.get("support", entry))) * 0.66
            tp3 = float(last.get("support") or entry * 0.94)
    except Exception:
        logger.exception("Entry/SL/TP calc failed for %s", symbol)
        return

    # rr simple
    try:
        if action == "LONG":
            denom = entry - sl if (entry - sl) != 0 else 1e-9
            rr1 = (tp1 - entry) / denom
        else:
            denom = sl - entry if (sl - entry) != 0 else 1e-9
            rr1 = (entry - tp1) / denom
    except Exception:
        rr1 = 0.0

    # gating: simple thresholds with aggression
    rr_min = max(0.5, 2.0 - 1.5 * AGGRESSION)
    allow = (confidence >= 0.3 and rr1 >= rr_min)
    if not allow and AGGRESSION > 0.7 and random.random() < 0.02 * AGGRESSION:
        allow = True
        votes.append("aggressive_override")

    if not allow:
        return

    # cooldown: one signal per symbol per SIGNAL_COOLDOWN_MIN
    now = datetime.now(timezone.utc)
    last_sig = _signals_store.get(symbol)
    if last_sig:
        last_time = datetime.fromisoformat(last_sig["time"])
        if now - last_time < timedelta(minutes=SIGNAL_COOLDOWN_MIN):
            logger.debug("Cooldown active for %s - skipping", symbol)
            return

    # prepare record
    rec = {
        "symbol": symbol,
        "time": now.isoformat(),
        "action": action,
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "confidence": round(float(confidence), 3),
        "rr1": round(float(rr1), 3),
        "votes": votes
    }
    _signals_store[symbol] = rec
    _signals_history.insert(0, rec)
    # cap history
    if len(_signals_history) > 200:
        _signals_history.pop()
    SIGNAL_COUNTER.inc()
    logger.info("Signal generated (demo) %s %s conf=%.2f rr1=%.2f", symbol, action, confidence, rr1)

# ---------------- background periodic scanner ----------------
async def periodic_scanner():
    """
    Periodically scan all symbols by calling analyze_and_maybe_signal.
    """
    while True:
        try:
            SCAN_COUNTER.inc()
            LAST_SCAN_GAUGE.set_to_current_time()
            logger.info("Starting periodic demo scan for %d symbols", len(SYMBOLS))
            # parallelize up to a limited concurrency
            sem = asyncio.Semaphore(6)
            async def worker(sym):
                async with sem:
                    try:
                        await analyze_and_maybe_signal(sym)
                    except Exception:
                        logger.exception("Error analyzing %s", sym)
            tasks = [asyncio.create_task(worker(s)) for s in SYMBOLS]
            await asyncio.gather(*tasks)
        except Exception:
            logger.exception("Periodic scanner error")
        await asyncio.sleep(max(5, DATA_INTERVAL_MIN * 60 // 2))  # scan twice per interval

# ---------------- FastAPI app and dashboard ----------------
app = FastAPI(title="Monolith Demo Pretop")

# mount prometheus metrics at /metrics
app.mount("/metrics", make_asgi_app())

@app.on_event("startup")
async def startup_event():
    # start simulation producers
    logger.info("Starting simulated producers for symbols: %s", SYMBOLS)
    for s in SYMBOLS:
        asyncio.create_task(_simulate_symbol(s, interval_min=DATA_INTERVAL_MIN, history=KLINES_HISTORY))
    # start scanner
    asyncio.create_task(periodic_scanner())

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """
    Simple HTML dashboard showing recent signals and controls.
    """
    html = """
    <html>
      <head>
        <title>Monolith Demo Pretop Dashboard</title>
        <style>
          body { font-family: Arial, sans-serif; margin: 20px; }
          .sig { border: 1px solid #ddd; padding: 8px; margin-bottom: 8px; border-radius: 6px; }
          .controls { margin-bottom: 16px; }
          .grid { display:flex; flex-wrap: wrap; gap: 12px; }
          .card { border:1px solid #eee; padding:8px; width: 320px; border-radius:6px; }
        </style>
      </head>
      <body>
        <h1>Monolith Demo Pretop Dashboard</h1>
        <div class="controls">
          <form method="post" action="/set_aggression">
            Aggression (0.0 - 1.0): <input type="text" name="aggr" value="{aggr}" />
            <button type="submit">Set</button>
          </form>
        </div>
        <h2>Recent Signals</h2>
        {signals_html}
        <h2>Symbols</h2>
        <div class="grid">
          {cards_html}
        </div>
      </body>
    </html>
    """
    # build signals html
    sigs = _signals_history[:20]
    signals_html = ""
    for s in sigs:
        signals_html += f'<div class="sig"><b>{s["symbol"]}</b> {s["action"]} conf={s["confidence"]} time={s["time"]}<br/>votes: {", ".join(s["votes"])}</div>'
    # build cards with image thumbnails
    cards_html = ""
    for sym in SYMBOLS:
        cards_html += f'<div class="card"><h3>{sym}</h3><img src="/plot/{sym}" width="300" /><p><a href="/api/signal/{sym}">Latest signal</a></p></div>'
    return HTMLResponse(html.format(aggr=AGGRESSION, signals_html=signals_html, cards_html=cards_html))

@app.post("/set_aggression")
async def set_aggression(aggr: str = Form(...)):
    global AGGRESSION
    try:
        val = float(aggr)
        if val < 0: val = 0.0
        if val > 1: val = 1.0
        AGGRESSION = val
        return HTMLResponse(f"<html><body>Aggression set to {AGGRESSION}. <a href='/'>Back</a></body></html>")
    except Exception:
        return HTMLResponse("<html><body>Invalid value. <a href='/'>Back</a></body></html>")

@app.get("/plot/{symbol}")
async def plot_endpoint(symbol: str):
    symbol = symbol.upper()
    df = await fetch_klines(symbol, limit=KLINES_HISTORY)
    if df is None:
        return JSONResponse({"error": "no data"}, status_code=404)
    # detect last action for annotation
    last_sig = _signals_store.get(symbol)
    entry = sl = tp1 = tp2 = tp3 = None
    action = "WATCH"
    if last_sig:
        action = last_sig.get("action", "WATCH")
        entry = last_sig.get("entry")
        sl = last_sig.get("sl")
        tp1 = last_sig.get("tp1")
        tp2 = last_sig.get("tp2")
        tp3 = last_sig.get("tp3")
    img = await plot_bytes(df, symbol, action, entry=entry, sl=sl, tp1=tp1, tp2=tp2, tp3=tp3)
    return StreamingResponse(iter([img]), media_type="image/png")

@app.get("/api/signals")
async def api_signals():
    # return last N signals
    return JSONResponse(_signals_history[:50])

@app.get("/api/signal/{symbol}")
async def api_signal(symbol: str):
    sig = _signals_store.get(symbol.upper())
    if not sig:
        return JSONResponse({"ok": False, "msg": "no signal"}, status_code=404)
    return JSONResponse(sig)

@app.get("/api/scan")
async def api_scan(background: BackgroundTasks):
    # trigger a manual scan in background
    background.add_task(manual_scan)
    return JSONResponse({"ok": True, "msg": "scan scheduled"})

async def manual_scan():
    await periodic_scanner()  # will run one full pass then sleep (but in our design periodic_scanner loops forever)
    # NOTE: in this demo manual_scan simply calls the scanner once; to avoid infinite loops you could refactor periodic_scanner

# ---------------- Run app ----------------
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting monolith demo app on http://127.0.0.1:%d", PORT)
    uvicorn.run("monolith_demo:app", host="127.0.0.1", port=PORT, log_level="info")