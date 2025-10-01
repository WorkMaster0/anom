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

CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "300"))   # default 5min
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
if Client is None:
    logger.error("python-binance not installed")
    binance_client = None
else:
    try:
        binance_client = Client(api_key=BINANCE_API_KEY or None,
                                api_secret=BINANCE_API_SECRET or None)
    except Exception as e:
        logger.exception("Binance init failed: %s", e)
        binance_client = None

# --------------------------
# Flask
# --------------------------
app = Flask(__name__)

# --------------------------
# Telegram
# --------------------------
def send_telegram_message(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured")
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        j = resp.json()
        if not resp.ok or not j.get("ok", False):
            logger.warning("Telegram error: %s", j)
        else:
            logger.info("Telegram message sent")
        return j
    except Exception as e:
        logger.exception("send_telegram_message: %s", e)
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
            syms = df["symbol"].tolist()
            logger.info("Top %d symbols: %s", len(syms), syms)
            return syms
        return [t["symbol"] for t in tickers if t.get("symbol", "").endswith("USDT")][:limit]
    except Exception as e:
        logger.exception("get_top_symbols error: %s", e)
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
    if EMAIndicator is None:
        return df

    n = len(df)
    if n < 10:
        return df  # too few candles

    # EMA cross
    df["ema_fast"] = EMAIndicator(df["close"], window=12).ema_indicator()
    df["ema_slow"] = EMAIndicator(df["close"], window=26).ema_indicator()
    df["ema_cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
    df["ema_cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))

    # RSI
    if n >= 14:
        df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
        df["rsi_long"] = df["rsi"] < 35
        df["rsi_short"] = df["rsi"] > 65
    else:
        df["rsi"] = np.nan
        df["rsi_long"] = df["rsi_short"] = False

    # MACD
    if n >= 26:
        macd = MACD(df["close"])
        df["macd_diff"] = macd.macd_diff()
        df["macd_long"] = df["macd_diff"] > 0
        df["macd_short"] = df["macd_diff"] < 0
    else:
        df["macd_diff"] = np.nan
        df["macd_long"] = df["macd_short"] = False

    # ADX
    if n >= 14:
        try:
            df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()
        except Exception:
            df["adx"] = np.nan
    else:
        df["adx"] = np.nan

    # ATR
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["close"].shift(1)).abs()
    df["tr3"] = (df["low"] - df["close"].shift(1)).abs()
    df["true_range"] = df[["tr1","tr2","tr3"]].max(axis=1)
    df["atr14"] = df["true_range"].rolling(14).mean().bfill()

    return df

# --------------------------
# Teams
# --------------------------
class StrategyTeam:
    def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        raise NotImplementedError

class TrendTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        last = df.iloc[-1]
        if bool(last.get("ema_cross_up")):
            return {"signal": "LONG", "patterns": ["ema_cross_up"]}
        if bool(last.get("ema_cross_down")):
            return {"signal": "SHORT", "patterns": ["ema_cross_down"]}
        return {"signal": "WATCH", "patterns": []}

class MomentumTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        last = df.iloc[-1]
        if bool(last.get("rsi_long")) and bool(last.get("macd_long")):
            return {"signal": "LONG", "patterns": ["rsi<35","macd_pos"]}
        if bool(last.get("rsi_short")) and bool(last.get("macd_short")):
            return {"signal": "SHORT", "patterns": ["rsi>65","macd_neg"]}
        return {"signal": "WATCH", "patterns": []}

class RiskTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        last = df.iloc[-1]
        atr = float(last.get("atr14", np.nan))
        if np.isnan(atr) or atr <= 0:
            return {"signal": "PASS", "patterns": ["no_atr"]}
        entry = float(last["close"])
        sl = entry - 1.5 * atr
        tp = entry + 3.0 * atr
        rr = (tp - entry) / (entry - sl) if (entry - sl) != 0 else 0.0
        return {"signal": "PASS", "patterns": [f"rr={rr:.2f}"], "rr": rr}

# --------------------------
# Coordinator
# --------------------------
class Coordinator:
    def __init__(self, teams: List[StrategyTeam]):
        self.teams = teams

    def decide(self, df: pd.DataFrame) -> Dict[str, Any]:
        results = []
        for t in self.teams:
            try:
                results.append(t.analyze(df))
            except Exception as e:
                results.append({"signal": "WATCH", "patterns": ["error"]})
        votes = [r for r in results if r["signal"] not in ("WATCH","PASS")]
        if not votes:
            return {"final_signal": "WATCH", "votes": results}
        long_votes = [v for v in votes if v["signal"] == "LONG"]
        short_votes = [v for v in votes if v["signal"] == "SHORT"]
        if len(long_votes) >= 1 and len(long_votes) > len(short_votes):
            return {"final_signal": "LONG", "votes": results}
        if len(short_votes) >= 1 and len(short_votes) > len(long_votes):
            return {"final_signal": "SHORT", "votes": results}
        return {"final_signal": "WATCH", "votes": results}

# --------------------------
# Analyze symbol
# --------------------------
def analyze_symbol(symbol: str, interval: str = KL_INTERVAL) -> Dict[str, Any]:
    try:
        df = fetch_klines(symbol, interval=interval, limit=KLINES_LIMIT)
        df = add_indicators(df)
    except Exception as e:
        logger.exception("analyze_symbol error for %s", symbol)
        return {"symbol": symbol, "error": str(e)}
    teams = [TrendTeam(), MomentumTeam(), RiskTeam()]
    coord = Coordinator(teams)
    decision = coord.decide(df)
    decision["symbol"] = symbol
    decision["last_price"] = float(df["close"].iloc[-1])
    return decision

# --------------------------
# Periodic analyzer
# --------------------------
def run_periodic_analysis():
    logger.info("Background analyzer started (cycle=%ds, top=%d)", CYCLE_SECONDS, TOP_N)
    while True:
        try:
            symbols = get_top_symbols(TOP_N)
            signals = []
            for sym in symbols:
                d = analyze_symbol(sym, KL_INTERVAL)
                if d.get("final_signal") in ("LONG","SHORT"):
                    signals.append(d)
                logger.info("Analyzed %s -> %s", sym, d.get("final_signal"))
                time.sleep(1)
            if signals:
                txt = f"⚡ Signals summary — {len(signals)} alerts\n"
                for s in signals:
                    txt += f"{s['symbol']}: {s['final_signal']} price={s['last_price']}\n"
                send_telegram_message(txt)
            else:
                logger.info("No alerts this cycle")
        except Exception as e:
            logger.exception("run_periodic_analysis loop error: %s", e)
        time.sleep(CYCLE_SECONDS)

# --------------------------
# Background thread
# --------------------------
def _start_bg_once():
    if getattr(_start_bg_once, "started", False):
        return
    t = threading.Thread(target=run_periodic_analysis, daemon=True)
    t.start()
    _start_bg_once.started = True

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
        results = {s: analyze_symbol(s, KL_INTERVAL) for s in symbols}
        positive = [v for v in results.values() if v.get("final_signal") in ("LONG","SHORT")]
        if positive:
            txt = f"🔔 Manual run alerts ({len(positive)})\n"
            for p in positive:
                txt += f"{p['symbol']}: {p['final_signal']} price={p.get('last_price')}\n"
            send_telegram_message(txt)
        return jsonify(results)
    except Exception as e:
        logger.exception("manual_analyze error")
        return jsonify({"error": str(e)}), 500