#!/usr/bin/env python3
# main.py - Pro version with WS-first, REST caching, rate-limiter, plot fixes

import os
import time
import json
import logging
import re
import random
from collections import deque
from datetime import datetime, timezone, timedelta
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple, List

import pandas as pd
import matplotlib.pyplot as plt
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import ta
import mplfinance as mpf
import numpy as np
import io

# Binance client (optional)
from binance.client import Client

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("pretop-bot")

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "5000"))
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
EMA_SCAN_LIMIT = int(os.getenv("EMA_SCAN_LIMIT", "500"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
CONF_THRESHOLD_MEDIUM = float(os.getenv("CONF_THRESHOLD_MEDIUM", "0.3"))
AGGRESSION = float(os.getenv("AGGRESSION", "0.3"))
SIGNAL_COOLDOWN_MIN = int(os.getenv("SIGNAL_COOLDOWN_MIN", "60"))
REST_RATE_LIMIT_PER_MIN = int(os.getenv("REST_RATE_LIMIT_PER_MIN", "1800"))  # safe default < Binance limit
REST_CACHE_TTL = int(os.getenv("REST_CACHE_TTL", "60"))  # seconds
SCAN_COOLDOWN_SEC = int(os.getenv("SCAN_COOLDOWN_SEC", "30"))

# ---------------- BINANCE ----------------
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
binance_client = None
try:
    if BINANCE_API_KEY and BINANCE_API_SECRET:
        binance_client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
except Exception:
    logger.exception("Failed to init Binance client")

# ---------------- HTTP session with retries ----------------
def create_session(retries=3, backoff=0.3):
    s = requests.Session()
    retry = Retry(total=retries, backoff_factor=backoff,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=frozenset(['GET', 'POST']))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    return s

session = create_session(retries=4, backoff=0.5)

# ---------------- STATE ----------------
def load_json_safe(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        logger.exception("load_json_safe error %s", path)
    return default

def save_json_safe(path: str, data):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        logger.exception("save_json_safe error %s", path)

state = load_json_safe(STATE_FILE, {"signals": {}, "last_scan": None})

# ---------------- TELEGRAM ----------------
_TELEGRAM_MD_V2_SPECIAL = r'_*[]()~`>#+-=|{}.!'
_md_v2_re = re.compile(r'([%s])' % re.escape(_TELEGRAM_MD_V2_SPECIAL))

def escape_md_v2(text: str) -> str:
    return _md_v2_re.sub(r'\\\1', str(text))

def send_telegram(text: str, photo: Optional[bytes]=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram not configured, skip send.")
        return
    try:
        if photo:
            files = {'photo': ('signal.png', photo, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=10)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception:
        logger.exception("send_telegram error")

# ---------------- WEBSOCKET MANAGER (optional) ----------------
try:
    from websocket_manager import WebSocketKlineManager
    HAS_WS = True
except Exception:
    HAS_WS = False
    WebSocketKlineManager = None
    logger.info("websocket_manager not available - WS disabled")

ALL_USDT = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT",
    "DOTUSDT","TRXUSDT","LTCUSDT","AVAXUSDT","SHIBUSDT","LINKUSDT","ATOMUSDT","XMRUSDT",
    "ETCUSDT","XLMUSDT","APTUSDT","NEARUSDT","FILUSDT","ICPUSDT","GRTUSDT","AAVEUSDT"
]

BINANCE_REST_URL = "https://fapi.binance.com/fapi/v1/klines"

ws_manager = None
if HAS_WS and WebSocketKlineManager:
    try:
        ws_manager = WebSocketKlineManager(symbols=ALL_USDT, interval="15m")
        Thread(target=ws_manager.start, daemon=True).start()
        logger.info("WebSocket manager started")
    except Exception:
        logger.exception("Failed to start WebSocket manager")
        ws_manager = None

# ---------------- REST rate limiter (simple sliding window) ----------------
rest_lock = Lock()
rest_timestamps = deque()

def rest_wait_for_slot():
    """
    Ensure we do not exceed REST_RATE_LIMIT_PER_MIN per 60 seconds.
    Blocks (sleep) until a slot is available.
    """
    with rest_lock:
        now = time.time()
        # remove old timestamps
        while rest_timestamps and now - rest_timestamps[0] > 60:
            rest_timestamps.popleft()
        if len(rest_timestamps) < REST_RATE_LIMIT_PER_MIN:
            rest_timestamps.append(now)
            return
        # otherwise need to wait until the oldest falls out of window
        oldest = rest_timestamps[0]
        wait = 60 - (now - oldest) + 0.01
    logger.debug("REST rate limit reached; sleeping %.2fs", wait)
    time.sleep(wait)
    # recursive attempt
    rest_wait_for_slot()

# ---------------- REST cache ----------------
klines_cache = {}
cache_lock = Lock()

def cache_get(symbol):
    with cache_lock:
        v = klines_cache.get(symbol)
        if not v:
            return None
        df, ts = v
        if time.time() - ts > REST_CACHE_TTL:
            del klines_cache[symbol]
            return None
        return df

def cache_set(symbol, df):
    with cache_lock:
        klines_cache[symbol] = (df, time.time())

# ---------------- FETCH KLÍNES (WS first, then REST with rate-limiting + cache) ----------------
def fetch_klines_rest(symbol, interval="15m", limit=500) -> Optional[pd.DataFrame]:
    # check cache
    df_cached = cache_get(symbol)
    if df_cached is not None:
        return df_cached

    # rate limit slot
    rest_wait_for_slot()

    # do REST call with retries already handled by session
    try:
        resp = session.get(BINANCE_REST_URL, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=8)
        resp.raise_for_status()
        data = resp.json()
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"
        ])
        for col in ["open","high","low","close","volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        cache_set(symbol, df)
        return df
    except Exception:
        logger.exception("Binance REST error fetch klines %s", symbol)
        return None

def fetch_klines(symbol: str, limit=500) -> Optional[pd.DataFrame]:
    # try ws_manager first
    if ws_manager:
        try:
            df = ws_manager.get_klines(symbol, limit)
            if df is not None and len(df) >= min(30, limit):
                return df
        except Exception:
            logger.debug("ws fetch failed for %s - fallback to REST", symbol)
    # fallback to REST (cached + rate-limited)
    return fetch_klines_rest(symbol, limit=limit)

# ---------------- FEATURE ENGINEERING ----------------
def apply_all_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().dropna()
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_shadow"] = df[["close", "open"]].min(axis=1) - df["low"]

    look = 20
    df["support"] = df["low"].rolling(look).min()
    df["resistance"] = df["high"].rolling(look).max()

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
    except Exception:
        logger.exception("ta indicators failed")
    return df

# ---------------- DETECT SIGNAL ----------------
def detect_signal_v2(df: pd.DataFrame) -> Tuple[str, List[str], bool, pd.Series, float]:
    df = df.copy().dropna()
    if len(df) < 20:
        last = df.iloc[-1] if len(df) else pd.Series()
        return "WATCH", [], False, last, 0.0

    last = df.iloc[-1]
    prev = df.iloc[-2]
    votes = []
    confidence = 0.45

    # Hammer / Shooting star
    if last["lower_shadow"] > 2 * max(abs(last["body"]), 1e-9) and last["body"] > 0:
        votes.append("hammer_bull"); confidence += 0.08
    if last["upper_shadow"] > 2 * max(abs(last["body"]), 1e-9) and last["body"] < 0:
        votes.append("shooting_star"); confidence += 0.08

    # Engulfing
    if last["body"] > 0 and prev["body"] < 0 and last["close"] > prev["open"] and last["open"] < prev["close"]:
        votes.append("bullish_engulfing"); confidence += 0.06
    if last["body"] < 0 and prev["body"] > 0 and last["close"] < prev["open"] and last["open"] > prev["close"]:
        votes.append("bearish_engulfing"); confidence += 0.06

    # Doji
    if abs(last["body"]) < 0.12 * max(last["range"], 1e-9):
        votes.append("doji"); confidence += 0.03

    # Inside / outside
    if last["high"] < prev["high"] and last["low"] > prev["low"]:
        votes.append("inside_bar"); confidence += 0.03
    if last["high"] > prev["high"] and last["low"] < prev["low"]:
        votes.append("outside_bar"); confidence += 0.03

    # 3 candles
    if len(df) >= 3:
        if all(df["close"].iloc[-i] > df["open"].iloc[-i] for i in range(1, 4)):
            votes.append("3_green"); confidence += 0.03
        if all(df["close"].iloc[-i] < df["open"].iloc[-i] for i in range(1, 4)):
            votes.append("3_red"); confidence += 0.03

    # Volume
    if last.get("vol_spike", False):
        votes.append("volume_spike"); confidence += 0.04
    if last["volume"] > 2 * df["vol_ma20"].iloc[-1]:
        votes.append("climax_volume"); confidence += 0.04

    # Structure flips
    if prev["close"] > prev.get("resistance", 0) and last["close"] < last.get("resistance", 0):
        votes.append("fake_breakout_short"); confidence += 0.04
    if prev["close"] < prev.get("support", 0) and last["close"] > last.get("support", 0):
        votes.append("fake_breakout_long"); confidence += 0.04
    if prev["close"] < prev.get("resistance", 0) and last["close"] > last.get("resistance", 0):
        votes.append("resistance_flip_support"); confidence += 0.04
    if prev["close"] > prev.get("support", 0) and last["close"] < last.get("support", 0):
        votes.append("support_flip_resistance"); confidence += 0.04

    # Retest / liquidity grab
    if last.get("support") and abs(last["close"] - last["support"]) / max(last["support"], 1e-9) < 0.003 and last["body"] > 0:
        votes.append("support_retest"); confidence += 0.03
    if last.get("resistance") and abs(last["close"] - last["resistance"]) / max(last["resistance"], 1e-9) < 0.003 and last["body"] < 0:
        votes.append("resistance_retest"); confidence += 0.03
    if last["low"] < last.get("support", 0) and last["close"] > last.get("support", 0):
        votes.append("liquidity_grab_long"); confidence += 0.03
    if last["high"] > last.get("resistance", 0) and last["close"] < last.get("resistance", 0):
        votes.append("liquidity_grab_short"); confidence += 0.03

    # Trend via EMA
    trend_val = df.get("ema21", pd.Series(df["close"].rolling(20).mean())).iloc[-1]
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
        if last["macd"] > last["macd_signal"] and prev["macd"] <= prev["macd_signal"]:
            votes.append("macd_cross_up"); confidence += 0.03
        if last["macd"] < last["macd_signal"] and prev["macd"] >= prev["macd_signal"]:
            votes.append("macd_cross_down"); confidence += 0.03

    # Pre-top detection
    pretop = False
    if len(df) >= 10 and (last["close"] - df["close"].iloc[-10]) / max(df["close"].iloc[-10], 1e-9) > 0.10:
        pretop = True
        votes.append("pretop"); confidence += 0.06

    # Action by proximity
    near_resistance = last.get("resistance") and last["close"] >= last["resistance"] * 0.985
    near_support = last.get("support") and last["close"] <= last["support"] * 1.015
    action = "WATCH"
    if near_resistance:
        action = "SHORT"
    elif near_support:
        action = "LONG"

    # Aggression nudges
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

# ---------------- PLOT (fix shapes) ----------------
def plot_signal_candles(df: pd.DataFrame, symbol: str, action: str, tp1=None, tp2=None, tp3=None, sl=None, entry=None) -> bytes:
    df_tail = df.tail(200)
    N = len(df_tail)
    if N == 0:
        return None

    def line(y):
        return [y] * N if y is not None else None

    addplots = []
    if tp1 is not None: addplots.append(mpf.make_addplot(line(tp1), linestyle="--"))
    if tp2 is not None: addplots.append(mpf.make_addplot(line(tp2), linestyle="--"))
    if tp3 is not None: addplots.append(mpf.make_addplot(line(tp3), linestyle="--"))
    if sl is not None:  addplots.append(mpf.make_addplot(line(sl), linestyle="--"))
    if entry is not None: addplots.append(mpf.make_addplot(line(entry), linestyle="--"))

    fig, ax = mpf.plot(df_tail, type='candle', style='yahoo', title=f"{symbol} - {action}",
                       addplot=addplots, returnfig=True)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()

# ---------------- FETCH TOP SYMBOLS ----------------
def fetch_top_symbols(limit=300) -> List[str]:
    if binance_client:
        try:
            tickers = binance_client.futures_ticker()
            usdt_pairs = [t for t in tickers if t['symbol'].endswith("USDT")]
            sorted_pairs = sorted(usdt_pairs, key=lambda x: abs(float(x.get("priceChangePercent", 0))), reverse=True)
            top_symbols = [d["symbol"] for d in sorted_pairs[:limit]]
            logger.info("Top %d symbols fetched via Binance API", limit)
            return top_symbols
        except Exception:
            logger.exception("Error fetching top symbols via Binance client")
    logger.info("Falling back to ALL_USDT")
    return ALL_USDT[:limit]

# ---------------- COOL-DOWN for signals ----------------
def can_send_signal(symbol: str, last_time_iso: Optional[str], cooldown_min: int = SIGNAL_COOLDOWN_MIN) -> bool:
    if not last_time_iso:
        return True
    try:
        last_time = datetime.fromisoformat(last_time_iso)
        return datetime.now(timezone.utc) - last_time >= timedelta(minutes=cooldown_min)
    except Exception:
        return True

# ---------------- ANALYZE & ALERT ----------------
def analyze_and_alert(symbol: str):
    df = fetch_klines(symbol, limit=200)
    if df is None or len(df) < 40:
        logger.debug("Not enough data for %s", symbol)
        return

    df = apply_all_features(df)
    action, votes, pretop, last, confidence = detect_signal_v2(df)

    if action == "WATCH":
        return

    # compute entry/sl/tps
    entry = stop_loss = tp1 = tp2 = tp3 = None
    try:
        if action == "LONG":
            entry = last.get("support") * 1.001 if last.get("support") else last["close"] * 0.999
            stop_loss = entry - max(1.5 * last.get("atr14", 0.0), entry * 0.01)
            tp1 = entry + (last.get("resistance", entry) - entry) * 0.33
            tp2 = entry + (last.get("resistance", entry) - entry) * 0.66
            tp3 = last.get("resistance") or entry * 1.06
        elif action == "SHORT":
            entry = last.get("resistance") * 0.999 if last.get("resistance") else last["close"] * 1.001
            stop_loss = entry + max(1.5 * last.get("atr14", 0.0), entry * 0.01)
            tp1 = entry - (entry - last.get("support", entry)) * 0.33
            tp2 = entry - (entry - last.get("support", entry)) * 0.66
            tp3 = last.get("support") or entry * 0.94
    except Exception:
        logger.exception("Entry/SL/TP calc failed for %s", symbol)
        return

    # rr calculations
    try:
        if action == "LONG":
            denom = (entry - stop_loss) if (entry - stop_loss) != 0 else 1e-9
            rr1 = (tp1 - entry) / denom
            rr2 = (tp2 - entry) / denom
            rr3 = (tp3 - entry) / denom
        else:
            denom = (stop_loss - entry) if (stop_loss - entry) != 0 else 1e-9
            rr1 = (entry - tp1) / denom
            rr2 = (entry - tp2) / denom
            rr3 = (entry - tp3) / denom
    except Exception:
        logger.exception("RR calculation error")
        return

    logger.info("Symbol=%s action=%s conf=%.2f rr1=%.2f votes=%s", symbol, action, confidence, rr1, votes)

    # aggression-adjusted RR minimum
    rr_min_required = max(0.5, 2.0 - 1.5 * AGGRESSION)

    allow_signal = (confidence >= CONF_THRESHOLD_MEDIUM and rr1 >= rr_min_required)
    if not allow_signal and AGGRESSION > 0.7 and random.random() < 0.02 * AGGRESSION:
        allow_signal = True
        votes.append("aggressive_override")
        logger.info("Aggressive override allowed for %s", symbol)

    if not allow_signal:
        return

    last_sig = state.get("signals", {}).get(symbol, {})
    if not can_send_signal(symbol, last_sig.get("time"), SIGNAL_COOLDOWN_MIN):
        logger.debug("Signal for %s suppressed by cooldown", symbol)
        return

    msg = (
        f"⚡ TRADE SIGNAL\n"
        f"Symbol: {symbol}\n"
        f"Action: {action}\n"
        f"Entry: {entry:.6f}\n"
        f"Stop-Loss: {stop_loss:.6f}\n"
        f"Take-Profit 1: {tp1:.6f} (RR {rr1:.2f})\n"
        f"Take-Profit 2: {tp2:.6f} (RR {rr2:.2f})\n"
        f"Take-Profit 3: {tp3:.6f} (RR {rr3:.2f})\n"
        f"Confidence: {confidence:.2f}\n"
        f"Reasons: {', '.join(votes)}\n"
    )

    try:
        photo_bytes = plot_signal_candles(df, symbol, action, tp1=tp1, tp2=tp2, tp3=tp3, sl=stop_loss, entry=entry)
    except Exception:
        logger.exception("Plotting failed for %s", symbol)
        photo_bytes = None

    send_telegram(msg, photo=photo_bytes)

    state.setdefault("signals", {})[symbol] = {
        "action": action, "entry": entry, "sl": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "rr1": rr1, "rr2": rr2, "rr3": rr3, "confidence": confidence,
        "time": datetime.now(timezone.utc).isoformat(), "last_price": float(last["close"]), "votes": votes
    }
    save_json_safe(STATE_FILE, state)

# ---------------- MASTER SCAN ----------------
last_scan_ts = 0
scan_lock = Lock()

def scan_all_symbols():
    global last_scan_ts
    with scan_lock:
        if time.time() - last_scan_ts < SCAN_COOLDOWN_SEC:
            logger.info("Scan skipped (cooldown active)")
            return
        last_scan_ts = time.time()

    symbols = fetch_top_symbols(limit=300)
    if not symbols:
        logger.warning("No symbols fetched, falling back to ALL_USDT")
        symbols = ALL_USDT
    logger.info("Starting scan for %d symbols (workers=%d, aggression=%.2f)", len(symbols), PARALLEL_WORKERS, AGGRESSION)

    def safe_analyze(sym):
        try:
            analyze_and_alert(sym)
        except Exception:
            logger.exception("Error analyzing symbol %s", sym)

    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
        list(exe.map(safe_analyze, symbols))

    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_json_safe(STATE_FILE, state)
    logger.info("Scan finished. Signals found: %d", len(state.get("signals", {})))

# ---------------- FLASK ----------------
from flask import Flask, request, jsonify
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
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
    elif text.startswith("/state"):
        sigs = state.get("signals", {})
        top = sorted(sigs.items(), key=lambda x: x[1].get("confidence", 0), reverse=True)[:5]
        summary = "\n".join([f"{k}: {v.get('action')} conf={v.get('confidence'):.2f}" for k, v in top])
        send_telegram(f"Top signals:\n{summary}")
    return jsonify({"ok": True})

# ---------------- MAIN ----------------
if __name__ == "__main__":
    logger.info("Starting pre-top detector bot (Pro). AGGRESSION=%.2f REST_LIMIT=%d", AGGRESSION, REST_RATE_LIMIT_PER_MIN)
    # initial background scan
    Thread(target=scan_all_symbols, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)