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
TOP_N = int(os.getenv("TOP_N", "50"))  # Тепер топ-50 символів
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
if Client is None:
    logger.error("python-binance not installed")
    binance_client = None
else:
    try:
        binance_client = Client(api_key=BINANCE_API_KEY or None, api_secret=BINANCE_API_SECRET or None)
    except Exception as e:
        logger.exception("Binance client init failed: %s", e)
        binance_client = None

# --------------------------
# Flask
# --------------------------
app = Flask(__name__)

# --------------------------
# Telegram helper
# --------------------------
def send_telegram_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if not resp.ok:
            logger.warning("Telegram send failed: %s %s", resp.status_code, resp.text)
        else:
            logger.info("Telegram message sent (len=%d)", len(text))
    except Exception as e:
        logger.exception("Telegram send exception: %s", e)

# --------------------------
# Binance helpers
# --------------------------
def get_top_symbols(limit: int = TOP_N) -> List[str]:
    if not binance_client:
        return ["BTCUSDT"]
    try:
        tickers = binance_client.get_ticker()
        df = pd.DataFrame(tickers)
        df["quoteVolume"] = pd.to_numeric(df.get("quoteVolume", 0), errors="coerce").fillna(0.0)
        df = df[df["symbol"].str.endswith("USDT")]
        df = df.sort_values("quoteVolume", ascending=False).head(limit)
        return df["symbol"].tolist()
    except Exception as e:
        logger.exception("get_top_symbols failed: %s", e)
        return ["BTCUSDT"]

def fetch_klines(symbol: str, interval: str = KL_INTERVAL, limit: int = KLINES_LIMIT) -> pd.DataFrame:
    if not binance_client:
        raise RuntimeError("Binance client missing")
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
    if len(df) < 14:
        logger.warning("Not enough data to compute indicators (need 14, got %d)", len(df))
        return df
    if EMAIndicator:
        df["ema_fast"] = EMAIndicator(df["close"], window=12).ema_indicator()
        df["ema_slow"] = EMAIndicator(df["close"], window=26).ema_indicator()
        df["ema_cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
        df["ema_cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
    if RSIIndicator:
        df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
        df["rsi_long"] = df["rsi"] < 30
        df["rsi_short"] = df["rsi"] > 70
    if MACD:
        macd = MACD(df["close"])
        df["macd_diff"] = macd.macd_diff()
        df["macd_long"] = df["macd_diff"] > 0
        df["macd_short"] = df["macd_diff"] < 0
    if ADXIndicator and len(df) >= 14:
        df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()
    # ATR
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["close"].shift(1)).abs()
    df["tr3"] = (df["low"] - df["close"].shift(1)).abs()
    df["true_range"] = df[["tr1","tr2","tr3"]].max(axis=1)
    df["atr14"] = df["true_range"].rolling(14).mean().bfill()
    # placeholders
    df["funding_long"] = False
    df["funding_short"] = False
    df["oi_long"] = False
    df["oi_short"] = False
    return df

# --------------------------
# Strategy Teams
# --------------------------
class StrategyTeam:
    def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        raise NotImplementedError

class TrendTeam(StrategyTeam):
    def analyze(self, df):
        if len(df) < 1: return {"signal":"WATCH","patterns":[]}
        last = df.iloc[-1]
        if bool(last.get("ema_cross_up")) and float(last.get("adx", 0)) > 25:
            return {"signal": "LONG","patterns": ["ema_cross_up","adx>25"]}
        if bool(last.get("ema_cross_down")) and float(last.get("adx", 0)) > 25:
            return {"signal": "SHORT","patterns": ["ema_cross_down","adx>25"]}
        return {"signal":"WATCH","patterns":[]}

class MomentumTeam(StrategyTeam):
    def analyze(self, df):
        if len(df) < 1: return {"signal":"WATCH","patterns":[]}
        last = df.iloc[-1]
        if bool(last.get("rsi_long")) and bool(last.get("macd_long")):
            return {"signal":"LONG","patterns":["rsi<30","macd_pos"]}
        if bool(last.get("rsi_short")) and bool(last.get("macd_short")):
            return {"signal":"SHORT","patterns":["rsi>70","macd_neg"]}
        return {"signal":"WATCH","patterns":[]}

class PatternTeam(StrategyTeam):
    def analyze(self, df):
        return {"signal":"WATCH","patterns":[]}

class OrderflowTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        if bool(last.get("funding_long")) and bool(last.get("oi_long")):
            return {"signal":"LONG","patterns":["funding_long","oi_long"]}
        if bool(last.get("funding_short")) and bool(last.get("oi_short")):
            return {"signal":"SHORT","patterns":["funding_short","oi_short"]}
        return {"signal":"WATCH","patterns":[]}

class RiskTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        atr = float(last.get("atr14", np.nan))
        if np.isnan(atr) or atr <= 0:
            return {"signal":"PASS","patterns":["no_atr"]}
        entry = float(last["close"])
        sl = entry - 1.5*atr
        tp = entry + 3*atr
        rr = (tp-entry)/(entry-sl) if entry-sl !=0 else 0
        return {"signal":"PASS" if rr>=3 else "WATCH","patterns":[f"rr={rr:.2f}"], "rr":rr}

# --------------------------
# Coordinator
# --------------------------
class Coordinator:
    def __init__(self, teams: List[StrategyTeam]):
        self.teams = teams
    def decide(self, df):
        results = []
        for t in self.teams:
            try:
                r = t.analyze(df)
            except Exception:
                r = {"signal":"WATCH","patterns":["error"]}
            results.append(r)
        votes = [r for r in results if r["signal"] not in ("WATCH","PASS")]
        if not votes: return {"final_signal":"WATCH","votes":results}
        long_votes = [v for v in votes if v["signal"]=="LONG"]
        short_votes = [v for v in votes if v["signal"]=="SHORT"]
        if len(long_votes)>=2 and len(long_votes)>len(short_votes):
            return {"final_signal":"LONG","votes":results}
        if len(short_votes)>=2 and len(short_votes)>len(long_votes):
            return {"final_signal":"SHORT","votes":results}
        return {"final_signal":"WATCH","votes":results}

# --------------------------
# Analyze symbol
# --------------------------
def analyze_symbol(symbol: str) -> Dict[str, Any]:
    try:
        df = fetch_klines(symbol)
        if len(df) < 14:
            logger.warning("Not enough data for %s", symbol)
            return {"symbol":symbol,"final_signal":"WATCH"}
        df = add_indicators(df)
    except Exception as e:
        logger.exception("analyze_symbol failed for %s", symbol)
        return {"symbol":symbol,"final_signal":"WATCH","error":str(e)}
    teams = [TrendTeam(),MomentumTeam(),PatternTeam(),OrderflowTeam(),RiskTeam()]
    coord = Coordinator(teams)
    decision = coord.decide(df)
    decision["symbol"] = symbol
    decision["last_price"] = float(df["close"].iloc[-1])
    return decision

# --------------------------
# Background periodic analysis (масштабний)
# --------------------------
def run_periodic_analysis():
    while True:
        symbols = get_top_symbols(TOP_N)
        alerts = []
        for s in symbols:
            d = analyze_symbol(s)
            if d.get("final_signal") in ("LONG","SHORT"):
                alerts.append(d)
            logger.info("Analyzed %s -> %s", s, d.get("final_signal"))
            time.sleep(1.2)
        if alerts:
            txt = f"⚡ <b>Signals summary ({len(alerts)})</b>\n"
            for a in alerts:
                txt += f"{a['symbol']}: {a['final_signal']} price {a.get('last_price')}\n"
            send_telegram_message(txt)
        else:
            logger.info("No alerts this cycle (%d symbols)", len(symbols))
        time.sleep(CYCLE_SECONDS)

threading.Thread(target=run_periodic_analysis, daemon=True, name="bg-analyzer").start()
logger.info("Background thread launched")

# --------------------------
# Flask endpoints
# --------------------------
@app.route("/")
def home(): return "✅ Crypto Coordinator running"

@app.route("/analyze")
def manual_analyze():
    symbols = get_top_symbols(TOP_N)
    results = {}
    alerts = []
    for s in symbols:
        d = analyze_symbol(s)
        results[s] = d
        if d.get("final_signal") in ("LONG","SHORT"):
            alerts.append(d)
    if alerts:
        txt = f"🔔 Manual run ({len(alerts)} alerts)\n"
        for a in alerts:
            txt += f"{a['symbol']}: {a['final_signal']} price {a.get('last_price')}\n"
        send_telegram_message(txt)
    return jsonify(results)