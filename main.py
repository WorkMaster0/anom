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
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("crypto-coordinator")

# --------------------------
# Binance client init
# --------------------------
if Client is None:
    logger.error("python-binance not installed")
    binance_client = None
else:
    try:
        binance_client = Client(api_key=BINANCE_API_KEY or None, api_secret=BINANCE_API_SECRET or None)
    except Exception as e:
        logger.exception("Binance init failed: %s", e)
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
        try:
            j = resp.json()
        except Exception:
            j = {"ok": resp.ok, "status_code": resp.status_code, "text": resp.text}
        if not resp.ok or not j.get("ok", False):
            logger.warning("Telegram API response not OK: %s", j)
        else:
            logger.info("Telegram message sent (len=%d)", len(text))
        return j
    except Exception as e:
        logger.exception("send_telegram_message error: %s", e)
        return None

# --------------------------
# Binance helpers
# --------------------------
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
            df = df.sort_values("quoteVolume", ascending=False)
            valid_syms = []
            for sym in df["symbol"]:
                try:
                    kl = fetch_klines(sym, KL_INTERVAL, KLINES_LIMIT)
                    if len(kl) >= 14:
                        valid_syms.append(sym)
                    if len(valid_syms) >= limit:
                        break
                except Exception:
                    continue
            logger.info("Top %d symbols with enough data: %s", len(valid_syms), valid_syms)
            return valid_syms
        return ["BTCUSDT"]
    except Exception as e:
        logger.exception("get_top_symbols error: %s", e)
        return ["BTCUSDT"]

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
    df["atr14"] = df["true_range"].rolling(14).mean().bfill()  # замість deprecated fillna
    df["funding_long"] = False
    df["funding_short"] = False
    df["oi_long"] = False
    df["oi_short"] = False
    return df

# --------------------------
# Teams (Trend/Momentum/Pattern/Orderflow/Risk)
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
        sl = entry - 1.5 * atr
        tp = entry + 3.0 * atr
        rr = (tp - entry) / (entry - sl) if (entry - sl) != 0 else 0.0
        return {"signal": "PASS" if rr >= 3.0 else "WATCH", "confidence": min(1, max(0, rr/5)), "patterns": ["rr=%.2f" % rr], "rr": rr}

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
                r = t.analyze(df)
            except Exception as e:
                logger.exception("Team analyze failed: %s", e)
                r = {"signal": "WATCH", "confidence": 0.0, "patterns": ["error"]}
            results.append(r)
        votes = [r for r in results if r["signal"] not in ("WATCH","PASS")]
        if not votes:
            return {"final_signal": "WATCH", "confidence": 0.0, "votes": results}
        long_votes = [v for v in votes if v["signal"] == "LONG"]
        short_votes = [v for v in votes if v["signal"] == "SHORT"]
        if len(long_votes) >= 2 and len(long_votes) > len(short_votes):
            conf = float(np.mean([v.get("confidence", 0.0) for v in long_votes]))
            return {"final_signal": "LONG", "confidence": conf, "votes": results}
        if len(short_votes) >= 2 and len(short_votes) > len(long_votes):
            conf = float(np.mean([v.get("confidence", 0.0) for v in short_votes]))
            return {"final_signal": "SHORT", "confidence": conf, "votes": results}
        return {"final_signal": "WATCH", "confidence": 0.0, "votes": results}

# --------------------------
# Per-symbol analyze
# --------------------------
def analyze_symbol(symbol: str, interval: str = KL_INTERVAL) -> Dict[str, Any]:
    try:
        df = fetch_klines(symbol, interval, KLINES_LIMIT)
        if len(df) < 14:
            logger.warning("Skipping %s: not enough data (%d candles)", symbol, len(df))
            return {"symbol": symbol, "final_signal": "WATCH"}
        df = add_indicators(df)
    except Exception as e:
        logger.exception("analyze_symbol failed for %s", symbol)
        return {"symbol": symbol, "final_signal": "WATCH", "error": str(e)}
    teams = [TrendTeam(), MomentumTeam(), PatternTeam(), OrderflowTeam(), RiskTeam()]
    coord = Coordinator(teams)
    decision = coord.decide(df)
    decision["symbol"] = symbol
    decision["last_price"] = float(df["close"].iloc[-1]) if len(df)>0 else None
    for v in decision.get("votes", []):
        if "rr" in v:
            decision["rr"] = v["rr"]
            break
    return decision

# --------------------------
# Periodic runner
# --------------------------
_last_cycle_ts = None
_lock = threading.Lock()

def run_periodic_analysis():
    global _last_cycle_ts
    logger.info("Background periodic analysis thread started (cycle %ds, top=%d)", CYCLE_SECONDS, TOP_N)
    while True:
        try:
            symbols = get_top_symbols(TOP_N)
            signals = []
            for sym in symbols:
                try:
                    d = analyze_symbol(sym, KL_INTERVAL)
                    if d.get("final_signal") in ("LONG", "SHORT"):
                        signals.append(d)
                    logger.info("Analyzed %s -> %s", sym, d.get("final_signal"))
                except Exception as e:
                    logger.exception("Error analyzing %s: %s", sym, e)
                time.sleep(1.2)
            if signals:
                txt = f"⚡ <b>Signals summary — {len(signals)} alerts</b>\n"
                for s in signals:
                    txt += f"\n<b>{s['symbol']}</b>: {s['final_signal']} (conf {s['confidence']:.2f}) price {s.get('last_price')}\n"
                    pat_list = []
                    for v in s.get("votes", []):
                        if v.get("patterns"):
                            pat_list.extend(v.get("patterns", []))
                    if pat_list:
                        txt += "patterns: " + ", ".join(map(str, pat_list)) + "\n"
                send_telegram_message(txt)
            else:
                logger.info("No alerts this cycle (checked %d symbols)", len(symbols))
        except Exception as e:
            logger.exception("run_periodic_analysis main loop error: %s", e)
        _last_cycle_ts = time.time()
        time.sleep(CYCLE_SECONDS)

# --------------------------
# Flask routes
# --------------------------
@app.route("/")
def home():
    return "OK", 200

@app.route("/analyze/<symbol>")
def analyze_endpoint(symbol):
    res = analyze_symbol(symbol)
    return jsonify(res)

# --------------------------
# Start background thread
# --------------------------
threading.Thread(target=run_periodic_analysis, daemon=True).start()

# --------------------------
# Run app
# --------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    logger.info("Starting Gunicorn web server on 0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port)