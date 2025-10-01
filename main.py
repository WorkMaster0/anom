#!/usr/bin/env python3
# crypto_profi_bot.py -- Profi signals bot 3.0
import os
import time
import threading
import logging
from typing import List, Dict, Any
import numpy as np
import pandas as pd
import requests
import matplotlib.pyplot as plt
import io
from flask import Flask, jsonify, send_file
from dotenv import load_dotenv

try:
    from binance.client import Client
except:
    Client = None

try:
    from ta.trend import EMAIndicator, MACD, ADXIndicator, SMAIndicator
    from ta.momentum import RSIIndicator, StochasticOscillator, ROCIndicator
    from ta.volatility import BollingerBands
    from ta.volume import OnBalanceVolumeIndicator
except:
    EMAIndicator = MACD = ADXIndicator = RSIIndicator = StochasticOscillator = ROCIndicator = BollingerBands = OnBalanceVolumeIndicator = None

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
KL_INTERVAL = "1h"
KLINES_LIMIT = 500

# --------------------------
# Logging
# --------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("crypto-profi-bot")

# --------------------------
# Binance client
# --------------------------
binance_client = None
if Client:
    try:
        binance_client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    except Exception as e:
        logger.exception("Binance client init failed: %s", e)

# --------------------------
# Flask app
# --------------------------
app = Flask(__name__)

# --------------------------
# Telegram helper
# --------------------------
def send_telegram_message(text: str, chart_bytes: bytes = None):
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
        if chart_bytes:
            url2 = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
            files = {"photo": ("chart.png", chart_bytes)}
            data = {"chat_id": CHAT_ID}
            requests.post(url2, files=files, data=data, timeout=10)
        logger.info("Telegram message sent")
    except Exception as e:
        logger.exception("Telegram send error: %s", e)

# --------------------------
# Binance helpers
# --------------------------
def get_top_symbols(limit=TOP_N):
    if not binance_client:
        return ["BTCUSDT"]
    tickers = binance_client.get_ticker()
    df = pd.DataFrame(tickers)
    if "quoteVolume" in df.columns:
        df["quoteVolume"] = pd.to_numeric(df["quoteVolume"], errors="coerce").fillna(0)
        df = df[df["symbol"].str.endswith("USDT")]
        df = df.sort_values("quoteVolume", ascending=False).head(limit)
        return df["symbol"].tolist()
    return ["BTCUSDT"]

def fetch_klines(symbol: str, interval=KL_INTERVAL, limit=KLINES_LIMIT):
    if not binance_client:
        raise RuntimeError("Binance client not initialized")
    raw = binance_client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time","open","high","low","close","volume","close_time",
        "quote_asset_volume","trades","tb_base","tb_quote","ignore"
    ])
    for c in ["open","high","low","close","volume","quote_asset_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("open_time", inplace=True)
    return df

# --------------------------
# Indicators & features
# --------------------------
def add_indicators(df: pd.DataFrame):
    df = df.copy()
    try:
        df["ema12"] = EMAIndicator(df["close"], window=12).ema_indicator()
        df["ema26"] = EMAIndicator(df["close"], window=26).ema_indicator()
        df["macd_diff"] = MACD(df["close"]).macd_diff()
        df["rsi14"] = RSIIndicator(df["close"], window=14).rsi()
        df["stoch"] = StochasticOscillator(df["high"], df["low"], df["close"]).stoch()
        df["roc"] = ROCIndicator(df["close"]).roc()
        df["adx"] = ADXIndicator(df["high"], df["low"], df["close"]).adx()
        df["sma50"] = SMAIndicator(df["close"], window=50).sma_indicator()
        df["boll_up"] = BollingerBands(df["close"]).bollinger_hband()
        df["boll_dn"] = BollingerBands(df["close"]).bollinger_lband()
        df["obv"] = OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
        df["atr14"] = df["high"].combine(df["low"], max) - df["low"].combine(df["close"].shift(1).abs(), max)
        df["atr14"] = df["atr14"].rolling(14).mean().bfill()
    except Exception as e:
        logger.exception("Indicator calc failed: %s", e)
    return df

# --------------------------
# Strategy Teams
# --------------------------
class StrategyTeam:
    def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        raise NotImplementedError

class TrendTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        if last["ema12"] > last["ema26"] and last["adx"] > 25:
            return {"signal": "LONG", "confidence": 0.75}
        if last["ema12"] < last["ema26"] and last["adx"] > 25:
            return {"signal": "SHORT", "confidence": 0.75}
        return {"signal": "WATCH", "confidence": 0.25}

class MomentumTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        if last["rsi14"] < 30 and last["macd_diff"] > 0:
            return {"signal": "LONG", "confidence": 0.6}
        if last["rsi14"] > 70 and last["macd_diff"] < 0:
            return {"signal": "SHORT", "confidence": 0.6}
        return {"signal": "WATCH", "confidence": 0.25}

class VolumeSpikeTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        vol_mean = df["volume"].rolling(20).mean().iloc[-1]
        if last["volume"] > 2 * vol_mean:
            return {"signal": "PUMP_ALERT", "confidence": 0.9}
        return {"signal": "WATCH", "confidence": 0.25}

# --------------------------
# Coordinator
# --------------------------
class Coordinator:
    def __init__(self, teams: List[StrategyTeam]):
        self.teams = teams

    def decide(self, df):
        votes = []
        for t in self.teams:
            try:
                r = t.analyze(df)
            except:
                r = {"signal": "WATCH", "confidence": 0}
            votes.append(r)
        final = {"LONG":0, "SHORT":0, "WATCH":0, "PUMP_ALERT":0}
        for v in votes:
            final[v["signal"]] += 1
        # pick majority
        if final["LONG"] >= 2:
            return {"final_signal":"LONG", "votes":votes}
        if final["SHORT"] >= 2:
            return {"final_signal":"SHORT", "votes":votes}
        if final["PUMP_ALERT"] >= 1:
            return {"final_signal":"PUMP_ALERT", "votes":votes}
        return {"final_signal":"WATCH", "votes":votes}

# --------------------------
# Analyze per-symbol
# --------------------------
def analyze_symbol(symbol):
    try:
        df = fetch_klines(symbol)
        df = add_indicators(df)
    except Exception as e:
        logger.exception("Failed fetch/add_indicators %s: %s", symbol, e)
        return {"symbol": symbol, "error": str(e)}
    coord = Coordinator([TrendTeam(), MomentumTeam(), VolumeSpikeTeam()])
    decision = coord.decide(df)
    decision["symbol"] = symbol
    decision["last_price"] = df["close"].iloc[-1]
    return decision

# --------------------------
# Background analysis
# --------------------------
def run_periodic():
    while True:
        symbols = get_top_symbols()
        signals = []
        for s in symbols:
            d = analyze_symbol(s)
            if d["final_signal"] != "WATCH":
                signals.append(d)
            time.sleep(0.2)
        # send telegram
        if signals:
            msg = "⚡ Crypto Signals:\n"
            for s in signals:
                msg += f"{s['symbol']}: {s['final_signal']} price={s['last_price']:.4f}\n"
            # make chart for first symbol
            chart_bytes = make_chart(signals[0]["symbol"])
            send_telegram_message(msg, chart_bytes)
        time.sleep(CYCLE_SECONDS)

# --------------------------
# Chart helper
# --------------------------
def make_chart(symbol):
    df = fetch_klines(symbol)
    plt.figure(figsize=(8,4))
    plt.plot(df["close"], label="Close")
    plt.plot(EMAIndicator(df["close"], 12).ema_indicator(), label="EMA12")
    plt.plot(EMAIndicator(df["close"], 26).ema_indicator(), label="EMA26")
    plt.title(symbol)
    plt.legend()
    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    return buf.read()

# --------------------------
# Start background
# --------------------------
t = threading.Thread(target=run_periodic, daemon=True)
t.start()

# --------------------------
# Flask endpoints
# --------------------------
@app.route("/")
def home():
    return "✅ Crypto Profi Bot running"

@app.route("/analyze")
def manual():
    results = {}
    symbols = get_top_symbols(limit=10)
    for s in symbols:
        results[s] = analyze_symbol(s)
    return jsonify(results)

# --------------------------
# Run Flask if script
# --------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))