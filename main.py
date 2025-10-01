#!/usr/bin/env python3
import os
import time
import threading
import logging
from io import BytesIO
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import requests
from flask import Flask, jsonify
from dotenv import load_dotenv

try:
    from binance.client import Client
except ImportError:
    Client = None

try:
    from ta.trend import EMAIndicator, MACD, ADXIndicator
    from ta.momentum import RSIIndicator
except ImportError:
    EMAIndicator = MACD = ADXIndicator = RSIIndicator = None

# ----------------------
# Load env
# ----------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()

CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "300"))
TOP_N = 50
KL_INTERVAL = "1h"
KLINES_LIMIT = 200  # для графіків

# ----------------------
# Logging
# ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("crypto-coordinator")

# ----------------------
# Binance client
# ----------------------
binance_client = None
if Client:
    try:
        binance_client = Client(BINANCE_API_KEY or None, BINANCE_API_SECRET or None)
    except Exception as e:
        logger.exception("Binance Client init failed: %s", e)

# ----------------------
# Flask app
# ----------------------
app = Flask(__name__)

# ----------------------
# Telegram helpers
# ----------------------
def send_telegram_message(text: str, img_bytes: BytesIO = None):
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured")
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    photo_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    if img_bytes:
        files = {"photo": ("chart.png", img_bytes.getvalue())}
        data = {"chat_id": CHAT_ID, "caption": text}
        requests.post(photo_url, files=files, data=data)
        return
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    requests.post(url, json=payload)

# ----------------------
# Binance helpers
# ----------------------
def get_top_symbols(limit: int = TOP_N):
    try:
        tickers = binance_client.get_ticker()
        df = pd.DataFrame(tickers)
        df["quoteVolume"] = pd.to_numeric(df["quoteVolume"], errors="coerce").fillna(0.0)
        df = df[df["symbol"].str.endswith("USDT")].sort_values("quoteVolume", ascending=False)
        return df["symbol"].tolist()[:limit]
    except Exception as e:
        logger.exception("get_top_symbols failed: %s", e)
        return ["BTCUSDT"]

def fetch_klines(symbol: str, interval: str = KL_INTERVAL, limit: int = KLINES_LIMIT):
    raw = binance_client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","quote_asset_volume","trades","tb_base","tb_quote","ignore"
    ])
    for c in ["open","high","low","close","volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    return df

# ----------------------
# Indicators
# ----------------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    n = len(df)
    if EMAIndicator and n >= 26:
        df["ema_fast"] = EMAIndicator(df["close"], 12).ema_indicator()
        df["ema_slow"] = EMAIndicator(df["close"], 26).ema_indicator()
        df["ema_cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
        df["ema_cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
    else:
        df["ema_cross_up"] = df["ema_cross_down"] = False
    if RSIIndicator and n >= 14:
        df["rsi"] = RSIIndicator(df["close"], 14).rsi()
        df["rsi_long"] = df["rsi"] < 30
        df["rsi_short"] = df["rsi"] > 70
    else:
        df["rsi_long"] = df["rsi_short"] = False
    if MACD and n >= 26:
        macd = MACD(df["close"])
        df["macd_long"] = macd.macd_diff() > 0
        df["macd_short"] = macd.macd_diff() < 0
    else:
        df["macd_long"] = df["macd_short"] = False
    if ADXIndicator and n >= 14:
        df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], 14).adx()
    else:
        df["adx"] = 0
    return df

# ----------------------
# Strategy
# ----------------------
def analyze_df(df: pd.DataFrame):
    last = df.iloc[-1]
    patterns = []
    signal = "WATCH"
    if bool(last.get("ema_cross_up")) and last.get("adx",0)>25:
        signal = "LONG"
        patterns.append("ema_cross_up,adx>25")
    elif bool(last.get("ema_cross_down")) and last.get("adx",0)>25:
        signal = "SHORT"
        patterns.append("ema_cross_down,adx>25")
    elif bool(last.get("rsi_long")) and bool(last.get("macd_long")):
        signal = "LONG"
        patterns.append("rsi<30,macd_pos")
    elif bool(last.get("rsi_short")) and bool(last.get("macd_short")):
        signal = "SHORT"
        patterns.append("rsi>70,macd_neg")
    return signal, patterns

# ----------------------
# Plot
# ----------------------
def plot_chart(df: pd.DataFrame, symbol: str, signal: str):
    plt.figure(figsize=(8,4))
    plt.plot(df["close"], label="Close")
    if signal=="LONG":
        plt.scatter(df.index[-1:], df["close"].iloc[-1:], color="green", s=80, label="LONG")
    elif signal=="SHORT":
        plt.scatter(df.index[-1:], df["close"].iloc[-1:], color="red", s=80, label="SHORT")
    plt.title(symbol)
    plt.legend()
    buf = BytesIO()
    plt.savefig(buf, format="png")
    plt.close()
    buf.seek(0)
    return buf

# ----------------------
# Analyze symbol and send Telegram
# ----------------------
def analyze_symbol(symbol: str):
    try:
        df = fetch_klines(symbol)
        df = add_indicators(df)
        signal, patterns = analyze_df(df)
        if signal in ["LONG","SHORT"]:
            text = f"🚀 {symbol} -> {signal}\nКомбінації: {', '.join(patterns)}"
            img = plot_chart(df, symbol, signal)
            send_telegram_message(text, img_bytes=img)
        logger.info("%s -> %s", symbol, signal)
    except Exception as e:
        logger.exception("Error analyzing %s: %s", symbol, e)

# ----------------------
# Background thread
# ----------------------
def periodic_analysis():
    while True:
        symbols = get_top_symbols()
        for sym in symbols:
            analyze_symbol(sym)
        time.sleep(CYCLE_SECONDS)

threading.Thread(target=periodic_analysis, daemon=True).start()

# ----------------------
# Flask routes
# ----------------------
@app.route("/")
def index():
    return jsonify({"status":"ok","message":"Crypto analyzer running"})

# ----------------------
# Run app
# ----------------------
if __name__=="__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)