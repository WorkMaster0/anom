import pandas as pd
import numpy as np
import logging
import time
from datetime import datetime
from binance.client import Client
from binance.exceptions import BinanceAPIException
import requests

# ---------------- CONFIG ----------------
BINANCE_API_KEY = "WvWJOeWRKoARJ8ugljdIIWqThP6mfrDIQTmkdYCtduFv4oDV7kTMhouDtMFxlPY3"
BINANCE_API_SECRET = "HmNZNEpBLPAX0IdRPfAH9mAvSHfshooFTUDp9FkKaDhpvc5a6Rr6B6Y8bp6NbpI5"
TELEGRAM_TOKEN = "8489382938:AAHeFFZPODspuEFcSQyjw8lWzYpRRSv9n3g"
TELEGRAM_CHAT_ID = "6053907025"

client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- TELEGRAM ----------------
def send_telegram(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload, timeout=10)
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
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        df["time"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except BinanceAPIException as e:
        logger.error(f"Binance error fetching data {symbol}: {e}")
        return None

def get_funding_rate(symbol: str):
    try:
        funding = client.futures_funding_rate(symbol=symbol, limit=1)
        return float(funding[0]["fundingRate"])
    except Exception:
        return 0.0

def get_open_interest_change(symbol: str):
    try:
        oi_now = float(client.futures_open_interest(symbol=symbol)["openInterest"])
        oi_prev = float(client.futures_open_interest_hist(symbol=symbol, period="5m", limit=1)[0]["sumOpenInterest"])
        if oi_prev == 0:
            return 0.0
        return (oi_now - oi_prev) / oi_prev
    except Exception:
        return 0.0

def get_liquidations(symbol: str):
    try:
        liqs = client.futures_liquidation_orders(symbol=symbol, limit=20)
        long_liq = sum(float(l["price"]) * float(l["origQty"]) for l in liqs if l["side"] == "SELL")
        short_liq = sum(float(l["price"]) * float(l["origQty"]) for l in liqs if l["side"] == "BUY")
        return {"long": long_liq, "short": short_liq}
    except Exception:
        return {"long": 0, "short": 0}

def get_quarterly_price(symbol: str):
    try:
        # квартальні фʼючерси мають суфікс, наприклад BTCUSDT_240927
        quarterly_symbol = symbol.replace("USDT", "USDT_240927")  
        price = float(client.futures_mark_price(symbol=quarterly_symbol)["markPrice"])
        return price
    except Exception:
        return float("nan")

# ---------------- FEATURES ----------------
def apply_futures_features(df: pd.DataFrame, funding: float, oi_change: float,
                           liquidations: dict, perp_price: float, quarterly_price: float) -> pd.DataFrame:
    df = df.copy()

    # Volume spike
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["volume_spike"] = df["volume"] > 3 * df["vol_ma20"]

    # Whale candle
    df["body"] = abs(df["close"] - df["open"])
    df["whale_candle"] = (df["body"] > df["close"] * 0.01) & (df["volume"] > 3 * df["vol_ma20"])

    # Funding extreme
    df["funding_extreme_long"] = funding > 0.0005
    df["funding_extreme_short"] = funding < -0.0005

    # Open interest surge
    df["oi_surge"] = oi_change > 0.15

    # Liquidations
    df["liq_long"] = liquidations.get("long", 0) > 5e6
    df["liq_short"] = liquidations.get("short", 0) > 5e6

    # Spread perp vs quarterly
    spread = (perp_price - quarterly_price) / quarterly_price if quarterly_price and quarterly_price == quarterly_price else 0
    df["spread_overheat"] = spread > 0.01
    df["spread_oversold"] = spread < -0.01

    return df

# ---------------- SIGNAL DETECTION ----------------
def detect_futures_signal(df: pd.DataFrame):
    last = df.iloc[-1]
    votes = []
    confidence = 0.5

    if last["volume_spike"]: votes.append("volume_spike"); confidence += 0.1
    if last["whale_candle"]: votes.append("whale_candle"); confidence += 0.1
    if last["funding_extreme_long"]: votes.append("funding_extreme_long"); confidence += 0.1
    if last["funding_extreme_short"]: votes.append("funding_extreme_short"); confidence += 0.1
    if last["oi_surge"]: votes.append("oi_surge"); confidence += 0.1
    if last["liq_long"]: votes.append("liq_long_cluster"); confidence += 0.1
    if last["liq_short"]: votes.append("liq_short_cluster"); confidence += 0.1
    if last["spread_overheat"]: votes.append("spread_overheat"); confidence += 0.1
    if last["spread_oversold"]: votes.append("spread_oversold"); confidence += 0.1

    # Trend bias
    action = "WATCH"
    if "funding_extreme_long" in votes or "spread_overheat" in votes or "liq_long_cluster" in votes:
        action = "SHORT"
    elif "funding_extreme_short" in votes or "spread_oversold" in votes or "liq_short_cluster" in votes:
        action = "LONG"

    confidence = min(1.0, confidence)
    return action, votes, confidence, last

# ---------------- ALERT ----------------
def analyze_and_alert_futures(symbol: str):
    df = fetch_futures_data(symbol)
    if df is None: return

    funding = get_funding_rate(symbol)
    oi_change = get_open_interest_change(symbol)
    liquidations = get_liquidations(symbol)
    perp_price = df["close"].iloc[-1]
    quarterly_price = get_quarterly_price(symbol)

    df = apply_futures_features(df, funding, oi_change, liquidations, perp_price, quarterly_price)
    action, votes, confidence, last = detect_futures_signal(df)

    if action == "WATCH":
        return

    msg = (
        f"⚡ *FUTURES SIGNAL*\n"
        f"Symbol: {symbol}\n"
        f"Action: *{action}*\n"
        f"Last Price: {last['close']:.2f}\n"
        f"Funding: {funding:.4%}\n"
        f"OI Change: {oi_change:.2%}\n"
        f"Confidence: {confidence:.2f}\n"
        f"Patterns: {', '.join(votes)}"
    )
    send_telegram(msg)

# ---------------- AUTO LOOP ----------------
def run_futures_bot(symbols):
    while True:
        for sym in symbols:
            try:
                analyze_and_alert_futures(sym)
            except Exception as e:
                logger.error(f"Error analyzing futures {sym}: {e}")
        time.sleep(60)  # перевірка щохвилини

# ---------------- START ----------------
if __name__ == "__main__":
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]  # додай свої
    run_futures_bot(symbols)