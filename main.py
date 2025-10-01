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

# behavior
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "300"))
TOP_N = int(os.getenv("TOP_N", "10"))
KL_INTERVAL = os.getenv("KL_INTERVAL", "1h")
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "300"))

# --------------------------
# Logging
# --------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("crypto-coordinator")

# --------------------------
# Binance client init
# --------------------------
if Client is None:
    logger.error("python-binance not installed.")
    binance_client = None
else:
    try:
        binance_client = Client(api_key=BINANCE_API_KEY or None, api_secret=BINANCE_API_SECRET or None)
    except Exception as e:
        logger.exception("Failed to init Binance Client: %s", e)
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
        logger.warning(f"Telegram not configured. BOT_TOKEN length: {len(BOT_TOKEN)}, CHAT_ID length: {len(CHAT_ID)}")
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        try:
            j = resp.json()
        except Exception:
            j = {"ok": resp.ok, "status_code": resp.status_code, "text": resp.text}
        if not resp.ok or not j.get("ok", False):
            logger.warning("Telegram API response not OK: status=%s json=%s", resp.status_code, j)
        else:
            logger.info("Telegram message sent (len=%d)", len(text))
        return j
    except Exception as e:
        logger.exception("send_telegram_message error: %s", e)
        return None

# --------------------------
# Binance helpers
# --------------------------
def get_top_symbols(limit: int = TOP_N) -> List[str]:
    if not binance_client:
        logger.warning("No binance client, returning fallback ['BTCUSDT']")
        return ["BTCUSDT"]
    try:
        tickers = binance_client.get_ticker()
        df = pd.DataFrame(tickers)
        if "quoteVolume" in df.columns:
            df["quoteVolume"] = pd.to_numeric(df["quoteVolume"], errors="coerce").fillna(0.0)
            df = df[df["symbol"].str.endswith("USDT")]
            df = df.sort_values("quoteVolume", ascending=False).head(limit)
            syms = df["symbol"].astype(str).tolist()
            logger.info("Top %d symbols: %s", len(syms), syms[:10])
            return syms
        syms = [t["symbol"] for t in tickers if t.get("symbol","").endswith("USDT")]
        return syms[:limit] or ["BTCUSDT"]
    except Exception as e:
        logger.exception("get_top_symbols error: %s", e)
        return ["BTCUSDT"]

def fetch_klines(symbol: str, interval: str = KL_INTERVAL, limit: int = KLINES_LIMIT) -> pd.DataFrame:
    if not binance_client:
        raise RuntimeError("Binance client not initialized")
    try:
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
    except Exception as e:
        logger.exception("fetch_klines(%s) failed: %s", symbol, e)
        raise

# --------------------------
# Indicators
# --------------------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if EMAIndicator is None:
        return df
    df["ema_fast"] = EMAIndicator(df["close"], window=12).ema_indicator()
    df["ema_slow"] = EMAIndicator(df["close"], window=26).ema_indicator()
    df["ema_cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
    df["ema_cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))
    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
    df["rsi_long"] = df["rsi"] < 30
    df["rsi_short"] = df["rsi"] > 70
    macd = MACD(df["close"])
    df["macd_diff"] = macd.macd_diff()
    df["macd_long"] = df["macd_diff"] > 0
    df["macd_short"] = df["macd_diff"] < 0
    df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["close"].shift(1)).abs()
    df["tr3"] = (df["low"] - df["close"].shift(1)).abs()
    df["true_range"] = df[["tr1","tr2","tr3"]].max(axis=1)
    df["atr14"] = df["true_range"].rolling(14).mean().bfill()
    df["funding_long"] = False
    df["funding_short"] = False
    df["oi_long"] = False
    df["oi_short"] = False
    return df

# --------------------------
# Teams
# --------------------------
class StrategyTeam:
    def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        raise NotImplementedError

class TrendTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        if bool(last.get("ema_cross_up")) and float(last.get("adx", 0)) > 25:
            return {"signal": "LONG", "patterns": ["ema_cross_up", "adx>25"]}
        if bool(last.get("ema_cross_down")) and float(last.get("adx", 0)) > 25:
            return {"signal": "SHORT", "patterns": ["ema_cross_down", "adx>25"]}
        return {"signal": "WATCH", "patterns": []}

class MomentumTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        if bool(last.get("rsi_long")) and bool(last.get("macd_long")):
            return {"signal": "LONG", "patterns": ["rsi<30","macd_pos"]}
        if bool(last.get("rsi_short")) and bool(last.get("macd_short")):
            return {"signal": "SHORT", "patterns": ["rsi>70","macd_neg"]}
        return {"signal": "WATCH", "patterns": []}

class PatternTeam(StrategyTeam):
    def analyze(self, df):
        return {"signal": "WATCH", "patterns": []}

class OrderflowTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        if bool(last.get("funding_long")) and bool(last.get("oi_long")):
            return {"signal": "LONG", "patterns": ["funding_long","oi_long"]}
        if bool(last.get("funding_short")) and bool(last.get("oi_short")):
            return {"signal": "SHORT", "patterns": ["funding_short","oi_short"]}
        return {"signal": "WATCH", "patterns": []}

class RiskTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        atr = float(last.get("atr14", np.nan))
        if np.isnan(atr) or atr <= 0:
            return {"signal": "PASS", "patterns": ["no_atr"]}
        return {"signal": "PASS", "patterns": []}

# --------------------------
# Coordinator
# --------------------------
class Coordinator:
    def __init__(self, teams: List[StrategyTeam]):
        self.teams = teams

    def decide(self, df: pd.DataFrame) -> Dict[str, Any]:
        results = [t.analyze(df) for t in self.teams]
        votes = [r for r in results if r["signal"] not in ("WATCH","PASS")]
        if not votes:
            return {"final_signal": "WATCH", "votes": results}
        long_votes = [v for v in votes if v["signal"] == "LONG"]
        short_votes = [v for v in votes if v["signal"] == "SHORT"]
        if len(long_votes) >= 2 and len(long_votes) > len(short_votes):
            return {"final_signal": "LONG", "votes": results}
        if len(short_votes) >= 2 and len(short_votes) > len(long_votes):
            return {"final_signal": "SHORT", "votes": results}
        return {"final_signal": "WATCH", "votes": results}

# --------------------------
# Analyze symbol
# --------------------------
def analyze_symbol(symbol: str) -> Dict[str, Any]:
    try:
        df = fetch_klines(symbol)
        df = add_indicators(df)
    except Exception as e:
        logger.exception("analyze_symbol failed for %s: %s", symbol, e)
        return {"symbol": symbol, "error": str(e)}
    teams = [TrendTeam(), MomentumTeam(), PatternTeam(), OrderflowTeam(), RiskTeam()]
    coord = Coordinator(teams)
    decision = coord.decide(df)
    decision["symbol"] = symbol
    decision["last_price"] = float(df["close"].iloc[-1]) if len(df) else None
    return decision

# --------------------------
# Periodic runner
# --------------------------
def run_periodic_analysis():
    logger.info("Background analyzer started (cycle=%ds, top=%d)", CYCLE_SECONDS, TOP_N)
    while True:
        try:
            symbols = get_top_symbols(TOP_N)
            alerts = []
            for s in symbols:
                d = analyze_symbol(s)
                if d.get("final_signal") in ("LONG","SHORT"):
                    alerts.append(d)
                logger.info("Analyzed %s -> %s", s, d.get("final_signal"))
                time.sleep(1.2)
            if alerts:
                txt = f"⚡ <b>Signals summary — {len(alerts)} alerts</b>\n"
                for a in alerts:
                    txt += f"\n<b>{a['symbol']}</b>: {a['final_signal']} price {a.get('last_price')}\n"
                    pat_list = []
                    for v in a.get("votes", []):
                        if v.get("patterns"):
                            pat_list.extend(v.get("patterns"))
                    if pat_list:
                        txt += "patterns: " + ", ".join(pat_list) + "\n"
                send_telegram_message(txt)
        except Exception as e:
            logger.exception("Periodic analysis loop error: %s", e)
        time.sleep(CYCLE_SECONDS)

# --------------------------
# Start background thread
# --------------------------
def _start_bg_once():
    if getattr(_start_bg_once, "started", False):
        return
    t = threading.Thread(target=run_periodic_analysis, daemon=True)
    t.start()
    _start_bg_once.started = True
    logger.info("Background thread launched")
_start_bg_once()

# --------------------------
# Flask endpoints
# --------------------------
@app.route("/")
def home():
    return "✅ Crypto Coordinator running"

@app.route("/analyze")
def manual_analyze():
    try:
        symbols = get_top_symbols(min(5, TOP_N))
        results = {}
        for s in symbols:
            results[s] = analyze_symbol(s)
        alerts = [v for v in results.values() if v.get("final_signal") in ("LONG","SHORT")]
        if alerts:
            txt = f"🔔 Manual run alerts ({len(alerts)})\n"
            for a in alerts:
                txt += f"{a['symbol']}: {a['final_signal']} price={a.get('last_price')}\n"
            send_telegram_message(txt)
        return jsonify(results)
    except Exception as e:
        logger.exception("manual_analyze error: %s", e)
        return jsonify({"error": str(e)}), 500