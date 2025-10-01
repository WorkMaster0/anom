#!/usr/bin/env python3
# main.py -- Crypto Signals Bot (Top50, multi-indicators, plots)

import os
import time
import logging
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import io
import requests
from flask import Flask, jsonify, send_file
from dotenv import load_dotenv

try:
    from binance.client import Client
except:
    Client = None

try:
    from ta.trend import EMAIndicator, MACD, ADXIndicator, SMAIndicator
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.volatility import BollingerBands
    from ta.volume import OnBalanceVolumeIndicator, VolumeWeightedAveragePrice
except:
    EMAIndicator = MACD = ADXIndicator = SMAIndicator = None

load_dotenv()

# -------------------
# Config
# -------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()

KL_INTERVAL = "1h"
KLINES_LIMIT = 300
TOP_N = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("crypto-bot")

# -------------------
# Binance client
# -------------------
binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET) if Client else None

# -------------------
# Flask
# -------------------
app = Flask(__name__)

# -------------------
# Telegram helper
# -------------------
def send_telegram_message(text, chart_bytes=None):
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        resp = requests.post(url, json=payload)
        logger.info("Telegram message sent: %s", text[:50])
    except Exception as e:
        logger.exception("Telegram send error: %s", e)

    if chart_bytes:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        files = {"photo": ("chart.png", chart_bytes)}
        data = {"chat_id": CHAT_ID}
        try:
            resp = requests.post(url, files=files, data=data)
            logger.info("Telegram chart sent")
        except Exception as e:
            logger.exception("Telegram chart error: %s", e)

# -------------------
# Fetch top symbols
# -------------------
def get_top_symbols(limit=TOP_N):
    tickers = binance_client.get_ticker()  # all symbols
    df = pd.DataFrame(tickers)
    df["quoteVolume"] = pd.to_numeric(df["quoteVolume"], errors="coerce").fillna(0.0)
    df = df[df["symbol"].str.endswith("USDT")]
    df = df.sort_values("quoteVolume", ascending=False)
    syms = df["symbol"].tolist()
    return syms[:limit]

# -------------------
# Fetch klines
# -------------------
def fetch_klines(symbol, interval=KL_INTERVAL, limit=KLINES_LIMIT):
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

# -------------------
# Indicators
# -------------------
def add_indicators(df):
    df = df.copy()
    # EMA/SMA
    df["ema12"] = EMAIndicator(df["close"], 12).ema_indicator()
    df["ema26"] = EMAIndicator(df["close"], 26).ema_indicator()
    df["sma50"] = SMAIndicator(df["close"], 50).sma_indicator()
    df["sma200"] = SMAIndicator(df["close"], 200).sma_indicator()

    # RSI + Stoch
    df["rsi"] = RSIIndicator(df["close"], 14).rsi()
    df["stoch"] = StochasticOscillator(df["high"], df["low"], df["close"], 14, 3).stoch()

    # MACD
    macd = MACD(df["close"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_diff"] = macd.macd_diff()

    # ADX
    if len(df) >= 14:
        df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], 14).adx()
    else:
        df["adx"] = 0

    # Bollinger
    bb = BollingerBands(df["close"], 20, 2)
    df["bb_h"] = bb.bollinger_hband()
    df["bb_l"] = bb.bollinger_lband()

    # ATR
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["close"].shift(1)).abs()
    df["tr3"] = (df["low"] - df["close"].shift(1)).abs()
    df["true_range"] = df[["tr1","tr2","tr3"]].max(axis=1)
    df["atr14"] = df["true_range"].rolling(14).mean().fillna(method="bfill")

    # OBV, VWAP
    df["obv"] = OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
    df["vwap"] = VolumeWeightedAveragePrice(df["high"], df["low"], df["close"], df["volume"]).volume_weighted_average_price()

    # Volume spike
    df["vol_spike"] = df["volume"] > df["volume"].rolling(20).mean()*3

    # Pump/Drop detection (last 3 bars)
    df["pct_change_3"] = df["close"].pct_change(3)
    return df

# -------------------
# Signal logic
# -------------------
def analyze_symbol(symbol):
    try:
        df = fetch_klines(symbol)
        df = add_indicators(df)
    except Exception as e:
        logger.exception("fetch failed: %s", e)
        return {"symbol": symbol, "signal": "ERROR"}

    last = df.iloc[-1]
    signal = "WATCH"

    # LONG signal
    if last["close"] > last["ema12"] and last["rsi"] < 60 and last["macd_diff"] > 0:
        signal = "LONG"
    # SHORT signal
    if last["close"] < last["ema12"] and last["rsi"] > 40 and last["macd_diff"] < 0:
        signal = "SHORT"
    # Pump alert
    if last["pct_change_3"] > 0.15 and last["vol_spike"]:
        signal = "PUMP_ALERT"
    # Dump alert
    if last["pct_change_3"] < -0.15 and last["vol_spike"]:
        signal = "DUMP_ALERT"

    return {
        "symbol": symbol,
        "signal": signal,
        "price": float(last["close"])
    }

# -------------------
# Plot
# -------------------
def plot_symbol(df, symbol, signal):
    plt.figure(figsize=(10,5))
    plt.plot(df.index, df["close"], label="Close")
    plt.plot(df.index, df["ema12"], label="EMA12")
    plt.plot(df.index, df["ema26"], label="EMA26")
    plt.fill_between(df.index, df["bb_l"], df["bb_h"], color="grey", alpha=0.2, label="BB")
    plt.title(f"{symbol} Signal: {signal}")
    plt.legend()
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    return buf

# -------------------
# Manual analyze endpoint
# -------------------
@app.route("/analyze")
def manual_analyze():
    symbols = get_top_symbols(TOP_N)
    results = []
    for s in symbols:
        res = analyze_symbol(s)
        results.append(res)
        # fetch df for chart
        df = fetch_klines(s)
        df = add_indicators(df)
        chart_buf = plot_symbol(df, s, res["signal"])
        # send telegram if signal is not WATCH
        if res["signal"] != "WATCH":
            text = f"⚡ <b>{res['symbol']}</b>: {res['signal']} price={res['price']:.4f}"
            send_telegram_message(text, chart_bytes=chart_buf.getvalue())
    return jsonify(results)

@app.route("/")
def home():
    return "✅ Crypto Signals Bot running"

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)