#!/usr/bin/env python3
# mega_trade_bot.py

import os
import time
import json
import logging
import re
import threading
import io
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import matplotlib.pyplot as plt
import requests
import ta
import mplfinance as mpf
import numpy as np
from binance.client import Client
from PIL import Image

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()])
logger = logging.getLogger("mega-trade-bot")

# ---------------- CONFIG (from ENV) ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "5000"))
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
STATE_FILE = "state.json"
CONF_THRESHOLD_MEDIUM = 0.01
MIN_SCORE_TO_ALERT = 0.01
PLOT_CANDLES = 300

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

binance_client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

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

def send_telegram(text: str, photo=None, tries=1):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram token / chat_id not set, skipping send.")
        return
    try:
        if photo:
            try:
                img = Image.open(io.BytesIO(photo))
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                buf.seek(0)
                files = {'photo': ('signal.png', buf, 'image/png')}
            except Exception as e:
                logger.warning("PIL failed, sending raw: %s", e)
                files = {'photo': ('signal.png', photo, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)
        if tries > 0:
            time.sleep(2)
            send_telegram(text, photo=photo, tries=tries-1)

# ---------------- FETCH / KLINES ----------------
BINANCE_REST_URL = "https://fapi.binance.com/fapi/v1/klines"

def fetch_klines_rest(symbol, interval="3m", limit=1000):
    try:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        resp = requests.get(BINANCE_REST_URL, params=params, timeout=12)
        resp.raise_for_status()
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

def fetch_top_symbols(limit=30):
    try:
        tickers = binance_client.futures_ticker()
        usdt_pairs = [t for t in tickers if t['symbol'].endswith("USDT")]
        sorted_pairs = sorted(usdt_pairs, key=lambda x: abs(float(x.get("priceChangePercent",0))), reverse=True)
        top_symbols = [d["symbol"] for d in sorted_pairs[:limit]]
        return top_symbols
    except Exception as e:
        logger.exception("Error fetching top symbols: %s", e)
        return []

# ---------------- FUNDING & OI ----------------
def fetch_funding_rate(symbol):
    try:
        fr = binance_client.futures_funding_rate(symbol=symbol, limit=1)
        return float(fr[0].get("fundingRate",0.0)) if fr else 0.0
    except: return 0.0

def fetch_open_interest(symbol):
    try:
        oi = binance_client.futures_open_interest(symbol=symbol)
        return float(oi.get("openInterest",0.0))
    except: return 0.0

# ---------------- SIGNALS & FEATURES ----------------
SIGNALS_WITH_WEIGHTS = [
    ("pre_top_wick",0.08),("pre_bottom_wick",0.08),
    ("pump_alert",0.1),("dump_alert",0.1),
    ("pv_div_long",0.05),("pv_div_short",0.05),
    ("funding_bias_long",0.06),("funding_bias_short",0.06),
    ("oi_spike_long",0.06),("oi_spike_short",0.06),
    ("ema_squeeze",0.04),("ema_breakout_long",0.08),("ema_breakout_short",0.08),
    ("pre_pump_cluster",0.05),("pre_dump_cluster",0.05)
]
SIGNAL_NAMES_ORDER = [s for s,w in SIGNALS_WITH_WEIGHTS]

# ---------------- APPLY ADVANCED FEATURES ----------------
def apply_advanced_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - df[["close","open"]].max(axis=1)
    df["lower_shadow"] = df[["close","open"]].min(axis=1) - df["low"]
    df["trend_ma"] = df["close"].rolling(20).mean()
    df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    # FUNDING / OI snapshots
    df["funding_rate"] = fetch_funding_rate("BTCUSDT")
    df["open_interest"] = fetch_open_interest("BTCUSDT")
    # 15 new features
    df["pre_top_wick"] = (df["upper_shadow"] > df["range"]*0.7) & (df["volume"] > 1.8*df["vol_ma20"])
    df["pre_bottom_wick"] = (df["lower_shadow"] > df["range"]*0.7) & (df["volume"] < 0.8*df["vol_ma20"])
    df["pump_alert"] = (df["volume"] > 2.5*df["vol_ma20"]) & (df["body"] > 0)
    df["dump_alert"] = (df["volume"] > 2.5*df["vol_ma20"]) & (df["body"] < 0)
    df["pv_div_long"] = (df["close"].diff() < 0) & (df["volume"].diff() > 0)
    df["pv_div_short"] = (df["close"].diff() > 0) & (df["volume"].diff() > 0)
    df["funding_bias_long"] = (df["funding_rate"] > 0.01) & (df["close"] > df["trend_ma"])
    df["funding_bias_short"] = (df["funding_rate"] < -0.01) & (df["close"] < df["trend_ma"])
    df["oi_spike_long"] = (df["open_interest"].diff() > df["open_interest"].rolling(20).mean()*0.5) & (df["body"] > 0)
    df["oi_spike_short"] = (df["open_interest"].diff() > df["open_interest"].rolling(20).mean()*0.5) & (df["body"] < 0)
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema_squeeze"] = (df["ema20"].rolling(10).max() - df["ema20"].rolling(10).min()) < df["atr"].rolling(10).mean()*0.3
    df["ema_breakout_long"] = (df["ema20"] > df["ema50"]) & df["ema_squeeze"]
    df["ema_breakout_short"] = (df["ema20"] < df["ema50"]) & df["ema_squeeze"]
    df["pre_pump_cluster"] = (df["close"].diff() > 0) & (df["close"].diff().shift(1) > 0) & (df["volume"] > df["vol_ma20"])
    df["pre_dump_cluster"] = (df["close"].diff() < 0) & (df["close"].diff().shift(1) < 0) & (df["volume"] > df["vol_ma20"])
    return df

# ---------------- SIGNAL SCORE ----------------
def compute_signal_score(df):
    df = df.copy()
    score = 0.0
    for name, weight in SIGNALS_WITH_WEIGHTS:
        if df[name].iloc[-1]:
            score += weight
    return score

# ---------------- PLOT ----------------
def plot_signal(df, symbol):
    df_plot = df.tail(PLOT_CANDLES)
    buf = io.BytesIO()
    mpf.plot(df_plot, type="candle", style="charles", volume=True, mav=(20,50),
             title=f"{symbol} Signal", savefig=dict(fname=buf, dpi=100))
    buf.seek(0)
    return buf.read()

# ---------------- PROCESS SYMBOL ----------------
def process_symbol(symbol):
    df = fetch_klines_rest(symbol)
    if df is None or df.empty:
        return
    df = apply_advanced_features(df)
    score = compute_signal_score(df)
    last_score = state["signals"].get(symbol, {}).get("score",0)
    if score >= MIN_SCORE_TO_ALERT and score != last_score:
        buf = plot_signal(df, symbol)
        text = f"*{symbol}*\\nScore: {score:.2f}\\nSignals: " + ", ".join([n for n in SIGNAL_NAMES_ORDER if df[n].iloc[-1]])
        send_telegram(text, photo=buf)
        state["signals"][symbol] = {"score": score, "time": str(datetime.now(timezone.utc))}
        save_json_safe(STATE_FILE, state)

# ---------------- MAIN LOOP ----------------
def main_loop():
    while True:
        top_symbols = fetch_top_symbols(limit=30)
        logger.info("Scanning top symbols: %s", top_symbols)
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
            exe.map(process_symbol, top_symbols)
        state["last_scan"] = str(datetime.now(timezone.utc))
        save_json_safe(STATE_FILE, state)
        time.sleep(180)  # scan every 3 minutes

if __name__ == "__main__":
    logger.info("Mega Trade Bot Started")
    main_loop()