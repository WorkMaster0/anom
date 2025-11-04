#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
signal_bot_webhook.py

Monolithic Telegram Signal Bot (Webhook edition)
- Flask web server for Telegram webhook
- Background scanner every 5 minutes (signals only)
- Self-ping to /health to avoid sleeping on some hosts
- Uses Binance public REST for tickers & klines (read-only)
- Commands: /smart_auto, /momentum, /reversal, /vol_spike, /auto_scan, /status
- Single-file, minimal external deps:
    pip install Flask pyTelegramBotAPI requests numpy pandas

SECURITY/USAGE:
- This bot generates *signals* only (no orders).
- Set TELEGRAM_TOKEN and WEBHOOK_TOKEN (secret path fragment) via env.
"""

import os
import time
import json
import math
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from functools import wraps

import requests
import numpy as np
import pandas as pd
from flask import Flask, request, jsonify

import telebot  # pyTelegramBotAPI

# ----------------------------
# Configuration (ENV)
# ----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
if not TELEGRAM_TOKEN:
    raise RuntimeError("Set TELEGRAM_TOKEN environment variable")

# webhook token: random secret fragment used in path to secure webhook endpoint
WEBHOOK_TOKEN = os.getenv("WEBHOOK_URL", "change_this_to_secure_token")

# bot behavior
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", str(5 * 60)))  # 5 minutes
SELF_PING_SECONDS = int(os.getenv("SELF_PING_SECONDS", str(4 * 60)))  # ping self to stay awake (4 min)
SIGNAL_COOLDOWN_MIN = int(os.getenv("SIGNAL_COOLDOWN_MIN", "30"))  # cooldown per-symbol
TOP_VOLUME_THRESHOLD = float(os.getenv("TOP_VOLUME_THRESHOLD", "5_000_000"))  # quoteVolume filter
TOP_SYMBOL_LIMIT = int(os.getenv("TOP_SYMBOL_LIMIT", "30"))
KLINES_INTERVAL = os.getenv("KLINES_INTERVAL", "1h")  # timeframe used for analysis
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "200"))
CACHE_TTL = int(os.getenv("CACHE_TTL", "30"))  # seconds for klines cache
RATE_LIMIT_WAIT = float(os.getenv("RATE_LIMIT_WAIT", "0.08"))  # small wait between REST calls

STATE_FILE = os.getenv("STATE_FILE", "signal_state.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("signal-bot")

# ----------------------------
# Flask + TeleBot init
# ----------------------------
app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, parse_mode=None)  # we'll send HTML with explicit parse_mode

# webhook route path: /telegram_webhook/<WEBHOOK_TOKEN>
WEBHOOK_ROUTE = f"/telegram_webhook/{WEBHOOK_TOKEN}"

# ----------------------------
# Simple in-memory cache & state
# ----------------------------
_klines_cache: Dict[str, Dict[str, Any]] = {}   # key -> {"ts": epoch, "df": pandas.DataFrame}
_cache_lock = threading.Lock()

_state_lock = threading.Lock()
try:
    with open(STATE_FILE, "r") as f:
        STATE = json.load(f)
except Exception:
    STATE = {"signals": {}, "last_scan": None}  # signals: symbol -> record
# record example: {"time": iso, "action":"LONG","entry":..., "confidence":0.7}

def save_state():
    try:
        with _state_lock:
            with open(STATE_FILE, "w") as f:
                json.dump(STATE, f, indent=2, default=str)
    except Exception:
        logger.exception("save_state failed")

# ----------------------------
# Helpers: Binance REST (public)
# ----------------------------
BINANCE_TICKER_24H = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines"

def safe_get_json(url, params=None, timeout=10):
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.debug("safe_get_json error: %s %s", e, url)
        return None

def fetch_tickers_24h():
    """Return list of ticker dicts (or empty list)"""
    data = safe_get_json(BINANCE_TICKER_24H)
    return data or []

def fetch_klines(symbol: str, interval: str = KLINES_INTERVAL, limit: int = KLINES_LIMIT):
    """Fetch klines with in-memory cache (pandas DataFrame)."""
    key = f"{symbol}_{interval}_{limit}"
    now = time.time()
    with _cache_lock:
        entry = _klines_cache.get(key)
        if entry and now - entry["ts"] < CACHE_TTL:
            return entry["df"].copy()
    # tiny sleep for rate control
    time.sleep(RATE_LIMIT_WAIT)
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    data = safe_get_json(BINANCE_KLINES, params=params)
    if not data:
        return None
    # columns: open_time, open, high, low, close, volume, close_time, quote_vol, trades, taker_base, taker_quote, ignore
    df = pd.DataFrame(data, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_vol","trades","taker_base","taker_quote","ignore"
    ])
    for col in ["open","high","low","close","volume","quote_vol"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    with _cache_lock:
        _klines_cache[key] = {"ts": now, "df": df}
    return df

# ----------------------------
# Feature engineering & S/R finder
# ----------------------------
def find_support_resistance(prices: np.ndarray, window: int = 20, delta: float = 0.005) -> List[float]:
    """
    Simple SR finder: local minima/maxima on rolling window.
    returns sorted levels (unique)
    """
    if len(prices) < window:
        return []
    levels = set()
    for i in range(window, len(prices)-window):
        s = prices[i-window:i+window+1]
        val = prices[i]
        if val == s.max() or val == s.min():
            levels.add(float(val))
    # compress nearby levels
    levels = sorted(levels)
    merged = []
    for lvl in levels:
        if not merged:
            merged.append(lvl)
            continue
        if abs(lvl - merged[-1]) / merged[-1] <= delta:
            # merge by avg
            merged[-1] = (merged[-1] + lvl) / 2.0
        else:
            merged.append(lvl)
    return merged

def apply_features_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().dropna()
    if df.empty:
        return df
    df["close_f"] = df["close"].astype(float)
    df["open_f"] = df["open"].astype(float)
    df["high_f"] = df["high"].astype(float)
    df["low_f"] = df["low"].astype(float)
    df["vol_f"] = df["volume"].astype(float)
    # candle
    df["body"] = df["close_f"] - df["open_f"]
    df["range"] = df["high_f"] - df["low_f"]
    df["upper_shadow"] = df["high_f"] - df[["close_f", "open_f"]].max(axis=1)
    df["lower_shadow"] = df[["close_f", "open_f"]].min(axis=1) - df["low_f"]
    # sma/ema/rsi/macd
    try:
        df["ema9"] = df["close_f"].ewm(span=9, adjust=False).mean()
        df["ema21"] = df["close_f"].ewm(span=21, adjust=False).mean()
        # RSI
        delta = df["close_f"].diff()
        up = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
        down = -delta.clip(upper=0).ewm(span=14, adjust=False).mean()
        rs = up / (down.replace(0, np.nan))
        df["rsi14"] = 100 - 100 / (1 + rs)
        # simple MACD
        ema12 = df["close_f"].ewm(span=12, adjust=False).mean()
        ema26 = df["close_f"].ewm(span=26, adjust=False).mean()
        df["macd"] = ema12 - ema26
        df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
        # ATR-ish: use simple average true range
        high_low = df["high_f"] - df["low_f"]
        high_close = (df["high_f"] - df["close_f"].shift()).abs()
        low_close = (df["low_f"] - df["close_f"].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df["atr14"] = tr.rolling(14).mean()
    except Exception:
        logger.exception("apply_features_df failure")
    return df

# ----------------------------
# Signal detection functions (pure-ish)
# ----------------------------
def detect_breakout(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """
    Detect breakout relative to SR levels: returns a signal dict or None.
    Short if price breaks below support; Long if above resistance.
    """
    arr = df["close"].astype(float).values
    levels = find_support_resistance(arr, window=20, delta=0.005)
    if not levels:
        return None
    last_price = arr[-1]
    # find nearest level under and above
    lowers = [l for l in levels if l < last_price]
    uppers = [l for l in levels if l > last_price]
    nearest_res = min(uppers) if uppers else None
    nearest_sup = max(lowers) if lowers else None

    # breakout thresholds
    if nearest_res and last_price >= nearest_res * 1.01:
        # long breakout above resistance (price broke higher than a previous resistance -> trend)
        return {"type": "LONG_BREAKOUT", "level": nearest_res, "price": float(last_price)}
    if nearest_sup and last_price <= nearest_sup * 0.99:
        return {"type": "SHORT_BREAKOUT", "level": nearest_sup, "price": float(last_price)}
    return None

def detect_pretop(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """
    Detect pre-top (fast pump pattern) — heuristic:
      - price increased >8% over last 3 candles (or 1 hour depending TF)
      - last volume spike > 1.5x vol mean(20)
    """
    closes = df["close"].astype(float).values
    vols = df["volume"].astype(float).values
    if len(closes) < 5:
        return None
    impulse = (closes[-1] - closes[-4]) / max(closes[-4], 1e-9)
    vol_mean20 = vols[-20:].mean() if len(vols) >= 20 else vols.mean()
    vol_spike = vols[-1] > 1.5 * max(vol_mean20, 1e-9)
    if impulse > 0.08 and vol_spike:
        # Find nearest support below last price
        levels = find_support_resistance(closes, window=20, delta=0.005)
        nearest_support = max([l for l in levels if l < closes[-1]], default=None)
        return {"type": "PRETOP", "impulse": float(impulse), "vol_spike": bool(vol_spike), "nearest_support": nearest_support, "price": float(closes[-1])}
    return None

def detect_momentum(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    """
    Detects strong one-sided momentum (3 consecutive candles same direction)
    """
    closes = df["close"].astype(float)
    opens = df["open"].astype(float)
    if len(closes) < 4:
        return None
    last3 = [(closes.iloc[-i] - opens.iloc[-i]) for i in range(1,4)]
    if all(x > 0 for x in last3):
        return {"type": "MOMENTUM_UP", "strength": sum(last3)}
    if all(x < 0 for x in last3):
        return {"type": "MOMENTUM_DOWN", "strength": sum(abs(x) for x in last3)}
    return None

def detect_vol_spike(df: pd.DataFrame) -> Optional[Dict[str, Any]]:
    vols = df["volume"].astype(float).values
    if len(vols) < 20:
        return None
    if vols[-1] > 2.0 * vols[-20:].mean():
        return {"type": "VOL_SPIKE", "ratio": float(vols[-1] / (vols[-20:].mean() or 1))}
    return None

# ----------------------------
# Utility for formatting/sending signals (signals-only)
# ----------------------------
def safe_escape_html(text: str) -> str:
    # basic escape for HTML mode in Telegram
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def build_signal_message(symbol: str, sig: Dict[str, Any]) -> str:
    t = sig.get("type", "SIGNAL")
    if t == "LONG_BREAKOUT":
        msg = f"🚀 <b>LONG breakout</b> {symbol}\nPrice: {sig['price']:.6f}\nLevel: {sig['level']:.6f}"
    elif t == "SHORT_BREAKOUT":
        msg = f"⚡ <b>SHORT breakout</b> {symbol}\nPrice: {sig['price']:.6f}\nLevel: {sig['level']:.6f}"
    elif t == "PRETOP":
        msg = f"⚠️ <b>PRE-TOP (pump)</b> {symbol}\nPrice: {sig['price']:.6f}\nImpulse: {sig['impulse']*100:.2f}%\nNearest support: {sig.get('nearest_support')}"
    elif t == "MOMENTUM_UP":
        msg = f"🔥 <b>MOMENTUM UP</b> {symbol}\nStrength: {sig.get('strength'):.6f}"
    elif t == "MOMENTUM_DOWN":
        msg = f"🔥 <b>MOMENTUM DOWN</b> {symbol}\nStrength: {sig.get('strength'):.6f}"
    elif t == "VOL_SPIKE":
        msg = f"📣 <b>VOLUME SPIKE</b> {symbol}\nRatio: {sig.get('ratio'):.2f}x"
    else:
        msg = f"🔔 <b>SIGNAL</b> {symbol}\nDetails: {sig}"
    return msg

def record_and_send_signal(chat_id: str, symbol: str, sig: Dict[str, Any]):
    """
    Records in STATE, avoids duplicates with cooldown, and sends Telegram message (HTML).
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with _state_lock:
        last = STATE["signals"].get(symbol)
        if last:
            try:
                last_time = datetime.fromisoformat(last["time"])
                if datetime.now(timezone.utc) - last_time < timedelta(minutes=SIGNAL_COOLDOWN_MIN):
                    logger.debug("Duplicate suppressed by cooldown for %s", symbol)
                    return False
            except Exception:
                pass
        # store
        STATE["signals"][symbol] = {"time": now_iso, **sig}
        STATE["last_scan"] = now_iso
        save_state()
    # send to telegram
    msg = build_signal_message(symbol, sig)
    try:
        bot.send_message(chat_id, safe_escape_html(msg), parse_mode="HTML")
    except Exception:
        # try plain text fallback
        try:
            bot.send_message(chat_id, msg)
        except Exception:
            logger.exception("Failed to send Telegram message for %s", symbol)
    return True

# ----------------------------
# Core scanner (top-level): scan_top_symbols -> check various detectors -> emit signals
# ----------------------------
def scan_top_symbols_and_emit(chat_id: str):
    """
    This function is run periodically in background thread.
    It fetches tickers, filters top USDT by quoteVolume, and analyzes top N symbols.
    Emits signals to given chat_id (owner).
    """
    try:
        tickers = fetch_tickers_24h()
        if not tickers:
            logger.warning("No tickers fetched")
            return
        # filter USDT and minimum volume
        usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
        # convert safe
        usdt = [t for t in usdt if float(t.get("quoteVolume", 0) or 0) > TOP_VOLUME_THRESHOLD]
        # sort by absolute percent change
        usdt_sorted = sorted(usdt, key=lambda x: abs(float(x.get("priceChangePercent", 0) or 0)), reverse=True)
        top = usdt_sorted[:TOP_SYMBOL_LIMIT]
        symbols = [d["symbol"] for d in top]
        logger.info("Auto-scan analyzing %d symbols", len(symbols))

        for symbol in symbols:
            try:
                df = fetch_klines(symbol, interval=KLINES_INTERVAL, limit=KLINES_LIMIT)
                if df is None or df.empty:
                    continue
                df = apply_features_df(df)
                # run detectors in order of priority
                sig = detect_pretop(df) or detect_vol_spike(df) or detect_breakout(df) or detect_momentum(df)
                if sig:
                    # ensure we attach some metadata
                    sig_meta = sig.copy()
                    sig_meta.setdefault("detected_at", datetime.now(timezone.utc).isoformat())
                    # prefer basis of our signals: always send as "signal" (not just info)
                    record_and_send_signal(chat_id, symbol, sig_meta)
                # short rate-control between symbols (avoid too many REST calls)
                time.sleep(RATE_LIMIT_WAIT)
            except Exception:
                logger.exception("Error processing %s", symbol)
    except Exception:
        logger.exception("scan_top_symbols_and_emit failed")

# ----------------------------
# Background thread management
# ----------------------------
_bg_thread: Optional[threading.Thread] = None
_bg_stop = threading.Event()

def background_loop(chat_id: str):
    """
    Runs forever every SCAN_INTERVAL_SECONDS. Also self-pings /health to keep host awake.
    """
    logger.info("Background scanner started (interval=%ss)", SCAN_INTERVAL_SECONDS)
    last_ping = 0.0
    while not _bg_stop.is_set():
        try:
            start = time.time()
            scan_top_symbols_and_emit(chat_id)
            elapsed = time.time() - start
            # self-ping to /health to keep dyno awake (only if SELF_HOST is set)
            now = time.time()
            if now - last_ping > SELF_PING_SECONDS:
                last_ping = now
                self_host = os.getenv("SELF_HOST")  # e.g. https://your-app.onrender.com
                if self_host:
                    try:
                        requests.get(self_host.rstrip("/") + "/health", timeout=5)
                        logger.debug("Self-ping to %s", self_host)
                    except Exception:
                        logger.debug("Self-ping failed")
            # sleep remaining interval
            sleep_for = SCAN_INTERVAL_SECONDS - elapsed
            if sleep_for > 0:
                _bg_stop.wait(sleep_for)
        except Exception:
            logger.exception("background_loop exception")
            _bg_stop.wait(SCAN_INTERVAL_SECONDS)

def start_background(chat_id: str):
    global _bg_thread
    if _bg_thread and _bg_thread.is_alive():
        logger.info("Background scanner already running")
        return
    _bg_stop.clear()
    _bg_thread = threading.Thread(target=background_loop, args=(chat_id,), daemon=True)
    _bg_thread.start()

def stop_background():
    _bg_stop.set()
    if _bg_thread:
        _bg_thread.join(timeout=2)

# ----------------------------
# Telegram command handlers (via telebot style but we use webhook -> manual handling)
# ----------------------------
# For webhook we process updates from Flask route and pass to telebot
# We'll implement some direct HTTP endpoints for manual trigger too.

# Helper decorator to ensure chat is set and start background
def require_chat_id(func):
    @wraps(func)
    def wrapper(chat_id: str, *args, **kwargs):
        # start background if not running
        start_background(chat_id)
        return func(chat_id, *args, **kwargs)
    return wrapper

@require_chat_id
def cmd_smart_auto(chat_id: str, args: Optional[str] = None):
    """
    One-shot smart_auto: analyze top symbols once and send immediate signals.
    """
    try:
        tickers = fetch_tickers_24h()
        usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
        usdt = [t for t in usdt if float(t.get("quoteVolume", 0) or 0) > TOP_VOLUME_THRESHOLD]
        usdt_sorted = sorted(usdt, key=lambda x: abs(float(x.get("priceChangePercent", 0) or 0)), reverse=True)
        top = usdt_sorted[:TOP_SYMBOL_LIMIT]
        symbols = [d["symbol"] for d in top]
        sent = 0
        for symbol in symbols:
            df = fetch_klines(symbol, interval=KLINES_INTERVAL, limit=KLINES_LIMIT)
            if df is None or df.empty:
                continue
            df = apply_features_df(df)
            sig = detect_pretop(df) or detect_vol_spike(df) or detect_breakout(df) or detect_momentum(df)
            if sig:
                sig_meta = sig.copy()
                sig_meta.setdefault("detected_at", datetime.now(timezone.utc).isoformat())
                if record_and_send_signal(chat_id, symbol, sig_meta):
                    sent += 1
            time.sleep(RATE_LIMIT_WAIT)
        if sent == 0:
            bot.send_message(chat_id, "ℹ️ Жодних сигналів не знайдено.")
        else:
            bot.send_message(chat_id, f"✅ Відправлено {sent} сигналів.")
    except Exception as e:
        logger.exception("cmd_smart_auto failed")
        bot.send_message(chat_id, f"❌ Error: {e}")

@require_chat_id
def cmd_momentum(chat_id: str, args: Optional[str] = None):
    tickers = fetch_tickers_24h()
    usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
    usdt_sorted = sorted(usdt, key=lambda x: abs(float(x.get("priceChangePercent", 0) or 0)), reverse=True)
    symbols = [d["symbol"] for d in usdt_sorted[:TOP_SYMBOL_LIMIT]]
    sent = 0
    for symbol in symbols:
        df = fetch_klines(symbol, limit=KLINES_LIMIT)
        if df is None or df.empty:
            continue
        df = apply_features_df(df)
        sig = detect_momentum(df)
        if sig:
            sig_meta = sig.copy()
            sig_meta.setdefault("detected_at", datetime.now(timezone.utc).isoformat())
            if record_and_send_signal(chat_id, symbol, sig_meta):
                sent += 1
        time.sleep(RATE_LIMIT_WAIT)
    bot.send_message(chat_id, f"✅ Momentum scan done. Sent: {sent}")

@require_chat_id
def cmd_reversal(chat_id: str, args: Optional[str] = None):
    tickers = fetch_tickers_24h()
    usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
    symbols = [d["symbol"] for d in sorted(usdt, key=lambda x: abs(float(x.get("priceChangePercent", 0) or 0)), reverse=True)[:TOP_SYMBOL_LIMIT]]
    sent = 0
    for symbol in symbols:
        df = fetch_klines(symbol, limit=KLINES_LIMIT)
        if df is None or df.empty:
            continue
        df = apply_features_df(df)
        sig = detect_pretop(df)
        if sig:
            sig_meta = sig.copy()
            sig_meta.setdefault("detected_at", datetime.now(timezone.utc).isoformat())
            if record_and_send_signal(chat_id, symbol, sig_meta):
                sent += 1
        time.sleep(RATE_LIMIT_WAIT)
    bot.send_message(chat_id, f"✅ Reversal (pretop) scan done. Sent: {sent}")

@require_chat_id
def cmd_vol_spike(chat_id: str, args: Optional[str] = None):
    tickers = fetch_tickers_24h()
    usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
    symbols = [d["symbol"] for d in sorted(usdt, key=lambda x: float(x.get("quoteVolume", 0) or 0), reverse=True)[:TOP_SYMBOL_LIMIT]]
    sent = 0
    for symbol in symbols:
        df = fetch_klines(symbol, limit=KLINES_LIMIT)
        if df is None or df.empty:
            continue
        df = apply_features_df(df)
        sig = detect_vol_spike(df)
        if sig:
            sig_meta = sig.copy()
            sig_meta.setdefault("detected_at", datetime.now(timezone.utc).isoformat())
            if record_and_send_signal(chat_id, symbol, sig_meta):
                sent += 1
        time.sleep(RATE_LIMIT_WAIT)
    bot.send_message(chat_id, f"✅ Vol spike scan done. Sent: {sent}")

@require_chat_id
def cmd_auto_scan(chat_id: str, args: Optional[str] = None):
    # aggregate call: smart_auto + momentum + reversal + vol_spike
    cmd_smart_auto(chat_id, args)
    cmd_momentum(chat_id, args)
    cmd_reversal(chat_id, args)
    cmd_vol_spike(chat_id, args)
    bot.send_message(chat_id, "✅ Auto scan done.")

@require_chat_id
def cmd_status(chat_id: str, args: Optional[str] = None):
    with _state_lock:
        last_scan = STATE.get("last_scan") or "never"
        signals_count = len(STATE.get("signals", {}))
        msg = f"🛰️ Status\nLast scan: {last_scan}\nSaved signals: {signals_count}\nBackground scanner: {'running' if _bg_thread and _bg_thread.is_alive() else 'stopped'}"
    bot.send_message(chat_id, msg)

# ----------------------------
# Flask endpoints: webhook + health + manual
# ----------------------------
@app.route("/health")
def health():
    return jsonify({"status": "ok", "time": datetime.now(timezone.utc).isoformat()})

# Telegram webhook POST endpoint
@app.route(WEBHOOK_ROUTE, methods=["POST"])
def telegram_webhook():
    try:
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception:
        logger.exception("Failed to process update")
    return "", 200

# manual trigger endpoints (useful for admin)
@app.route("/admin/start", methods=["POST"])
def admin_start():
    # start background with chat_id set to env ADMIN_CHAT_ID if present
    admin_id = os.getenv("ADMIN_CHAT_ID")
    if not admin_id:
        return jsonify({"ok": False, "error": "ADMIN_CHAT_ID not set"}), 400
    start_background(admin_id)
    return jsonify({"ok": True, "msg": "background started"})

@app.route("/admin/stop", methods=["POST"])
def admin_stop():
    stop_background()
    return jsonify({"ok": True, "msg": "background stopped"})

# ----------------------------
# TeleBot message handlers (register functions)
# ----------------------------
# We'll use bot.message_handler decorator to map commands to our functions.
@bot.message_handler(commands=['smart_auto'])
def _on_smart_auto(message):
    chat_id = message.chat.id
    threading.Thread(target=cmd_smart_auto, args=(chat_id, None), daemon=True).start()

@bot.message_handler(commands=['momentum'])
def _on_momentum(message):
    chat_id = message.chat.id
    threading.Thread(target=cmd_momentum, args=(chat_id, None), daemon=True).start()

@bot.message_handler(commands=['reversal'])
def _on_reversal(message):
    chat_id = message.chat.id
    threading.Thread(target=cmd_reversal, args=(chat_id, None), daemon=True).start()

@bot.message_handler(commands=['vol_spike'])
def _on_vol_spike(message):
    chat_id = message.chat.id
    threading.Thread(target=cmd_vol_spike, args=(chat_id, None), daemon=True).start()

@bot.message_handler(commands=['auto_scan'])
def _on_auto_scan(message):
    chat_id = message.chat.id
    threading.Thread(target=cmd_auto_scan, args=(chat_id, None), daemon=True).start()

@bot.message_handler(commands=['status'])
def _on_status(message):
    chat_id = message.chat.id
    threading.Thread(target=cmd_status, args=(chat_id, None), daemon=True).start()

# simple start command to ensure background starts and gives info
@bot.message_handler(commands=['start', 'help'])
def _on_start(message):
    chat_id = message.chat.id
    # start bg
    start_background(chat_id)
    txt = (
        "SignalBot Webhook edition\n\n"
        "/smart_auto - scan for S/R breakouts & pre-tops (signals)\n"
        "/momentum - momentum signals\n"
        "/reversal - pre-top (pump) signals\n"
        "/vol_spike - volume spike signals\n"
        "/auto_scan - do all scans\n"
        "/status - status of bot\n\n"
        "Bot auto-scans every 5 minutes in background. It will send signals as they are found (avoids duplicates by cooldown)."
    )
    bot.send_message(chat_id, txt)

# ----------------------------
# Webhook registration helper (call once when deploying)
# ----------------------------
def set_webhook(url_base: str):
    """
    Sets Telegram webhook to url_base + WEBHOOK_ROUTE.
    url_base should be like https://your-domain.com
    """
    webhook_url = url_base.rstrip("/") + WEBHOOK_ROUTE
    r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook", params={"url": webhook_url})
    if r.status_code == 200:
        logger.info("Webhook set to %s", webhook_url)
        return True
    else:
        logger.error("Failed to set webhook: %s %s", r.status_code, r.text)
        return False

# ----------------------------
# Startup: if launched directly, optionally set webhook and start Flask
# ----------------------------
def run_flask(host="0.0.0.0", port=5000, set_hook: Optional[str] = None):
    if set_hook:
        ok = set_webhook(set_hook)
        if not ok:
            logger.warning("Webhook registration failed")
    # run Flask app (production should use gunicorn/uvicorn)
    app.run(host=host, port=port)

# ----------------------------
# Startup: if launched directly, start app
# ----------------------------
if __name__ == "__main__":
    SELF_HOST = os.getenv("SELF_HOST")  # e.g. https://anom-nyc9.onrender.com
    WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "change_this_to_secure_token")

    # register webhook correctly
    if SELF_HOST:
        set_webhook(SELF_HOST)
    else:
        logger.warning("SELF_HOST not set — Telegram webhook not registered!")

    # optionally start background scanner if admin chat ID known
    admin_chat = os.getenv("ADMIN_CHAT_ID")
    if admin_chat:
        start_background(admin_chat)
    else:
        logger.info("ADMIN_CHAT_ID not set — scanner will start after /start")

    # run Flask web server
    run_flask(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))