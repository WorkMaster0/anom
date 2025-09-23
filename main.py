import pandas as pd
import logging
import time
from binance.client import Client
import requests
from flask import Flask
import threading

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = "8489382938:AAHeFFZPODspuEFcSQyjw8lWzYpRRSv9n3g"
TELEGRAM_CHAT_ID = "6053907025"

# Публічний клієнт без API ключів
client = Client("", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------- TELEGRAM ----------------
def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
        logger.info("Telegram message sent")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ---------------- FETCH DATA ----------------
def fetch_futures_data(symbol: str, interval="1m", limit=200):
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
        logger.error(f"Error fetching data {symbol}: {e}")
        return None

def get_funding_rate(symbol: str):
    try:
        funding = client.futures_funding_rate(symbol=symbol, limit=1)
        return float(funding[0]["fundingRate"])
    except Exception:
        return 0.0

def get_quarterly_price(symbol: str):
    try:
        quarterly_symbol = symbol.replace("USDT", "USDT_240927")
        price = float(client.futures_mark_price(symbol=quarterly_symbol)["markPrice"])
        return price
    except Exception:
        return float("nan")

# ---------------- FEATURES ----------------
def apply_futures_features(df, funding, perp_price, quarterly_price):
    df = df.copy()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["volume_spike"] = df["volume"] > 3 * df["vol_ma20"]
    df["body"] = abs(df["close"] - df["open"])
    df["whale_candle"] = (df["body"] > df["close"] * 0.01) & (df["volume"] > 3 * df["vol_ma20"])
    df["funding_extreme_long"] = funding > 0.0005
    df["funding_extreme_short"] = funding < -0.0005
    spread = (perp_price - quarterly_price) / quarterly_price if quarterly_price and quarterly_price == quarterly_price else 0
    df["spread_overheat"] = spread > 0.01
    df["spread_oversold"] = spread < -0.01
    return df

# ---------------- SIGNAL ----------------
def detect_futures_signal(df):
    last = df.iloc[-1]
    votes = []
    confidence = 0.5
    if last["volume_spike"]: votes.append("volume_spike"); confidence += 0.1
    if last["whale_candle"]: votes.append("whale_candle"); confidence += 0.1
    if last["funding_extreme_long"]: votes.append("funding_extreme_long"); confidence += 0.1
    if last["funding_extreme_short"]: votes.append("funding_extreme_short"); confidence += 0.1
    if last["spread_overheat"]: votes.append("spread_overheat"); confidence += 0.1
    if last["spread_oversold"]: votes.append("spread_oversold"); confidence += 0.1

    action = "WATCH"
    if "funding_extreme_long" in votes or "spread_overheat" in votes:
        action = "SHORT"
    elif "funding_extreme_short" in votes or "spread_oversold" in votes:
        action = "LONG"

    confidence = min(1.0, confidence)
    return action, votes, confidence, last

# ---------------- ALERT ----------------
def analyze_and_alert_futures(symbol: str):
    logger.info(f"Analyzing {symbol}...")
    df = fetch_futures_data(symbol)
    if df is None:
        logger.warning(f"No data for {symbol}")
        return

    funding = get_funding_rate(symbol)
    perp_price = df["close"].iloc[-1]
    quarterly_price = get_quarterly_price(symbol)

    df = apply_futures_features(df, funding, perp_price, quarterly_price)
    action, votes, confidence, last = detect_futures_signal(df)

    logger.info(f"{symbol} action: {action}, votes: {votes}, confidence: {confidence:.2f}")
    if action == "WATCH":
        return

    msg = (
        f"⚡ *FUTURES SIGNAL*\n"
        f"Symbol: {symbol}\n"
        f"Action: *{action}*\n"
        f"Last Price: {last['close']:.2f}\n"
        f"Funding: {funding:.4%}\n"
        f"Confidence: {confidence:.2f}\n"
        f"Patterns: {', '.join(votes)}"
    )
    send_telegram(msg)

# ---------------- LOOP ----------------
def run_futures_bot(symbols):
    logger.info("Bot started in loop...")
    while True:
        for sym in symbols:
            try:
                analyze_and_alert_futures(sym)
            except Exception as e:
                logger.error(f"Error analyzing {sym}: {e}")
        time.sleep(60)

# --- Flask ---
app = Flask(__name__)
@app.route("/")
def home():
    return "Futures bot is running!"

def start_bot():
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    run_futures_bot(symbols)

threading.Thread(target=start_bot, daemon=True).start()

# ---------------- START ----------------
if __name__ == "__main__":
    send_telegram("✅ Futures bot started (public API mode)")
    app.run(host="0.0.0.0", port=5000)