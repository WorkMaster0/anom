#!/usr/bin/env python3
"""
Lite Strategy Bot
- EMA50 + ADX trend context
- Entry: retest support / false-break / wick with volume confirmation
- TP/SL from swing levels (fallback to ATR), RR >= rr_target
- Backtest uses next `lookahead` bars to decide TP/SL hit
- Plots candlestick charts (mplfinance) with Entry/SL/TP
"""

import os, time, json, logging, io, threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
import mplfinance as mpf
import matplotlib.pyplot as plt

# ---------------- CONFIG ----------------
BINANCE_REST_KLINES = "https://fapi.binance.com/fapi/v1/klines"
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "5000"))
STATE_FILE = "state_lite.json"

INTERVAL = "3m"
FETCH_LIMIT = 1000
PLOT_BARS = 150

ADX_PERIOD = 14
EMA_PERIOD = 50
ATR_PERIOD = 14

SWING_LOOKBACK = 20
BACKTEST_LOOKAHEAD = 20
RR_TARGET = 2.5

CONF_THRESHOLD = 0.0   # мінімальна confidence — легка перевірка
MIN_SCORE = 0.0

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lite-bot")

# ---------------- STATE ----------------
def load_state(path):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.debug("load_state error: %s", e)
    return {"signals": {}, "last_scan": None}

def save_state(path, data):
    try:
        with open(path + ".tmp", "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(path + ".tmp", path)
    except Exception as e:
        logger.debug("save_state error: %s", e)

state = load_state(STATE_FILE)

# ---------------- HELPERS: REST KLINES ----------------
def fetch_klines_rest(symbol, interval=INTERVAL, limit=FETCH_LIMIT):
    try:
        resp = requests.get(BINANCE_REST_KLINES, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"
        ])
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        return df
    except Exception as e:
        logger.debug("fetch_klines_rest error %s %s", symbol, e)
        return None

def fetch_top_symbols(limit=30):
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=10)
        r.raise_for_status()
        tickers = r.json()
        usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
        sorted_pairs = sorted(usdt, key=lambda x: abs(float(x.get("priceChangePercent", 0))), reverse=True)
        symbols = [d["symbol"] for d in sorted_pairs[:limit]]
        logger.info("Fetched %d symbols: %s", len(symbols), symbols[:10])
        return symbols
    except Exception as e:
        logger.warning("fetch_top_symbols REST failed: %s", e)
        return []

# ---------------- INDICATORS & FEATURES ----------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Adds EMA50, ATR, ADX (simple), volume MA, support/resistance and candle geometry."""
    df = df.copy()
    df["ema50"] = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean()

    # True Range and ATR
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["close"].shift(1)).abs()
    df["tr3"] = (df["low"] - df["close"].shift(1)).abs()
    df["tr"] = df[["tr1","tr2","tr3"]].max(axis=1)
    df["atr"] = df["tr"].rolling(ATR_PERIOD, min_periods=1).mean()

    # Simple ADX approximation using directional movement (not optimized but works)
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    atr_sm = df["tr"].rolling(ADX_PERIOD, min_periods=1).mean()
    plus_di = 100 * (pd.Series(plus_dm).rolling(ADX_PERIOD, min_periods=1).mean() / atr_sm)
    minus_di = 100 * (pd.Series(minus_dm).rolling(ADX_PERIOD, min_periods=1).mean() / atr_sm)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)).replace([np.inf, -np.inf], 0).fillna(0) * 100
    df["adx"] = dx.ewm(span=ADX_PERIOD, adjust=False).mean()

    # Volume MA
    df["vol_ma20"] = df["volume"].rolling(20, min_periods=1).mean()

    # Support/Resistance (rolling)
    df["support"] = df["low"].rolling(SWING_LOOKBACK, min_periods=1).min()
    df["resistance"] = df["high"].rolling(SWING_LOOKBACK, min_periods=1).max()

    # Candle geometry
    df["body"] = df["close"] - df["open"]
    df["upper_shadow"] = df["high"] - df[["close","open"]].max(axis=1)
    df["lower_shadow"] = df[["close","open"]].min(axis=1) - df["low"]

    return df

# ---------------- SWING LEVELS ----------------
def swing_low(df, lookback=SWING_LOOKBACK):
    if len(df) < lookback + 1:
        return df["low"].min()
    return df["low"].iloc[-(lookback+1):-1].min()

def swing_high(df, lookback=SWING_LOOKBACK):
    if len(df) < lookback + 1:
        return df["high"].max()
    return df["high"].iloc[-(lookback+1):-1].max()

# ---------------- SIGNAL LOGIC (CORE) ----------------
def detect_signal(df: pd.DataFrame):
    """
    Return (action:str|None, reasons:dict, confidence:float)
    action: "LONG" / "SHORT" / None
    """
    if df is None or len(df) < 10:
        return None, {}, 0.0

    df = add_indicators(df)
    last = df.iloc[-1]
    prev = df.iloc[-2]

    reasons = {}
    confidence = 0.0

    # Trend context
    trend_long = last["close"] > last["ema50"]
    trend_short = last["close"] < last["ema50"]
    adx_strong = last["adx"] > 20

    # Volume confirmation
    vol_ok = last["volume"] >= max(1.0, last["vol_ma20"]*0.8)

    # LONG conditions (retest support OR false-break low OR long lower wick)
    long_retest = (last["low"] <= last["support"] * 1.001) and (last["close"] > last["support"])
    false_break_low = (last["low"] < last["support"]) and (last["close"] > last["support"])
    long_wick = last["lower_shadow"] > 1.5 * abs(last["body"]) and last["close"] > last["open"]

    # SHORT conditions
    short_retest = (last["high"] >= last["resistance"] * 0.999) and (last["close"] < last["resistance"])
    false_break_high = (last["high"] > last["resistance"]) and (last["close"] < last["resistance"])
    short_wick = last["upper_shadow"] > 1.5 * abs(last["body"]) and last["close"] < last["open"]

    # Compose decision
    action = None
    # LONG path
    if trend_long and adx_strong and vol_ok and (long_retest or false_break_low or long_wick):
        action = "LONG"
        confidence += 0.5
        if long_retest: confidence += 0.15; reasons["long_retest"] = True
        if false_break_low: confidence += 0.15; reasons["false_break_low"] = True
        if long_wick: confidence += 0.10; reasons["long_wick"] = True
        if vol_ok: reasons["vol_ok"] = True
        reasons["trend_long"] = True
    # SHORT path
    elif trend_short and adx_strong and vol_ok and (short_retest or false_break_high or short_wick):
        action = "SHORT"
        confidence += 0.5
        if short_retest: confidence += 0.15; reasons["short_retest"] = True
        if false_break_high: confidence += 0.15; reasons["false_break_high"] = True
        if short_wick: confidence += 0.10; reasons["short_wick"] = True
        if vol_ok: reasons["vol_ok"] = True
        reasons["trend_short"] = True

    confidence = min(1.0, confidence)
    return action, reasons, confidence

# ---------------- LEVELS: ENTRY/SL/TP ----------------
def calculate_levels(df: pd.DataFrame, action: str, rr_target=RR_TARGET):
    """
    Entry: last close
    SL: swing low/high (previous SWING_LOOKBACK bars), fallback on ATR
    TP: computed by rr_target
    Returns (entry, sl, tp)
    """
    if df is None or len(df) < 3 or action not in ("LONG", "SHORT"):
        return None, None, None

    df = add_indicators(df)
    entry = df["close"].iloc[-1]
    atr = df["atr"].iloc[-1] if not np.isnan(df["atr"].iloc[-1]) and df["atr"].iloc[-1] > 0 else (df["high"].iloc[-SWING_LOOKBACK:].max() - df["low"].iloc[-SWING_LOOKBACK:].min()) / SWING_LOOKBACK
    # Swing excludes the current bar (use previous lookback bars)
    if action == "LONG":
        sl = swing_low(df, SWING_LOOKBACK)
        if np.isnan(sl) or sl >= entry:
            sl = entry - max(atr, entry * 0.001)
        tp = entry + (entry - sl) * rr_target
    else:
        sl = swing_high(df, SWING_LOOKBACK)
        if np.isnan(sl) or sl <= entry:
            sl = entry + max(atr, entry * 0.001)
        tp = entry - (sl - entry) * rr_target

    # Safety caps: not more than ±50% of price
    max_move = abs(entry) * 0.5
    tp = max(min(tp, entry + max_move), entry - max_move)
    # Ensure tp and sl are sensible
    if action == "LONG" and tp <= entry:
        tp = entry + max(atr * rr_target, entry * 0.001)
    if action == "SHORT" and tp >= entry:
        tp = entry - max(atr * rr_target, entry * 0.001)

    return entry, sl, tp

# ---------------- PLOT: candlesticks + lines (mplfinance) ----------------
def plot_trade_chart(df: pd.DataFrame, symbol: str, entry, sl, tp, action):
    """
    Uses mplfinance to draw last PLOT_BARS candles with entry/sl/tp annotated.
    Returns PNG bytes.
    """
    dfp = df.tail(PLOT_BARS).copy()
    if dfp.empty:
        return None

    # mplfinance expects columns Open/High/Low/Close and optional Volume
    dfp_plot = dfp[["open","high","low","close","volume"]].copy()
    dfp_plot.columns = ["Open","High","Low","Close","Volume"]

    # build addplot(s) for horizontal lines
    ap = []
    # create a series aligned with dfp_plot.index
    entry_s = pd.Series([entry]*len(dfp_plot), index=dfp_plot.index)
    sl_s = pd.Series([sl]*len(dfp_plot), index=dfp_plot.index)
    tp_s = pd.Series([tp]*len(dfp_plot), index=dfp_plot.index)
    ap.append(mpf.make_addplot(entry_s, color="orange", linestyle="--"))
    ap.append(mpf.make_addplot(sl_s, color="red", linestyle="--"))
    ap.append(mpf.make_addplot(tp_s, color="green", linestyle="--"))

    title = f"{symbol} | {action} | Entry {entry:.6f}  TP {tp:.6f}  SL {sl:.6f}"
    fig, axes = mpf.plot(dfp_plot, type="candle", style="charles", volume=True, addplot=ap, title=title, returnfig=True, figsize=(12,8))
    # Save to buffer
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()

# ---------------- BACKTEST (realistic) ----------------
def backtest_symbol(df: pd.DataFrame, symbol: str, lookahead=BACKTEST_LOOKAHEAD):
    """
    Walk through history and collect signals + outcomes (TP/SL within `lookahead` bars).
    Returns DataFrame of signals and results.
    """
    signals = []
    for i in range(SWING_LOOKBACK + EMA_PERIOD, len(df) - lookahead):
        sub = df.iloc[:i+1].copy()
        action, reasons, conf = detect_signal(sub)
        if action is None:
            continue
        entry, sl, tp = calculate_levels(sub, action)
        if None in (entry, sl, tp):
            continue
        future = df.iloc[i+1:i+1+lookahead]
        hit_tp = False
        hit_sl = False
        for _, f in future.iterrows():
            if action == "LONG":
                if f["high"] >= tp:
                    hit_tp = True
                    break
                if f["low"] <= sl:
                    hit_sl = True
                    break
            else:
                if f["low"] <= tp:
                    hit_tp = True
                    break
                if f["high"] >= sl:
                    hit_sl = True
                    break
        result = "TP" if hit_tp else ("SL" if hit_sl else "NONE")
        signals.append({
            "time": sub.index[-1], "symbol": symbol, "action": action, "entry": entry,
            "sl": sl, "tp": tp, "confidence": conf, "reasons": reasons, "result": result
        })
    return pd.DataFrame(signals)

# ---------------- LIVE ANALYSIS & ALERT ----------------
def analyze_and_alert(symbol: str):
    df = fetch_klines_rest(symbol, interval=INTERVAL, limit=500)
    if df is None or len(df) < SWING_LOOKBACK + 10:
        return
    action, reasons, confidence = detect_signal(df)
    if action is None:
        return

    entry, sl, tp = calculate_levels(df, action)
    if None in (entry, sl, tp):
        return

    rr = abs((tp - entry) / (entry - sl)) if (entry - sl) != 0 else None

    # Basic filters
    if confidence < CONF_THRESHOLD:
        logger.debug("%s filtered by confidence %.3f", symbol, confidence)
        return
    if rr is None or rr < 1.5:   # allow looser RR for live
        logger.debug("%s filtered by RR %.3f", symbol, rr)
        return

    # Build message & chart
    msg = (
        f"⚡ Lite SIGNAL\nSymbol: {symbol}\nSide: {action}\nEntry: {entry:.6f}\nTP: {tp:.6f}\nSL: {sl:.6f}\nRR: {rr:.2f}\n"
        f"Confidence: {confidence:.2f}\nReasons: {', '.join(reasons.keys())}"
    )
    logger.info("Signal %s | %s | RR=%.2f | reasons=%s", symbol, action, rr or 0.0, list(reasons.keys()))
    chart = plot_trade_chart(df, symbol, entry, sl, tp, action)
    # send to telegram if enabled
    if TELEGRAM_TOKEN and CHAT_ID:
        try:
            send_telegram(msg, photo=chart)
        except Exception as e:
            logger.debug("telegram send failed: %s", e)
    else:
        # save chart locally for quick check
        try:
            if chart:
                fname = f"signal_{symbol}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.png"
                with open(fname, "wb") as f:
                    f.write(chart)
                logger.info("Saved chart to %s", fname)
        except Exception:
            pass

    # save state
    state.setdefault("signals", {})[symbol] = {
        "action": action, "entry": entry, "sl": sl, "tp": tp, "rr": rr, "confidence": confidence,
        "reasons": reasons, "time": str(df.index[-1])
    }
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_state(STATE_FILE, state)

# ---------------- TELEGRAM (simple) ----------------
def escape_md_v2(text: str) -> str:
    esc = r"_*[]()~`>#+-=|{}.!"
    return ''.join("\\"+c if c in esc else c for c in text)

def send_telegram(text: str, photo=None, tries=1):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto" if photo else f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        if photo:
            files = {'photo': ('signal.png', io.BytesIO(photo), 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            requests.post(url, data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.debug("send_telegram error: %s", e)
        if tries > 0:
            time.sleep(1)
            send_telegram(text, photo=photo, tries=tries-1)

# ---------------- SCAN ----------------
def fetch_top_symbols(limit=30):
    # try public REST for tickers (safer than binance client)
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=10)
        r.raise_for_status()
        tickers = r.json()
        usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
        sorted_pairs = sorted(usdt, key=lambda x: abs(float(x.get("priceChangePercent", 0))), reverse=True)
        return [d["symbol"] for d in sorted_pairs[:limit]]
    except Exception as e:
        logger.debug("fetch_top_symbols REST failed: %s", e)
        return []

def scan_all(limit=30):
    symbols = fetch_top_symbols(limit)
    if not symbols:
        logger.warning("No symbols fetched")
        return
    logger.info("Scanning %d symbols", len(symbols))
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as ex:
        list(ex.map(analyze_and_alert, symbols))
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_state(STATE_FILE, state)

# ---------------- SIMPLE HTTP SERVER (optional) ----------------
def start_http(port=PORT):
    import http.server, socketserver
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            logger.debug("HTTP: " + format % args)
    try:
        with socketserver.TCPServer(("", port), Handler) as httpd:
            logger.info("HTTP server listening on port %d", port)
            httpd.serve_forever()
    except Exception as e:
        logger.debug("HTTP server error: %s", e)

# ---------------- MAIN ----------------
if __name__ == "__main__":
    # start http for hosting health (optional)
    threading.Thread(target=start_http, daemon=True).start()

    logger.info("Lite bot starting — Backtest + Live scan")

    # Quick backtest on top symbols (one-off)
    try:
        syms = fetch_top_symbols(limit=10)
        all_stats = []
        for s in syms:
            df = fetch_klines_rest(s, interval=INTERVAL, limit=1000)
            if df is None or len(df) < SWING_LOOKBACK + 50: 
                continue
            df = add_indicators(df)
            stats = backtest_symbol(df, s, lookahead=BACKTEST_LOOKAHEAD)
            if not stats.empty:
                winrate = (stats["result"] == "TP").mean()
                logger.info("Backtest %s signals=%d winrate=%.2f", s, len(stats), winrate)
                all_stats.append((s, len(stats), winrate))
    except Exception as e:
        logger.debug("Backtest phase failed: %s", e)

    # Live loop
    try:
        while True:
            try:
                scan_all(limit=20)
            except Exception as e:
                logger.debug("scan_all error: %s", e)
            time.sleep(3 * 60)  # run every 3 minutes
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")