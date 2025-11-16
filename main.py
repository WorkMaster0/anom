# main.py
import os
import time
import json
import logging
import re
from datetime import datetime, timezone
from threading import Thread
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import matplotlib.pyplot as plt
import requests
import ta
import mplfinance as mpf
import numpy as np
import io

from binance.client import Client
from binance import ThreadedWebsocketManager

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("pretop-bot")

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")  # paste token or set env
CHAT_ID = os.getenv("CHAT_ID", "")               # paste chat id or set env
PORT = int(os.getenv("PORT", "5000"))
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))  # seconds
STATE_FILE = "state.json"
CONF_THRESHOLD_MEDIUM = float(os.getenv("CONF_THRESHOLD_MEDIUM", "0.3"))
TOP_SYMBOL_LIMIT = int(os.getenv("TOP_SYMBOL_LIMIT", "200"))  # how many top symbols to scan

# ---------------- BINANCE CLIENT ----------------
# Put your keys in env or leave empty for public reads that use REST for klines.
binance_client = Client(api_key=os.getenv("BINANCE_API_KEY", ""), api_secret=os.getenv("BINANCE_API_SECRET", ""))

# ---------------- STATE ----------------
def load_json_safe(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.exception("load_json_safe error %s: %s", path, e)
    return default

def save_json_safe(path, data):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        logger.exception("save_json_safe error %s: %s", path, e)

state = load_json_safe(STATE_FILE, {"signals": {}, "last_scan": None})

# ---------------- TELEGRAM ----------------
MARKDOWNV2_ESCAPE = r"_*[]()~`>#+-=|{}.!"

def escape_md_v2(text: str) -> str:
    return re.sub(f"([{re.escape(MARKDOWNV2_ESCAPE)}])", r"\\\1", str(text))

def send_telegram(text: str, photo=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram credentials missing, skipping send.")
        return
    try:
        if photo:
            files = {'photo': ('signal.png', photo, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=10)
            logger.debug("Telegram sendPhoto status: %s", resp.status_code)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
            logger.debug("Telegram sendMessage status: %s", resp.status_code)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

# ---------------- BINANCE WEBSOCKET (miniTicker) ----------------
_tickers_cache = {}   # symbol -> {lastPrice, changePercent, volume, quoteVolume}
_tickers_last_update = 0
_twm = None

def start_ws_tickers():
    """Запускаємо futures miniTicker websocket для кешування даних топ-символів"""
    global _twm, _tickers_cache, _tickers_last_update
    try:
        _twm = ThreadedWebsocketManager()
        _twm.start()

        def handle(msg):
            # msg is dict for a single update or dict for a combined stream depending on lib version
            try:
                # miniTicker fields include: 's' (symbol), 'c' (close), 'P' (%), 'v' (volume), 'q' (quoteVolume)
                if not isinstance(msg, dict):
                    return
                if "s" in msg and "c" in msg:
                    sym = msg["s"]
                    _tickers_cache[sym] = {
                        "lastPrice": float(msg["c"]),
                        "changePercent": float(msg.get("P", 0.0)),
                        "volume": float(msg.get("v", 0.0)),
                        "quoteVolume": float(msg.get("q", 0.0))
                    }
                    _tickers_last_update = time.time()
            except Exception:
                logger.exception("Error in WS handle")
        # Start futures miniTicker socket
        _twm.start_futures_miniticker_socket(callback=handle)
        logger.info("Started futures miniTicker websocket")
    except Exception as e:
        logger.exception("start_ws_tickers error: %s", e)

def fetch_top_symbols(limit=TOP_SYMBOL_LIMIT):
    """Отримуємо топ символів з кешу websocket по абсолютній % зміні."""
    global _tickers_cache
    if not _tickers_cache:
        logger.debug("Tickers cache empty, falling back to REST/all list")
        # Fallback: try to use REST sparingly to fetch pairs (we do this only if WS not ready)
        try:
            tickers = binance_client.futures_ticker()
            usdt_pairs = [t['symbol'] for t in tickers if t['symbol'].endswith("USDT")]
            return usdt_pairs[:limit] if limit else usdt_pairs
        except Exception as e:
            logger.exception("REST fallback fetch_top_symbols failed: %s", e)
            return []
    usdt_pairs = {s: d for s, d in _tickers_cache.items() if s.endswith("USDT")}
    sorted_pairs = sorted(usdt_pairs.items(), key=lambda kv: abs(kv[1].get("changePercent", 0)), reverse=True)
    symbols = [s for s, _ in sorted_pairs]
    return symbols[:limit] if limit else symbols

# ---------------- FETCH KLINES (REST fallback if WS klines not present) ----------------
BINANCE_REST_URL = "https://fapi.binance.com/fapi/v1/klines"

def fetch_klines_rest(symbol, interval="15m", limit=500):
    try:
        resp = requests.get(BINANCE_REST_URL, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        data = resp.json()
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"
        ])
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        return df
    except Exception as e:
        logger.exception("REST fetch error for %s: %s", symbol, e)
        return None

# If you have a kline websocket manager (like your WebSocketKlineManager), you can plug it in.
# For now we'll use only REST klines to keep code simple and robust.
def fetch_klines(symbol, interval="15m", limit=200):
    # Could be replaced by your WebSocketKlineManager.get_klines(symbol, limit)
    return fetch_klines_rest(symbol, interval=interval, limit=limit)

# ---------------- FEATURE HELPERS ----------------
def apply_basic_candles(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_shadow"] = df[["close", "open"]].min(axis=1) - df["low"]
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    return df

# ---------------- PUMP / DUMP REVERSAL DETECTOR (8 features + 2 extras) ----------------
def detect_pump_reversals(df: pd.DataFrame):
    """
    Return list of signals (dictionaries) detected on the latest candle.
    Each signal: {'action': 'SHORT'/'LONG', 'reason': '...', 'score': float}
    """
    signals = []
    if df is None or len(df) < 30:
        return signals

    df = apply_basic_candles(df)
    last = df.iloc[-1]
    entry = float(last["close"])
    vol_ma = float(df["volume"].rolling(20).mean().iloc[-1])
    recent_change_10 = (last["close"] - df["close"].iloc[-10]) / df["close"].iloc[-10]
    recent_change_5 = (last["close"] - df["close"].iloc[-5]) / df["close"].iloc[-5]

    # helper to append with small score
    def add(action, reason, score=0.1):
        signals.append({"action": action, "reason": reason, "score": score})

    # Feature 1: Volume Spike Pump / Dump (big % move + huge volume)
    if recent_change_10 > 0.18 and last["volume"] > 4 * vol_ma:
        add("SHORT", "Volume Spike Pump", 0.25)
    if recent_change_10 < -0.18 and last["volume"] > 4 * vol_ma:
        add("LONG", "Volume Spike Dump", 0.25)

    # Feature 2: Exhaustion Wick (large upper wick on pump / lower on dump)
    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]
    body = abs(last["body"]) if last["body"] != 0 else 1e-9
    if upper_wick > body * 2 and recent_change_5 > 0.10 and last["volume"] > 1.5 * vol_ma:
        add("SHORT", "Exhaustion Upper Wick", 0.15)
    if lower_wick > body * 2 and recent_change_5 < -0.10 and last["volume"] > 1.5 * vol_ma:
        add("LONG", "Exhaustion Lower Wick", 0.15)

    # Feature 3: Momentum Reversal (3 candles momentum drop)
    mom = df["close"].diff()
    if len(mom) >= 3:
        if mom.iloc[-3] > 0 and mom.iloc[-2] > 0 and mom.iloc[-1] < 0 and recent_change_5 > 0.12:
            add("SHORT", "Momentum Reversal After Pump", 0.12)
        if mom.iloc[-3] < 0 and mom.iloc[-2] < 0 and mom.iloc[-1] > 0 and recent_change_5 < -0.12:
            add("LONG", "Momentum Reversal After Dump", 0.12)

    # Feature 4: RSI Overextension
    rsi = ta.momentum.RSIIndicator(df["close"], 14).rsi()
    last_rsi = rsi.iloc[-1]
    if last_rsi > 82:
        add("SHORT", "RSI Overbought", 0.09)
    if last_rsi < 18:
        add("LONG", "RSI Oversold", 0.09)

    # Feature 5: Volume Divergence (price up but volume down)
    if len(df) >= 4:
        if df["close"].iloc[-1] > df["close"].iloc[-2] > df["close"].iloc[-3] and df["volume"].iloc[-1] < df["volume"].iloc[-2]:
            add("SHORT", "Price Up + Volume Down (Divergence)", 0.10)
        if df["close"].iloc[-1] < df["close"].iloc[-2] < df["close"].iloc[-3] and df["volume"].iloc[-1] < df["volume"].iloc[-2]:
            add("LONG", "Price Down + Volume Down (Divergence)", 0.10)

    # Feature 6: Volatility Spike (ATR explosion)
    recent_vol = (df["high"] - df["low"]).iloc[-5:].mean()
    prev_vol = (df["high"] - df["low"]).iloc[-20:-5].mean()
    if prev_vol > 0 and recent_vol > prev_vol * 2 and last["volume"] > 2 * vol_ma:
        if recent_change_5 > 0.12:
            add("SHORT", "Volatility Spike on Pump", 0.12)
        if recent_change_5 < -0.12:
            add("LONG", "Volatility Spike on Dump", 0.12)

    # Feature 7: Liquidity Grab / Fake Breakout
    rolling_high = df["high"].rolling(20).max().iloc[-2]
    rolling_low = df["low"].rolling(20).min().iloc[-2]
    if last["high"] > rolling_high and last["close"] < rolling_high:
        add("SHORT", "Liquidity Grab Top (Fake Breakout)", 0.14)
    if last["low"] < rolling_low and last["close"] > rolling_low:
        add("LONG", "Liquidity Grab Bottom (Fake Breakout)", 0.14)

    # Feature 8: Climax Candle (huge volume + huge body beyond ATR)
    atr = float(df["atr"].iloc[-1] if not np.isnan(df["atr"].iloc[-1]) else (df["high"]-df["low"]).iloc[-1])
    if last["volume"] > 6 * vol_ma and abs(last["close"] - df["close"].iloc[-2]) > 3 * atr:
        if recent_change_5 > 0.15:
            add("SHORT", "Climax Pump Candle", 0.2)
        if recent_change_5 < -0.15:
            add("LONG", "Climax Dump Candle", 0.2)

    # Extra Feature 9: Smart Money Shift (big buy-side then large distribution)
    # Detect a period where taker buy quote surged then reversed
    # (requires taker data; fallback: check quoteVolume spike)
    if "quote_asset_volume" in df.columns or "quoteVolume" in df.columns:
        # fallback: use quoteVolume last candle if available
        qvol = df.get("quote_asset_volume", df.get("quoteVolume", None))
        if qvol is not None:
            try:
                qvol_last = float(qvol.iloc[-1])
                qvol_ma = float(pd.Series(qvol).rolling(20).mean().iloc[-1])
                if qvol_last > 4 * qvol_ma and recent_change_5 > 0.1:
                    add("SHORT", "Smart Money Distribution (quoteVol spike)", 0.12)
            except Exception:
                pass

    # Extra Feature 10: Trap Candle (wick beyond level + close back)
    # If candle pierces level (20-roll high/low) but closes back -> trap
    if last["high"] > rolling_high and last["close"] < rolling_high and last["upper_shadow"] > last["range"] * 0.6:
        add("SHORT", "Top Trap Candle", 0.12)
    if last["low"] < rolling_low and last["close"] > rolling_low and last["lower_shadow"] > last["range"] * 0.6:
        add("LONG", "Bottom Trap Candle", 0.12)

    # Merge same-direction signals to produce a combined score
    combined = {}
    for s in signals:
        combined.setdefault(s["action"], 0.0)
        combined[s["action"]] += s["score"]

    # produce final signals list with threshold
    output = []
    for action, score in combined.items():
        # signal threshold — require at least 0.18 combined weight to alert
        if score >= 0.18:
            # also include reasons (collect all reasons for that action)
            reasons = [s["reason"] for s in signals if s["action"] == action]
            output.append({"action": action, "score": round(min(1.0, score), 3), "reasons": reasons})
    return output

# ---------------- PLOT UTILITY ----------------
def plot_signal_candles(df, symbol, action, tp1=None, tp2=None, tp3=None, sl=None, entry=None):
    addplots = []
    if tp1 is not None: addplots.append(mpf.make_addplot([tp1]*len(df), color='green', linestyle="--"))
    if tp2 is not None: addplots.append(mpf.make_addplot([tp2]*len(df), color='lime', linestyle="--"))
    if tp3 is not None: addplots.append(mpf.make_addplot([tp3]*len(df), color='darkgreen', linestyle="--"))
    if sl is not None: addplots.append(mpf.make_addplot([sl]*len(df), color='red', linestyle="--"))
    if entry is not None: addplots.append(mpf.make_addplot([entry]*len(df), color='blue', linestyle="--"))

    fig, ax = mpf.plot(df.tail(200), type='candle', style='yahoo', title=f"{symbol} - {action}", addplot=addplots, returnfig=True)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf

# ---------------- ANALYZE & ALERT (pump/dump mode) ----------------
def analyze_and_alert(symbol: str):
    try:
        df = fetch_klines(symbol, interval="15m", limit=200)
        if df is None or len(df) < 30:
            return

        signals = detect_pump_reversals(df)
        if not signals:
            return

        # For each detected direction produce an alert
        for s in signals:
            action = s["action"]
            score = s["score"]
            reasons = s["reasons"]
            last = df.iloc[-1]
            entry = float(last["close"])

            # Use far support/resistance (20-50 candles window) to set sl/tp around real levels
            support = df["low"].rolling(50).min().iloc[-1]
            resistance = df["high"].rolling(50).max().iloc[-1]

            if action == "SHORT":
                stop_loss = float(last["high"] * 1.01)
                # TPs should be placed before important supports — use a ladder approaching long-term support
                tp1 = entry * 0.985
                tp2 = max(entry * 0.96, support * 1.01 if not np.isnan(support) else entry * 0.95)
                tp3 = max(entry * 0.92, support * 1.005 if not np.isnan(support) else entry * 0.90)
            else:  # LONG
                stop_loss = float(last["low"] * 0.99)
                tp1 = entry * 1.015
                tp2 = min(entry * 1.04, resistance * 0.995 if not np.isnan(resistance) else entry * 1.06)
                tp3 = min(entry * 1.08, resistance * 0.999 if not np.isnan(resistance) else entry * 1.12)

            # Risk/Reward calculation (conservative)
            rr1 = (tp1 - entry) / (entry - stop_loss) if action == "LONG" else (entry - tp1) / (stop_loss - entry)
            rr2 = (tp2 - entry) / (entry - stop_loss) if action == "LONG" else (entry - tp2) / (stop_loss - entry)
            rr3 = (tp3 - entry) / (entry - stop_loss) if action == "LONG" else (entry - tp3) / (stop_loss - entry)

            # Quality filter: require some minimal combined score and RR1 > 0.8 (pump reversals are aggressive, allow lower RR)
            if score < 0.2:
                continue
            if rr1 < 0.5:
                # if first RR too low, push TP1 a bit closer to increase RR or skip
                # here we skip to avoid spam
                continue

            # Compose message
            msg = (
                f"⚡ PUMP/DUMP REVERSAL ALERT\n"
                f"Symbol: {symbol}\n"
                f"Action: {action}\n"
                f"Score: {score:.2f}\n"
                f"Reasons: {', '.join(reasons)}\n"
                f"Entry: {entry:.6f}\n"
                f"Stop-Loss: {stop_loss:.6f}\n"
                f"TP1: {tp1:.6f} (RR {rr1:.2f})\n"
                f"TP2: {tp2:.6f} (RR {rr2:.2f})\n"
                f"TP3: {tp3:.6f} (RR {rr3:.2f})\n"
            )

            logger.info("Signal %s %s score=%.2f rr1=%.2f reasons=%s", symbol, action, score, rr1, reasons)
            photo_buf = plot_signal_candles(df, symbol, action, tp1=tp1, tp2=tp2, tp3=tp3, sl=stop_loss, entry=entry)
            send_telegram(msg, photo=photo_buf)

            # save state
            state.setdefault("signals", {})[symbol] = {
                "action": action, "entry": entry, "sl": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "rr1": rr1, "rr2": rr2, "rr3": rr3, "score": score, "reasons": reasons,
                "time": str(df.index[-1]), "last_price": float(entry)
            }
            save_json_safe(STATE_FILE, state)
    except Exception as e:
        logger.exception("analyze_and_alert error for %s: %s", symbol, e)

# ---------------- MASTER SCAN ----------------
def scan_all_symbols():
    symbols = fetch_top_symbols(limit=TOP_SYMBOL_LIMIT)
    if not symbols:
        logger.warning("No symbols fetched, falling back to empty list")
        return
    logger.info("Starting scan for %d symbols", len(symbols))
    def safe_analyze(sym):
        try:
            analyze_and_alert(sym)
        except Exception as e:
            logger.exception("Error analyzing symbol %s: %s", sym, e)
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
        list(exe.map(safe_analyze, symbols))
    state["last_scan"] = str(datetime.now(timezone.utc))
    save_json_safe(STATE_FILE, state)
    logger.info("Scan finished at %s", state["last_scan"])

# ---------------- BACKGROUND SCANNER ----------------
def background_scanner():
    while True:
        try:
            scan_all_symbols()
        except Exception as e:
            logger.exception("Background scan error: %s", e)
        time.sleep(SCAN_INTERVAL)

# Start WS listener thread early
Thread(target=start_ws_tickers, daemon=True).start()
# Start background scanner thread
Thread(target=background_scanner, daemon=True).start()

# ---------------- FLASK ----------------
from flask import Flask, request, jsonify
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "time": str(datetime.now(timezone.utc)),
        "signals": len(state.get("signals", {}))
    })

@app.route("/telegram_webhook/<token>", methods=["POST"])
def telegram_webhook(token):
    if token != TELEGRAM_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    update = request.get_json(force=True) or {}
    text = update.get("message", {}).get("text", "").lower().strip()
    if text.startswith("/scan"):
        send_telegram("⚡ Manual scan started.")
        Thread(target=scan_all_symbols, daemon=True).start()
    if text.startswith("/status"):
        send_telegram(f"Status: last_scan={state.get('last_scan')}, signals={len(state.get('signals', {}))}")
    return jsonify({"ok": True})

# ---------------- MAIN ----------------
if __name__ == "__main__":
    logger.info("Starting pump/dump detector bot")
    # quick test Telegram connectivity (will be skipped silently if no token)
    try:
        send_telegram("✅ Pump/Dump detector bot started.")
    except Exception:
        pass
    # main web server
    app.run(host="0.0.0.0", port=PORT)