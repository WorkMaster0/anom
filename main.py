#!/usr/bin/env python3
import os
import time
import json
import logging
import re
import threading
from datetime import datetime, timezone
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
from scipy.stats import binomtest
import http.server
import socketserver
from PIL import Image

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
PLOT_CANDLES = 500  # більше свічок на графіку

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
        return
    try:
        if photo:
            try:
                img = Image.open(io.BytesIO(photo))
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                buf.seek(0)
                files = {'photo': ('signal.png', buf, 'image/png')}
            except Exception:
                files = {'photo': ('signal.png', photo, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=15)
    except requests.exceptions.ReadTimeout:
        if tries > 0:
            time.sleep(2)
            send_telegram(text, photo=photo, tries=tries-1)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

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
        sorted_pairs = sorted(usdt_pairs, key=lambda x: abs(float(x.get("priceChangePercent", 0))), reverse=True)
        return [d["symbol"] for d in sorted_pairs[:limit]]
    except Exception as e:
        logger.exception("Error fetching top symbols: %s", e)
        return []

# ---------------- FEATURE ENGINEERING ----------------
def apply_pro_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["support"] = df["low"].rolling(20).min()
    df["resistance"] = df["high"].rolling(20).max()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > 1.5 * df["vol_ma20"]
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_shadow"] = df[["close", "open"]].min(axis=1) - df["low"]
    df["liquidity_grab_long"] = (df["low"] < df["support"]) & (df["close"] > df["support"])
    df["liquidity_grab_short"] = (df["high"] > df["resistance"]) & (df["close"] < df["resistance"])
    df["trend_ma"] = df["close"].rolling(20).mean()
    df["trend_up"] = df["close"] > df["trend_ma"]
    df["trend_down"] = df["close"] < df["trend_ma"]
    df["imbalance_up"] = (df["body"] > 0) & (df["body"] > df["range"] * 0.6)
    df["imbalance_down"] = (df["body"] < 0) & (abs(df["body"]) > df["range"] * 0.6)
    df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["ema_cross_up"] = (df["ema20"] > df["ema50"]) & (df["ema20"].shift(1) <= df["ema50"].shift(1))
    df["ema_cross_down"] = (df["ema20"] < df["ema50"]) & (df["ema20"].shift(1) >= df["ema50"].shift(1))
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["rsi_long"] = df["rsi"] < 30
    df["rsi_short"] = df["rsi"] > 70
    df["power_signal_long"] = df["ema_cross_up"] & df["rsi_long"] & df["vol_spike"]
    df["power_signal_short"] = df["ema_cross_down"] & df["rsi_short"] & df["vol_spike"]
    return df

# ---------------- SIGNAL DETECTION ----------------
def detect_signal_pro(df: pd.DataFrame):
    last = df.iloc[-1]
    votes = []
    confidence = 0.5
    if last.get("power_signal_long", False):
        votes.append("power_signal_long"); return "LONG", votes, last, confidence+0.2
    if last.get("power_signal_short", False):
        votes.append("power_signal_short"); return "SHORT", votes, last, confidence+0.2
    return "WATCH", votes, last, confidence

# ---------------- QUALITY SCORE ----------------
def calculate_quality_score_pro(df, votes, confidence):
    score = confidence + len(votes) * 0.1
    return min(1.0, score)

# ---------------- LEVELS ----------------
def calculate_levels(last, action):
    entry = float(last["close"])
    atr = float(last["atr"]) if not pd.isna(last.get("atr", np.nan)) else float(last["high"] - last["low"])
    if action == "LONG":
        sl = entry - 1.5 * atr
        tp = entry + 3 * atr
    elif action == "SHORT":
        sl = entry + 1.5 * atr
        tp = entry - 3 * atr
    else:
        sl = tp = entry
    return entry, sl, tp

# ---------------- PLOT ----------------
def plot_signal_chart(df, symbol, entry, sl, tp, action):
    df_plot = df.tail(PLOT_CANDLES).copy()
    df_plot.index.name = "Date"
    addplots = [
        mpf.make_addplot(df_plot["support"], panel=0, type='line', linestyle=':', alpha=0.6),
        mpf.make_addplot(df_plot["resistance"], panel=0, type='line', linestyle=':', alpha=0.6)
    ]
    fig, axes = mpf.plot(df_plot, type='candle', style='charles', volume=True,
                         addplot=addplots, title=f"{symbol} | {action}",
                         returnfig=True, figsize=(12, 8))
    price_ax = axes[0] if isinstance(axes, (list, tuple)) else axes
    price_ax.axhline(entry, color='green' if action=="LONG" else 'red', linestyle='--')
    price_ax.axhline(tp, color='blue', linestyle='--')
    price_ax.axhline(sl, color='orange', linestyle='--')
    last_candle = df_plot.iloc[-1]
    price_ax.annotate(action, xy=(df_plot.index[-1], last_candle["close"]),
                      xytext=(df_plot.index[-1], last_candle["close"]*1.01),
                      arrowprops=dict(facecolor='yellow', shrink=0.05))
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()

# ---------------- LIVE ANALYSIS ----------------
def analyze_and_alert(symbol: str):
    df = fetch_klines_rest(symbol, interval="3m", limit=1000)
    if df is None or len(df) < 50:
        return
    df = apply_pro_features(df)
    action, votes, last, confidence = detect_signal_pro(df)
    if action == "WATCH":
        return
    score = calculate_quality_score_pro(df, votes, confidence)
    entry, sl, tp = calculate_levels(last, action)
    rr = (tp-entry)/(entry-sl) if action=="LONG" else (entry-tp)/(sl-entry)
    if confidence >= CONF_THRESHOLD_MEDIUM and score >= MIN_SCORE_TO_ALERT:
        msg = (f"⚡ TRADE SIGNAL\n"
               f"Symbol: {symbol}\nAction: {action}\nEntry: {entry:.6f}\n"
               f"TP: {tp:.6f}\nSL: {sl:.6f}\nR/R: {rr:.2f}\nScore: {score:.2f}\nPatterns: {', '.join(votes)}")
        chart = plot_signal_chart(df, symbol, entry, sl, tp, action)
        send_telegram(msg, photo=chart)

# ---------------- SCAN ----------------
def scan_all_symbols(limit=30):
    symbols = fetch_top_symbols(limit=limit)
    if not symbols: return
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
        list(exe.map(analyze_and_alert, symbols))

# ---------------- HTTP ----------------
def start_http():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args): pass
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        httpd.serve_forever()

# ---------------- MAIN ----------------
if __name__ == "__main__":
    threading.Thread(target=start_http, daemon=True).start()
    logger.info("Bot started")
    while True:
        try:
            scan_all_symbols(limit=30)
        except Exception as e:
            logger.exception("scan error: %s", e)
        time.sleep(180)