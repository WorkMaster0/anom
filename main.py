import os
import time
import json
import logging
import re
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
from scipy.stats import binomtest
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
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
STATE_FILE = "state.json"
CONF_THRESHOLD_MEDIUM = 0.3

# ---------------- BINANCE CLIENT ----------------
binance_client = Client(api_key="", api_secret="")

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
        logger.exception("send_telegram error: %s", e)

# ---------------- FETCH / KLINES ----------------
BINANCE_REST_URL = "https://fapi.binance.com/fapi/v1/klines"

def fetch_klines_rest(symbol, interval="3m", limit=2500):  # збільшено у 5 разів
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

def fetch_top_symbols(limit=30):
    try:
        tickers = binance_client.futures_ticker()
        usdt_pairs = [t for t in tickers if t['symbol'].endswith("USDT")]
        sorted_pairs = sorted(
            usdt_pairs,
            key=lambda x: abs(float(x.get("priceChangePercent", 0))),
            reverse=True
        )
        top_symbols = [d["symbol"] for d in sorted_pairs[:limit]]
        logger.info("Top %d symbols fetched: %s", limit, top_symbols[:10])
        return top_symbols
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
    df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    return df

# ---------------- SIGNAL DETECTION ----------------
def detect_signal_pro(df: pd.DataFrame):
    last = df.iloc[-1]
    confidence = 0.5
    action = "WATCH"

    if last["close"] > last["resistance"]:
        action = "LONG"
        confidence += 0.3
    elif last["close"] < last["support"]:
        action = "SHORT"
        confidence += 0.3

    confidence = max(0.0, min(1.0, confidence))
    return action, ["basic"], last, confidence

# ---------------- LEVELS ----------------
def calculate_levels(last, action):
    atr = last["atr"] if not pd.isna(last["atr"]) else (last["high"] - last["low"])
    entry = last["close"]
    if action == "LONG":
        sl = last["support"]
        tp = entry + 3.0 * atr
    else:
        sl = last["resistance"]
        tp = entry - 3.0 * atr
    return entry, sl, tp

# ---------------- QUALITY SCORE ----------------
def calculate_quality_score_pro(df, votes, confidence):
    score = confidence + len(votes) * 0.05
    if votes:
        score = score / (1 + len(votes) / 3)
    return max(0.0, min(1.0, score))

# ---------------- PLOT ----------------
def plot_signal_chart(df, symbol, entry, sl, tp, action):
    df_plot = df.tail(400).copy()
    df_plot.index.name = "Date"
    add_plots = [
        mpf.make_addplot([entry]*len(df_plot), color="green", linestyle="--"),
        mpf.make_addplot([sl]*len(df_plot), color="red", linestyle="--"),
        mpf.make_addplot([tp]*len(df_plot), color="blue", linestyle="--"),
        mpf.make_addplot(df_plot["support"], color="gray", linestyle=":"),
        mpf.make_addplot(df_plot["resistance"], color="black", linestyle=":"),
    ]
    fig, ax = mpf.plot(
        df_plot,
        type="candle",
        style="charles",
        volume=True,
        addplot=add_plots,
        title=f"{symbol} | {action}",
        returnfig=True
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()

# ---------------- LIVE ----------------
def live_loop():
    logger.info("=== LIVE STARTED ===")
    while True:
        try:
            symbols = fetch_top_symbols(limit=30)
            def process_symbol(symbol):
                df = fetch_klines_rest(symbol, "3m", limit=2500)
                if df is None: return
                df = apply_pro_features(df)
                action, votes, last, confidence = detect_signal_pro(df)
                quality = calculate_quality_score_pro(df, votes, confidence)
                if action!="WATCH" and quality>=0.6:
                    entry, sl, tp = calculate_levels(last, action)
                    chart = plot_signal_chart(df, symbol, entry, sl, tp, action)
                    msg = (
                        f"⚡ TRADE SIGNAL\n"
                        f"Symbol: {symbol}\n"
                        f"Action: {action}\n"
                        f"Entry (Market): {entry:.6f}\n"
                        f"Take-Profit: {tp:.6f}\n"
                        f"Stop-Loss: {sl:.6f}\n"
                        f"Confidence: {confidence:.2f}\n"
                        f"Quality Score: {quality:.2f}\n"
                        f"Patterns: {','.join(votes)}"
                    )
                    send_telegram(msg, photo=chart)
            with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as executor:
                executor.map(process_symbol, symbols)
            time.sleep(180)
        except Exception as e:
            logger.exception("Live loop error: %s", e)
            time.sleep(60)

# ---------------- MAIN ----------------
if __name__ == "__main__":
    logger.info("Starting bot in LIVE mode")
    live_loop()