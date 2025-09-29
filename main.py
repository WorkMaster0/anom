#!/usr/bin/env python3
import os
import time
import json
import logging
import re
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import matplotlib.pyplot as plt
import requests
import ta
import mplfinance as mpf
import numpy as np
import io
from binance.client import Client
from flask import Flask, jsonify, send_file

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("trade-bot")

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "5000"))
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
STATE_FILE = "state.json"
CONF_THRESHOLD_MEDIUM = 0.3
MIN_SCORE_TO_ALERT = 0.60
PLOT_CANDLES = 500

# ---------------- BINANCE CLIENT ----------------
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
        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        logger.exception("save_json_safe error %s: %s", path, e)

state = load_json_safe(STATE_FILE, {"last_signal": None})

# ---------------- TELEGRAM ----------------
MARKDOWNV2_ESCAPE = r"_*[]()~`>#+-=|{}.!"

def escape_md_v2(text: str) -> str:
    return re.sub(f"([{re.escape(MARKDOWNV2_ESCAPE)}])", r"\\\1", str(text))

def send_telegram(text: str, photo=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        if photo:
            files = {'photo': ('signal.png', photo, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        logger.error("Telegram error: %s", e)

# ---------------- FETCH ----------------
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
        logger.error("REST fetch error %s: %s", symbol, e)
        return None

def fetch_top_symbols(limit=30):
    try:
        tickers = binance_client.futures_ticker()
        usdt_pairs = [t for t in tickers if t['symbol'].endswith("USDT")]
        sorted_pairs = sorted(usdt_pairs, key=lambda x: abs(float(x.get("priceChangePercent", 0))), reverse=True)
        return [d["symbol"] for d in sorted_pairs[:limit]]
    except Exception as e:
        logger.error("fetch_top_symbols: %s", e)
        return []

# ---------------- FEATURES ----------------
def apply_features(df):
    df = df.copy()
    df["support"] = df["low"].rolling(20).min()
    df["resistance"] = df["high"].rolling(20).max()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > 1.5 * df["vol_ma20"]
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["trend_ma"] = df["close"].rolling(20).mean()
    df["trend_up"] = df["close"] > df["trend_ma"]
    df["trend_down"] = df["close"] < df["trend_ma"]
    df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["ema_cross_up"] = (df["ema20"] > df["ema50"]) & (df["ema20"].shift(1) <= df["ema50"].shift(1))
    df["ema_cross_down"] = (df["ema20"] < df["ema50"]) & (df["ema20"].shift(1) >= df["ema50"].shift(1))
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["rsi_long"] = df["rsi"] < 30
    df["rsi_short"] = df["rsi"] > 70
    df["power_long"] = df["ema_cross_up"] & df["rsi_long"] & df["vol_spike"]
    df["power_short"] = df["ema_cross_down"] & df["rsi_short"] & df["vol_spike"]
    return df

# ---------------- SIGNALS ----------------
def detect_signal(df):
    last = df.iloc[-1]
    votes = []
    conf = 0.5
    if last.get("power_long", False):
        votes.append("power_long"); return "LONG", votes, last, conf+0.2
    if last.get("power_short", False):
        votes.append("power_short"); return "SHORT", votes, last, conf+0.2
    return "WATCH", votes, last, conf

def calculate_levels(last, action):
    entry = float(last["close"])
    atr = float(last["atr"]) if not pd.isna(last.get("atr")) else last["high"] - last["low"]
    if action == "LONG":
        sl = entry - 1.5 * atr
        tp = entry + 3 * atr
    elif action == "SHORT":
        sl = entry + 1.5 * atr
        tp = entry - 3 * atr
    else:
        sl = tp = entry
    return entry, sl, tp

def plot_chart(df, symbol, entry, sl, tp, action):
    dfp = df.tail(PLOT_CANDLES).copy()
    dfp.index.name = "Date"
    addplots = [
        mpf.make_addplot(dfp["support"], panel=0, type='line', linestyle=':', alpha=0.6),
        mpf.make_addplot(dfp["resistance"], panel=0, type='line', linestyle=':', alpha=0.6)
    ]
    fig, axes = mpf.plot(dfp, type='candle', style='charles', volume=True,
                         addplot=addplots, title=f"{symbol} | {action}",
                         returnfig=True, figsize=(12, 8))
    ax = axes[0]
    ax.axhline(entry, color='green' if action=="LONG" else 'red', linestyle='--')
    ax.axhline(tp, color='blue', linestyle='--')
    ax.axhline(sl, color='orange', linestyle='--')
    last = dfp.iloc[-1]
    ax.annotate(action, xy=(dfp.index[-1], last["close"]),
                xytext=(dfp.index[-1], last["close"]*1.01),
                arrowprops=dict(facecolor='yellow', shrink=0.05))
    buf = io.BytesIO()
    fig.savefig(buf, format='png')
    buf.seek(0)
    plt.close(fig)
    return buf

# ---------------- CORE ----------------
def analyze_symbol(symbol):
    df = fetch_klines_rest(symbol, "3m", 1000)
    if df is None or len(df) < 50:
        return
    df = apply_features(df)
    action, votes, last, conf = detect_signal(df)
    if action == "WATCH":
        return
    entry, sl, tp = calculate_levels(last, action)
    score = conf + 0.1*len(votes)
    rr = (tp-entry)/(entry-sl) if action=="LONG" else (entry-tp)/(sl-entry)
    if score >= MIN_SCORE_TO_ALERT:
        msg = (f"⚡ TRADE SIGNAL\n"
               f"Symbol: {symbol}\nAction: {action}\nEntry: {entry:.4f}\n"
               f"TP: {tp:.4f}\nSL: {sl:.4f}\nR/R: {rr:.2f}\nScore: {score:.2f}\nPatterns: {', '.join(votes)}")
        chart = plot_chart(df, symbol, entry, sl, tp, action)
        state["last_signal"] = {"symbol": symbol, "action": action, "entry": entry,
                                "tp": tp, "sl": sl, "rr": rr, "score": score,
                                "time": datetime.utcnow().isoformat()}
        save_json_safe(STATE_FILE, state)
        send_telegram(msg, photo=chart)

def scan_loop():
    while True:
        try:
            symbols = fetch_top_symbols(30)
            with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
                exe.map(analyze_symbol, symbols)
        except Exception as e:
            logger.error("scan_loop: %s", e)
        time.sleep(180)

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running ✅"

@app.route("/last_signal")
def last_signal():
    return jsonify(state.get("last_signal"))

@app.route("/chart")
def last_chart():
    sig = state.get("last_signal")
    if not sig:
        return "No signal yet", 404
    df = fetch_klines_rest(sig["symbol"], "3m", 1000)
    df = apply_features(df)
    buf = plot_chart(df, sig["symbol"], sig["entry"], sig["sl"], sig["tp"], sig["action"])
    return send_file(buf, mimetype="image/png")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    threading.Thread(target=scan_loop, daemon=True).start()
    logger.info("Bot started with Flask")
    app.run(host="0.0.0.0", port=PORT)