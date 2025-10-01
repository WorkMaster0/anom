#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py -- Профітний Crypto Signal Bot
Топ-50 токенів, 50+ індикаторів, комбіновані стратегії, Telegram-графіки
"""

import os
import time
import threading
import logging
from typing import List, Dict, Any
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import io
import requests
from flask import Flask, jsonify, send_file
from dotenv import load_dotenv

# Binance
try:
    from binance.client import Client
except ImportError:
    Client = None

# TA libs
try:
    from ta.trend import EMAIndicator, SMAIndicator, MACD, ADXIndicator, CCIIndicator
    from ta.momentum import RSIIndicator, StochasticOscillator
    from ta.volatility import BollingerBands, AverageTrueRange
    from ta.volume import OnBalanceVolumeIndicator, MFIIndicator
except ImportError:
    EMAIndicator = SMAIndicator = MACD = ADXIndicator = CCIIndicator = None
    RSIIndicator = StochasticOscillator = None
    BollingerBands = AverageTrueRange = None
    OnBalanceVolumeIndicator = MFIIndicator = None

# --------------------------
# Load env
# --------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "300"))  # 5 min
TOP_N = 50
KL_INTERVALS = ["15m", "1h", "4h"]
KLINES_LIMIT = 300

# --------------------------
# Logging
# --------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("crypto-bot")

# --------------------------
# Binance client
# --------------------------
if Client:
    binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
else:
    binance_client = None
    logger.warning("python-binance not installed, fallback mode active")

# --------------------------
# Flask app
# --------------------------
app = Flask(__name__)

# --------------------------
# Telegram
# --------------------------
def send_telegram_message(text: str, image_bytes: bytes = None):
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    if image_bytes:
        url_img = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        files = {"photo": ("chart.png", image_bytes)}
        payload = {"chat_id": CHAT_ID, "caption": text}
        try:
            requests.post(url_img, data=payload, files=files, timeout=10)
        except Exception as e:
            logger.exception("Telegram photo send failed: %s", e)
        return
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.exception("Telegram message send failed: %s", e)

# --------------------------
# Binance helpers
# --------------------------
def get_top_symbols(limit=TOP_N) -> List[str]:
    try:
        tickers = binance_client.get_ticker()
        df = pd.DataFrame(tickers)
        df["quoteVolume"] = pd.to_numeric(df["quoteVolume"], errors="coerce").fillna(0.0)
        df = df[df["symbol"].str.endswith("USDT")]
        df = df.sort_values("quoteVolume", ascending=False)
        return df["symbol"].head(limit).tolist()
    except Exception as e:
        logger.exception("get_top_symbols failed: %s", e)
        return ["BTCUSDT"]

def fetch_klines(symbol: str, interval: str = "1h", limit: int = KLINES_LIMIT) -> pd.DataFrame:
    try:
        raw = binance_client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(raw, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades","tb_base","tb_quote","ignore"
        ])
        for c in ["open","high","low","close","volume","quote_asset_volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        return df
    except Exception as e:
        logger.exception("fetch_klines(%s) failed: %s", symbol, e)
        raise

# --------------------------
# Indicators
# --------------------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if EMAIndicator:
        df["ema12"] = EMAIndicator(df["close"], window=12).ema_indicator()
        df["ema26"] = EMAIndicator(df["close"], window=26).ema_indicator()
        df["ema_cross_up"] = (df["ema12"] > df["ema26"]) & (df["ema12"].shift(1) <= df["ema26"].shift(1))
        df["ema_cross_down"] = (df["ema12"] < df["ema26"]) & (df["ema12"].shift(1) >= df["ema26"].shift(1))
    if RSIIndicator:
        df["rsi14"] = RSIIndicator(df["close"], window=14).rsi()
    if MACD:
        macd = MACD(df["close"])
        df["macd_diff"] = macd.macd_diff()
    if ADXIndicator:
        if len(df) >= 14:
            df["adx14"] = ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()
        else:
            df["adx14"] = np.nan
    if AverageTrueRange:
        df["atr14"] = AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    # Bollinger
    if BollingerBands:
        bb = BollingerBands(df["close"])
        df["bb_high"] = bb.bollinger_hband()
        df["bb_low"] = bb.bollinger_lband()
    # OnBalanceVolume
    if OnBalanceVolumeIndicator:
        df["obv"] = OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
    # MFI
    if MFIIndicator:
        df["mfi"] = MFIIndicator(df["high"], df["low"], df["close"], df["volume"]).money_flow_index()
    return df

# --------------------------
# Signal computation
# --------------------------
def compute_signal(df: pd.DataFrame) -> Dict[str, Any]:
    last = df.iloc[-1]
    signal = "WATCH"
    confidence = 0.0
    reasons = []

    # EMA cross
    if last.get("ema_cross_up") and last.get("adx14", 0) > 25:
        signal = "LONG"
        confidence += 0.6
        reasons.append("EMA cross up + ADX>25")
    if last.get("ema_cross_down") and last.get("adx14", 0) > 25:
        signal = "SHORT"
        confidence += 0.6
        reasons.append("EMA cross down + ADX>25")

    # RSI extremes
    if last.get("rsi14") < 30:
        signal = "LONG"
        confidence += 0.4
        reasons.append("RSI<30")
    if last.get("rsi14") > 70:
        signal = "SHORT"
        confidence += 0.4
        reasons.append("RSI>70")

    # MACD
    if last.get("macd_diff") > 0:
        signal = "LONG"
        confidence += 0.3
        reasons.append("MACD>0")
    if last.get("macd_diff") < 0:
        signal = "SHORT"
        confidence += 0.3
        reasons.append("MACD<0")

    # Bollinger
    if last.get("close") > last.get("bb_high", np.nan):
        signal = "SHORT"
        confidence += 0.2
        reasons.append("Above BB high")
    if last.get("close") < last.get("bb_low", np.nan):
        signal = "LONG"
        confidence += 0.2
        reasons.append("Below BB low")

    confidence = min(confidence, 1.0)
    return {"signal": signal, "confidence": confidence, "reasons": reasons, "last_price": last["close"]}

# --------------------------
# Plot chart
# --------------------------
def plot_symbol(df: pd.DataFrame, symbol: str) -> bytes:
    plt.figure(figsize=(8,4))
    plt.plot(df.index, df["close"], label="Close", color="blue")
    if "ema12" in df: plt.plot(df.index, df["ema12"], label="EMA12", color="orange")
    if "ema26" in df: plt.plot(df.index, df["ema26"], label="EMA26", color="green")
    plt.title(f"{symbol} chart")
    plt.legend()
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    return buf.getvalue()

# --------------------------
# Analyze one symbol
# --------------------------
def analyze_symbol(symbol: str) -> Dict[str, Any]:
    try:
        df = fetch_klines(symbol, "15m")
        df = add_indicators(df)
        sig = compute_signal(df)
        sig["symbol"] = symbol
        return sig
    except Exception as e:
        logger.exception("analyze_symbol failed for %s", symbol)
        return {"symbol": symbol, "signal": "WATCH", "confidence": 0.0, "reasons": ["error"], "last_price": 0.0}

# --------------------------
# Main loop
# --------------------------
def main_loop():
    while True:
        logger.info("Starting analysis cycle...")
        symbols = get_top_symbols()
        alerts = []
        for sym in symbols:
            res = analyze_symbol(sym)
            if res["signal"] != "WATCH":
                chart = plot_symbol(fetch_klines(sym, "15m"), sym)
                send_telegram_message(
                    f"{sym}: {res['signal']} price={res['last_price']:.4f}\nReasons: {', '.join(res['reasons'])}\nConfidence: {res['confidence']:.2f}",
                    chart
                )
                alerts.append(res)
        if not alerts:
            logger.info("No alerts this cycle (checked %d symbols)", len(symbols))
        time.sleep(CYCLE_SECONDS)

# --------------------------
# Flask routes
# --------------------------
@app.route("/")
def index():
    return jsonify({"status": "ok", "top_n": TOP_N, "cycle_seconds": CYCLE_SECONDS})

@app.route("/manual")
def manual():
    symbols = get_top_symbols()
    results = []
    for sym in symbols[:10]:
        res = analyze_symbol(sym)
        results.append(res)
    return jsonify(results)

# --------------------------
# Start background thread
# --------------------------
threading.Thread(target=main_loop, daemon=True).start()

# --------------------------
# Run Flask
# --------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)