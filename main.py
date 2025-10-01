#!/usr/bin/env python3
# main.py -- Web service + background periodic analyzer (Binance + Telegram)
import os
import time
import threading
import logging
from typing import List, Dict, Any
import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify
from dotenv import load_dotenv

# optional libs
try:
    from binance.client import Client
except Exception:
    Client = None

try:
    from ta.trend import EMAIndicator, MACD, ADXIndicator
    from ta.momentum import RSIIndicator
except Exception:
    EMAIndicator = MACD = ADXIndicator = RSIIndicator = None

# --------------------------
# Load env
# --------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()

CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "300"))
TOP_N = int(os.getenv("TOP_N", "10"))
KL_INTERVAL = os.getenv("KL_INTERVAL", "1h")
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "300"))

# --------------------------
# Logging
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("crypto-coordinator")

# --------------------------
# Binance client
# --------------------------
if Client:
    try:
        binance_client = Client(api_key=BINANCE_API_KEY or None, api_secret=BINANCE_API_SECRET or None)
    except Exception as e:
        logger.exception("Binance Client init failed: %s", e)
        binance_client = None
else:
    logger.error("python-binance not installed")
    binance_client = None

# --------------------------
# Flask app
# --------------------------
app = Flask(__name__)

# --------------------------
# Telegram helper
# --------------------------
def send_telegram_message(text: str) -> Dict[str, Any]:
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured")
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        j = resp.json() if resp.ok else {"ok": resp.ok, "status_code": resp.status_code, "text": resp.text}
        if not j.get("ok", False):
            logger.warning("Telegram API not OK: %s", j)
        else:
            logger.info("Telegram message sent (len=%d)", len(text))
        return j
    except Exception as e:
        logger.exception("Telegram send error: %s", e)
        return None

# --------------------------
# Binance helpers
# --------------------------
def get_top_symbols(limit: int = TOP_N) -> List[str]:
    if not binance_client:
        return ["BTCUSDT"]
    try:
        tickers = binance_client.get_ticker()
        df = pd.DataFrame(tickers)
        if "quoteVolume" in df.columns:
            df["quoteVolume"] = pd.to_numeric(df["quoteVolume"], errors="coerce").fillna(0.0)
            df = df[df["symbol"].str.endswith("USDT")]
            df = df.sort_values("quoteVolume", ascending=False).head(limit)
            return df["symbol"].tolist()
        return [t["symbol"] for t in tickers if t.get("symbol", "").endswith("USDT")][:limit]
    except Exception as e:
        logger.exception("get_top_symbols failed: %s", e)
        return ["BTCUSDT"]

def fetch_klines(symbol: str, interval: str = KL_INTERVAL, limit: int = KLINES_LIMIT) -> pd.DataFrame:
    if not binance_client:
        raise RuntimeError("Binance client not initialized")
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

# --------------------------
# Indicators
# --------------------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    n = len(df)

    # EMA
    if EMAIndicator and n >= 26:
        df["ema_fast"] = EMAIndicator(df["close"], window=12).ema_indicator()
        df["ema_slow"] = EMAIndicator(df["close"], window=26).ema_indicator()
        df["ema_cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
        df["ema_cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
    else:
        df["ema_fast"] = df["close"]
        df["ema_slow"] = df["close"]
        df["ema_cross_up"] = False
        df["ema_cross_down"] = False

    # RSI
    if RSIIndicator and n >= 14:
        df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
        df["rsi_long"] = df["rsi"] < 30
        df["rsi_short"] = df["rsi"] > 70
    else:
        df["rsi"] = np.nan
        df["rsi_long"] = df["rsi_short"] = False

    # MACD
    if MACD and n >= 26:
        macd = MACD(df["close"])
        df["macd_diff"] = macd.macd_diff()
        df["macd_long"] = df["macd_diff"] > 0
        df["macd_short"] = df["macd_diff"] < 0
    else:
        df["macd_diff"] = 0
        df["macd_long"] = df["macd_short"] = False

    # ADX
    if ADXIndicator and n >= 14:
        df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()
    else:
        df["adx"] = np.nan

    # ATR
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["close"].shift(1)).abs()
    df["tr3"] = (df["low"] - df["close"].shift(1)).abs()
    df["true_range"] = df[["tr1","tr2","tr3"]].max(axis=1)
    df["atr14"] = df["true_range"].rolling(14).mean().bfill()

    # placeholders
    df["funding_long"] = df["funding_short"] = False
    df["oi_long"] = df["oi_short"] = False

    return df

# --------------------------
# Strategy Teams
# --------------------------
class StrategyTeam:
    def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        raise NotImplementedError

class TrendTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        last = df.iloc[-1]
        if bool(last.get("ema_cross_up")) and float(last.get("adx", 0)) > 25:
            return {"signal": "LONG", "confidence": 0.75, "patterns": ["ema_cross_up", "adx>25"]}
        if bool(last.get("ema_cross_down")) and float(last.get("adx", 0)) > 25:
            return {"signal": "SHORT", "confidence": 0.75, "patterns": ["ema_cross_down", "adx>25"]}
        return {"signal": "WATCH", "confidence": 0.25, "patterns": []}

class MomentumTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        last = df.iloc[-1]
        if bool(last.get("rsi_long")) and bool(last.get("macd_long")):
            return {"signal": "LONG", "confidence": 0.6, "patterns": ["rsi<30","macd_pos"]}
        if bool(last.get("rsi_short")) and bool(last.get("macd_short")):
            return {"signal": "SHORT", "confidence": 0.6, "patterns": ["rsi>70","macd_neg"]}
        return {"signal": "WATCH", "confidence": 0.25, "patterns": []}

class PatternTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        return {"signal": "WATCH", "confidence": 0.25, "patterns": []}

class OrderflowTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        last = df.iloc[-1]
        if bool(last.get("funding_long")) and bool(last.get("oi_long")):
            return {"signal": "LONG", "confidence": 0.5, "patterns": ["funding_long","oi_long"]}
        if bool(last.get("funding_short")) and bool(last.get("oi_short")):
            return {"signal": "SHORT", "confidence": 0.5, "patterns": ["funding_short","oi_short"]}
        return {"signal": "WATCH", "confidence": 0.25, "patterns": []}

class RiskTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        last = df.iloc[-1]
        atr = float(last.get("atr14", np.nan))
        if np.isnan(atr) or atr <= 0:
            return {"signal": "PASS", "confidence": 1.0, "patterns": ["no_atr"]}
        entry = float(last["close"])
        sl = entry - 1.5*atr
        tp = entry + 3.0*atr
        rr = (tp - entry)/(entry - sl) if (entry - sl)!=0 else 1
        return {"signal": "WATCH", "confidence": min(rr/2,1.0), "patterns": ["rr_calc"]}

TEAMS = [TrendTeam(), MomentumTeam(), PatternTeam(), OrderflowTeam(), RiskTeam()]

# --------------------------
# Analysis
# --------------------------
def analyze_symbol(symbol: str) -> Dict[str, Any]:
    try:
        df = fetch_klines(symbol)
        df = add_indicators(df)
        results = [team.analyze(df) for team in TEAMS]
        # combine
        signal_counts = {"LONG":0,"SHORT":0,"WATCH":0,"PASS":0}
        for r in results:
            signal_counts[r.get("signal","WATCH")] += 1
        final_signal = max(signal_counts, key=signal_counts.get)
        # Telegram
        msg = f"<b>{symbol}</b>: {final_signal} ({signal_counts})"
        send_telegram_message(msg)
        logger.info("Analyzed %s -> %s", symbol, final_signal)
        return {"symbol": symbol, "signal": final_signal, "details": signal_counts}
    except Exception as e:
        logger.exception("analyze_symbol failed for %s", symbol)
        return {"symbol": symbol, "signal": "WATCH", "details": {}}

def periodic_analysis():
    while True:
        symbols = get_top_symbols()
        logger.info("Top %d symbols with enough data: %s", len(symbols), symbols)
        for sym in symbols:
            analyze_symbol(sym)
        logger.info("Cycle complete, sleeping %ds", CYCLE_SECONDS)
        time.sleep(CYCLE_SECONDS)

# --------------------------
# Flask routes
# --------------------------
@app.route("/")
def index():
    return jsonify({"status": "ok", "message": "Crypto analyzer running"}), 200

# --------------------------
# Start background thread
# --------------------------
threading.Thread(target=periodic_analysis, daemon=True).start()
logger.info("Background periodic analysis thread started (cycle %ds, top=%d)", CYCLE_SECONDS, TOP_N)

# --------------------------
# Run app
# --------------------------
if __name__ == "__main__":
    import os
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port)