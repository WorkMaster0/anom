import pandas as pd
import numpy as np
import logging
import time
import io
import requests
import threading
import matplotlib.pyplot as plt
import mplfinance as mpf
from datetime import datetime
from flask import Flask
from binance.client import Client

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = "8489382938:AAHeFFZPODspuEFcSQyjw8lWzYpRRSv9n3g"
TELEGRAM_CHAT_ID = "6053907025"

client = Client("", "")  # публічний клієнт, ключі не потрібні

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------- TELEGRAM ----------------
def send_telegram_text(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
        logger.info("Telegram text sent")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

def send_telegram_photo(img_buf, caption=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    files = {"photo": img_buf}
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
    try:
        requests.post(url, data=data, files=files, timeout=20)
        logger.info("Telegram photo sent")
    except Exception as e:
        logger.error(f"Telegram photo error: {e}")

# ---------------- FETCH DATA ----------------
def fetch_futures_data(symbol: str, interval="15m", limit=300):
    try:
        klines = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=[
            "timestamp", "open", "high", "low", "close", "volume",
            "close_time", "quote_asset_volume", "number_of_trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = df[col].astype(float)
        df["time"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        logger.error(f"Error fetching {symbol}: {e}")
        return None

# ---------------- PATTERNS ----------------
def detect_patterns(df):
    last = df.iloc[-1]
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    volumes = df["volume"].values
    signals = []

    # Triangle (звуження діапазону)
    if (max(highs[-20:]) - min(lows[-20:]))/last["close"] < 0.02:
        signals.append("Triangle")

    # Rectangle (флет)
    if (max(highs[-30:]) - min(lows[-30:]))/last["close"] < 0.015:
        signals.append("Rectangle")

    # Double Top
    if abs(highs[-5] - highs[-15]) / last["close"] < 0.01:
        signals.append("Double Top")

    # Double Bottom
    if abs(lows[-5] - lows[-15]) / last["close"] < 0.01:
        signals.append("Double Bottom")

    # Head & Shoulders (спрощена перевірка)
    if highs[-15] < highs[-10] and highs[-5] < highs[-10]:
        signals.append("Head & Shoulders")

    # Flag (сильний рух + консолідація)
    if abs(closes[-30] - closes[-1]) / closes[-30] > 0.05 and (max(highs[-10:]) - min(lows[-10:]))/last["close"] < 0.02:
        signals.append("Flag")

    # Volume spike
    vol_ma = pd.Series(volumes).rolling(20).mean().iloc[-1]
    if last["volume"] > 2 * vol_ma:
        signals.append("Volume Spike")

    return signals

# ---------------- PLOT ----------------
def plot_chart(df, symbol, pattern_name):
    fig, axlist = mpf.plot(
        df.tail(80),
        type="candle",
        style="charles",
        volume=True,
        returnfig=True,
        figsize=(10,6),
        title=f"{symbol} - {pattern_name}"
    )
    ax = axlist[0]
    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    last80 = df.tail(80)

    # --- Trendlines ---
    if "Triangle" in pattern_name:
        ax.plot(last80.index, np.linspace(max(highs[-80:]), min(highs[-10:]), len(last80)), "r--", lw=1.5)
        ax.plot(last80.index, np.linspace(min(lows[-80:]), max(lows[-10:]), len(last80)), "g--", lw=1.5)
    elif "Rectangle" in pattern_name:
        ax.hlines([max(highs[-30:]), min(lows[-30:])], xmin=last80.index[0], xmax=last80.index[-1], colors=["r","g"], linestyles="--")
    elif "Double Top" in pattern_name:
        ax.hlines(max(highs[-10:]), xmin=last80.index[0], xmax=last80.index[-1], colors="r", linestyles="--")
    elif "Double Bottom" in pattern_name:
        ax.hlines(min(lows[-10:]), xmin=last80.index[0], xmax=last80.index[-1], colors="g", linestyles="--")
    elif "Head & Shoulders" in pattern_name:
        neckline = (lows[-20] + lows[-10]) / 2
        ax.hlines(neckline, xmin=last80.index[0], xmax=last80.index[-1], colors="orange", linestyles="--")
    elif "Flag" in pattern_name:
        mid = np.mean(closes[-20:])
        ax.plot(last80.index, np.linspace(mid*0.98, mid*1.02, len(last80)), "b--", lw=1.2)

    # --- Volume spike ---
    vol = last80["volume"].values
    vol_ma = pd.Series(vol).rolling(20).mean().iloc[-1]
    for i, v in enumerate(vol):
        if v > 2 * vol_ma:
            ax.vlines(last80.index[i], ymin=lows[-80:].min(), ymax=highs[-80:].max(), colors="red", alpha=0.3)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf

# ---------------- ANALYZE ----------------
def analyze_symbol(symbol):
    df = fetch_futures_data(symbol)
    if df is None:
        return
    signals = detect_patterns(df)
    if not signals:
        return
    for sig in signals:
        caption = f"⚡ *FUTURES SIGNAL*\nSymbol: {symbol}\nPattern: *{sig}*\nLast Price: {df['close'].iloc[-1]:.2f}"
        chart = plot_chart(df, symbol, sig)
        send_telegram_photo(chart, caption=caption)

# ---------------- LOOP ----------------
def run_bot(symbols):
    logger.info("Bot started...")
    while True:
        for sym in symbols:
            try:
                analyze_symbol(sym)
            except Exception as e:
                logger.error(f"Error analyzing {sym}: {e}")
        time.sleep(60)

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return "Futures bot is running!"

def start_bot():
    # Отримуємо список усіх ф’ючерсних символів USDT
    try:
        info = client.futures_exchange_info()
        symbols = [s["symbol"] for s in info["symbols"] if s["quoteAsset"] == "USDT"]
    except:
        symbols = ["BTCUSDT","ETHUSDT","SOLUSDT"]
    run_bot(symbols)

threading.Thread(target=start_bot, daemon=True).start()

# ---------------- START ----------------
if __name__ == "__main__":
    send_telegram_text("✅ Futures bot started with pattern detection")
    app.run(host="0.0.0.0", port=5000)