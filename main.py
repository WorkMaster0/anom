#!/usr/bin/env python3
# insane_trade_bot_full.py

import os, time, io, logging, threading, requests, json
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import ta
from binance.client import Client
from PIL import Image
from telegram import Bot

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("insane-bot")

# ---------------- CONFIG ----------------
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
PLOT_CANDLES = int(os.getenv("PLOT_CANDLES", "300"))
CONF_THRESHOLD = float(os.getenv("CONF_THRESHOLD", "0.5"))

binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
telegram_bot = Bot(token=TELEGRAM_TOKEN)

# ---------------- UTILS ----------------
def send_telegram(msg: str, photo_bytes=None):
    if photo_bytes:
        try:
            telegram_bot.send_photo(chat_id=CHAT_ID, photo=photo_bytes, caption=msg)
        except Exception as e:
            logger.exception("Telegram photo send failed: %s", e)
    else:
        try:
            telegram_bot.send_message(chat_id=CHAT_ID, text=msg)
        except Exception as e:
            logger.exception("Telegram text send failed: %s", e)

def fetch_klines(symbol, interval="3m", limit=1000):
    try:
        data = binance_client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"
        ])
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        return df
    except Exception as e:
        logger.exception("fetch_klines failed for %s: %s", symbol, e)
        return None

# ---------------- ANALYST CLASSES ----------------
class AnalystBase:
    """Базовий аналітик"""
    def analyze(self, df: pd.DataFrame):
        return {"bias":"NEUTRAL","confidence":0.0,"reason":"Base"}

class TrendAnalyst(AnalystBase):
    """EMA + ADX"""
    def analyze(self, df):
        last = df.iloc[-1]
        if last["ema20"] > last["ema50"] and last["adx"] > 25:
            return {"bias":"LONG","confidence":0.8,"reason":"EMA20>EMA50 + ADX strong"}
        elif last["ema20"] < last["ema50"] and last["adx"] > 25:
            return {"bias":"SHORT","confidence":0.8,"reason":"EMA20<EMA50 + ADX strong"}
        return {"bias":"NEUTRAL","confidence":0.0,"reason":"Trend flat"}

class MomentumAnalyst(AnalystBase):
    """RSI"""
    def analyze(self, df):
        last = df.iloc[-1]
        if last["rsi"] < 30:
            return {"bias":"LONG","confidence":0.7,"reason":"RSI oversold"}
        elif last["rsi"] > 70:
            return {"bias":"SHORT","confidence":0.7,"reason":"RSI overbought"}
        return {"bias":"NEUTRAL","confidence":0.0,"reason":"Momentum neutral"}

class VolumeAnalyst(AnalystBase):
    """Volume spike"""
    def analyze(self, df):
        last = df.iloc[-1]
        vol_avg = df["volume"].rolling(5).mean().iloc[-1]
        if last["volume"] > 1.5*vol_avg:
            return {"bias":"LONG","confidence":0.6,"reason":"Volume spike"}
        return {"bias":"NEUTRAL","confidence":0.0,"reason":"Volume normal"}

class FundingAnalyst(AnalystBase):
    """Funding rate bias"""
    def analyze(self, df, funding_rate=0.0):
        if funding_rate > 0.01:
            return {"bias":"LONG","confidence":0.5,"reason":"Positive funding bias"}
        elif funding_rate < -0.01:
            return {"bias":"SHORT","confidence":0.5,"reason":"Negative funding bias"}
        return {"bias":"NEUTRAL","confidence":0.0,"reason":"Funding neutral"}

# ---------------- FEATURE ENGINEERING ----------------
def compute_features(df):
    df["ema20"] = ta.trend.ema_indicator(df["close"], 20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], 50)
    df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], 14)
    df["rsi"] = ta.momentum.rsi(df["close"], 14)
    df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14)
    return df

# ---------------- ENSEMBLE ----------------
def ensemble(analyses):
    long_conf = sum(a["confidence"] for a in analyses if a["bias"]=="LONG")
    short_conf = sum(a["confidence"] for a in analyses if a["bias"]=="SHORT")
    if long_conf>short_conf and long_conf>CONF_THRESHOLD:
        return "LONG", long_conf, analyses
    elif short_conf>long_conf and short_conf>CONF_THRESHOLD:
        return "SHORT", short_conf, analyses
    return "WATCH", 0.0, analyses

# ---------------- PLOT ----------------
def plot_chart(df, action):
    last = df.iloc[-1]
    entry = last["close"]
    atr = last["atr"] if "atr" in df else 1
    if action=="LONG":
        sl = entry-1.5*atr; tp = entry+3*atr
    elif action=="SHORT":
        sl = entry+1.5*atr; tp = entry-3*atr
    else:
        sl=tp=entry

    fig, ax = plt.subplots(figsize=(12,6))
    ax.plot(df.index, df["close"], color="black", lw=1.2)
    ax.axhline(entry,color="gold",ls="--",lw=1.2,label="Entry")
    ax.axhline(sl,color="red",ls="--",lw=1,label="SL")
    ax.axhline(tp,color="blue",ls="--",lw=1,label="TP")
    ax.set_title(f"Action: {action} | Entry: {entry:.2f}")
    ax.legend()
    plt.xticks(rotation=45)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close(fig)
    return buf

# ---------------- ANALYZE & SEND ----------------
def analyze_symbol(symbol):
    df = fetch_klines(symbol)
    if df is None or len(df)<50:
        return
    df = compute_features(df)
    try:
        fr = float(binance_client.futures_funding_rate(symbol=symbol, limit=1)[0]["fundingRate"])
    except: fr=0.0

    analysts = [TrendAnalyst(), MomentumAnalyst(), VolumeAnalyst(), FundingAnalyst()]
    analyses=[]
    for a in analysts:
        if isinstance(a, FundingAnalyst):
            analyses.append(a.analyze(df, funding_rate=fr))
        else:
            analyses.append(a.analyze(df))
    decision, conf, details = ensemble(analyses)
    chart = plot_chart(df, decision)
    msg = f"💥 Symbol: {symbol}\nDecision: {decision}\nConfidence: {conf:.2f}\nAnalyses:\n"
    for d in details: msg+=f"- {d['bias']} ({d['confidence']:.2f}): {d['reason']}\n"
    send_telegram(msg, chart)
    logger.info(f"{symbol} analyzed: {decision}")

# ---------------- SCAN ----------------
def scan_symbols():
    try:
        tickers = [t["symbol"] for t in binance_client.futures_ticker() if t["symbol"].endswith("USDT")]
        top_symbols = sorted(tickers, key=lambda s: abs(float(binance_client.futures_ticker(symbol=s)[0]["priceChangePercent"])), reverse=True)[:30]
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
            list(exe.map(analyze_symbol, top_symbols))
    except Exception as e:
        logger.exception("scan_symbols failed: %s", e)

# ---------------- MAIN ----------------
if __name__=="__main__":
    while True:
        scan_symbols()
        time.sleep(3*60)