#!/usr/bin/env python3
# main.py - Smart Money Futures scanner + Telegram alerts
# Requirements: python-binance, pandas, numpy, requests, flask
# Install: pip install python-binance pandas numpy requests flask ta mplfinance

import os
import time
import json
import logging
import math
from datetime import datetime, timezone
from threading import Thread
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import numpy as np
import requests
from flask import Flask, jsonify

from binance.client import Client
from binance.exceptions import BinanceAPIException

# ---------------- CONFIG / ENV ----------------
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("CHAT_ID", "")
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))  # sec (default 5 minutes)
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
SYMBOLS_ENV = os.getenv("SYMBOLS", "")  # optional CSV of symbols

# safety defaults
if not BINANCE_API_KEY or not BINANCE_API_SECRET:
    logging.warning("BINANCE API KEY/SECRET not set - some endpoints will fail if used.")
if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    logging.warning("TELEGRAM token/chat not set - alerts will not be sent.")

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("smc-futures-bot")

# ---------------- BINANCE CLIENT ----------------
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# ---------------- STATE ----------------
STATE_FILE = "state_signals.json"
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            return json.load(open(STATE_FILE, "r"))
    except Exception:
        logger.exception("load_state error")
    return {"signals": {}, "last_scan": None}
def save_state(state):
    try:
        json.dump(state, open(STATE_FILE + ".tmp", "w"), indent=2, default=str)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception:
        logger.exception("save_state error")
state = load_state()

# ---------------- TELEGRAM ----------------
def send_telegram(text: str, parse_mode="Markdown"):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("Telegram disabled - would send:\n%s", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            logger.warning("Telegram send failed: %s %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

# ---------------- UTIL: fetch top symbols ----------------
def fetch_top_movers(limit=100):
    """Fetch top movers by 24h change from futures tickers."""
    try:
        tickers = client.futures_ticker()  # returns list of dicts
        usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
        sorted_by_change = sorted(usdt, key=lambda x: abs(float(x.get("priceChangePercent", 0))), reverse=True)
        symbols = [t["symbol"] for t in sorted_by_change[:limit]]
        logger.info("Top movers: %s", symbols[:10])
        return symbols
    except Exception as e:
        logger.exception("fetch_top_movers error: %s", e)
        return []

# ---------------- FETCH KLINES ----------------
def fetch_klines(symbol: str, interval="5m", limit=200):
    try:
        kl = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(kl, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"
        ])
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        return df
    except BinanceAPIException as e:
        logger.error("Binance API error fetching klines %s: %s", symbol, e)
    except Exception as e:
        logger.exception("fetch_klines error %s: %s", symbol, e)
    return None

# ---------------- SMC / PATTERN FEATURE ENGINEERING ----------------
def apply_smc_features(df: pd.DataFrame):
    df = df.copy()
    # basic
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_wick"] = df["high"] - df[["close","open"]].max(axis=1)
    df["lower_wick"] = df[["close","open"]].min(axis=1) - df["low"]
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > 2.5 * df["vol_ma20"]

    # dynamic support/resistance - simple last 20
    df["support"] = df["low"].rolling(20).min()
    df["resistance"] = df["high"].rolling(20).max()

    # imbalances (FVG-like): look for consecutive candles with gap (simplified)
    fvg = []
    for i in range(len(df)):
        if i >= 2:
            prev1_high = df["high"].iat[i-2]
            prev1_low = df["low"].iat[i-2]
            # simple FVG: current low > previous high (gap up) OR current high < previous low (gap down)
            cur_low = df["low"].iat[i]
            cur_high = df["high"].iat[i]
            fvg.append((cur_low > prev1_high) or (cur_high < prev1_low))
        else:
            fvg.append(False)
    df["fvg"] = fvg

    # liquidity sweeps: wick beyond SR but closed back inside
    df["liquidity_sweep_long"] = (df["low"] < df["support"]) & (df["close"] > df["support"])
    df["liquidity_sweep_short"] = (df["high"] > df["resistance"]) & (df["close"] < df["resistance"])

    # CHOCH detection (change of character) - simple heuristic:
    # find short-term trend: last 10 closes slope sign, compare prev 10
    df["ma10"] = df["close"].rolling(10).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    choch = [False]*len(df)
    for i in range(len(df)):
        if i >= 25:
            # previous structure
            prev_high = df["high"].iloc[i-20:i-10].max()
            prev_low = df["low"].iloc[i-20:i-10].min()
            recent_high = df["high"].iloc[i-10:i].max()
            recent_low = df["low"].iloc[i-10:i].min()
            # CHOCH to bullish if recent_high > prev_high and close breaks above prev_high after being below earlier
            if recent_high > prev_high and df["close"].iat[i] > prev_high:
                choch[i] = "bullish"
            elif recent_low < prev_low and df["close"].iat[i] < prev_low:
                choch[i] = "bearish"
    df["choch"] = choch

    # accumulation zone: low-range + rising volume
    df["range_ma20"] = df["range"].rolling(20).mean()
    df["accumulation_zone"] = (df["range"] < 0.6 * df["range_ma20"]) & (df["volume"] > df["vol_ma20"])

    # short-term imbalance (strong body)
    df["imbalance_up"] = (df["body"] > 0) & (df["body"] > 0.6 * df["range"])
    df["imbalance_down"] = (df["body"] < 0) & (abs(df["body"]) > 0.6 * df["range"])

    return df

# ---------------- AUX: funding, oi, liq ----------------
def get_funding_rate(symbol):
    try:
        fr = client.futures_funding_rate(symbol=symbol, limit=1)
        return float(fr[0]["fundingRate"]) if fr else 0.0
    except Exception:
        return 0.0

def get_open_interest(symbol):
    try:
        oi = client.futures_open_interest(symbol=symbol)
        return float(oi.get("openInterest", 0))
    except Exception:
        return 0.0

def get_recent_liquidations(symbol, limit=50):
    try:
        # Note: some python-binance wrappers don't have futures_liq_orders; use futures_account or public endpoints if missing.
        # We'll try futures_liquidation_orders and gracefully fallback.
        if hasattr(client, "futures_liquidation_orders"):
            liqs = client.futures_liquidation_orders(symbol=symbol, limit=limit)
            return liqs
        return []
    except Exception:
        return []

# ---------------- SIGNAL DETECTION (SMC) ----------------
def detect_smc_signals(df: pd.DataFrame, funding: float, oi_now: float, oi_prev: float):
    last = df.iloc[-1]
    votes = []
    conf = 0.45

    # FVG / imbalance
    if last.get("fvg", False):
        votes.append("FVG/Imbalance"); conf += 0.07

    # liquidity sweeps
    if last.get("liquidity_sweep_long", False):
        votes.append("Liquidity Sweep (Long)"); conf += 0.08
    if last.get("liquidity_sweep_short", False):
        votes.append("Liquidity Sweep (Short)"); conf += 0.08

    # vol spike / whale candle
    if last.get("vol_spike", False):
        votes.append("Volume Spike"); conf += 0.06
    if last.get("imbalance_up", False):
        votes.append("Imbalance Up"); conf += 0.05
    if last.get("imbalance_down", False):
        votes.append("Imbalance Down"); conf += 0.05

    # CHOCH
    choch_val = last.get("choch", False)
    if choch_val == "bullish":
        votes.append("CHOCH Bullish"); conf += 0.08
    elif choch_val == "bearish":
        votes.append("CHOCH Bearish"); conf += 0.08

    # accumulation
    if last.get("accumulation_zone", False):
        votes.append("Accumulation Zone"); conf += 0.04

    # funding & OI context
    if funding > 0.0006:
        votes.append("Funding Long Bias (extreme)"); conf += 0.06
    if funding < -0.0006:
        votes.append("Funding Short Bias (extreme)"); conf += 0.06
    if oi_prev and oi_prev > 0:
        oi_change = (oi_now - oi_prev) / oi_prev
        if oi_change > 0.12:
            votes.append("OI Surge"); conf += 0.06
        elif oi_change < -0.12:
            votes.append("OI Drop"); conf -= 0.03

    # determine action
    action = "WATCH"
    # strong combos -> actionable
    if any("Liquidity Sweep (Long)" in v for v in votes) and "CHOCH Bullish" in votes:
        action = "LONG"
    elif any("Liquidity Sweep (Short)" in v for v in votes) and "CHOCH Bearish" in votes:
        action = "SHORT"
    else:
        # fallback to directional biases
        # more longs than shorts -> LONG bias
        longs = sum(1 for v in votes if "Long" in v or "Bull" in v or "Up" in v)
        shorts = sum(1 for v in votes if "Short" in v or "Bear" in v or "Down" in v)
        if longs >= 2 and longs > shorts:
            action = "LONG"
        elif shorts >= 2 and shorts > longs:
            action = "SHORT"

    conf = max(0.0, min(1.0, conf))
    return action, votes, conf

# ---------------- QUALITY / SCORE ----------------
def smc_quality_score(votes, conf):
    # map signals to points
    mapping = {
        "FVG/Imbalance": 8, "Liquidity Sweep (Long)": 12, "Liquidity Sweep (Short)": 12,
        "Volume Spike": 6, "Imbalance Up": 6, "Imbalance Down": 6,
        "CHOCH Bullish": 10, "CHOCH Bearish": 10, "Accumulation Zone": 5,
        "Funding Long Bias (extreme)": 5, "Funding Short Bias (extreme)": 5,
        "OI Surge": 6, "OI Drop": -3
    }
    score = conf * 100
    for v in votes:
        score += mapping.get(v, 0)
    # normalize 0-100
    score = max(0, min(100, score))
    return int(score)

# ---------------- ENTRY / SL / TP suggestion ----------------
def suggest_levels(df, action, last):
    support = last["support"]
    resistance = last["resistance"]
    if pd.isna(support) or pd.isna(resistance):
        return None, None, None, None
    if action == "LONG":
        entry = last["close"]
        sl = support * 0.995 if support>0 else entry*0.995
        tp1 = entry + (resistance - entry) * 0.33
        tp2 = entry + (resistance - entry) * 0.66
        tp3 = resistance
    elif action == "SHORT":
        entry = last["close"]
        sl = resistance * 1.005 if resistance>0 else entry*1.005
        tp1 = entry - (entry - support) * 0.33
        tp2 = entry - (entry - support) * 0.66
        tp3 = support
    else:
        return None, None, None, None
    return entry, sl, tp1, tp2, tp3

# ---------------- MAIN analyze_and_alert ----------------
def analyze_and_alert(symbol):
    try:
        interval = "5m"
        df = fetch_klines(symbol, interval=interval, limit=200)
        if df is None or len(df) < 50:
            return

        # fetch context features
        funding = get_funding_rate(symbol)
        oi_now = get_open_interest(symbol)
        # quick previous OI sample (5m ago): fetch older klines for OI history not available, so approximate by small sleepless fallback
        # We'll attempt to fetch open interest historical via futures_open_interest_hist (may not exist) else reuse oi_now
        oi_prev = oi_now
        try:
            if hasattr(client, "futures_open_interest_hist"):
                hist = client.futures_open_interest_hist(symbol=symbol, period="5m", limit=2)
                if len(hist) >= 2:
                    oi_prev = float(hist[-2].get("sumOpenInterest", oi_now))
        except Exception:
            pass

        # pre-process features
        df = apply_smc_features(df)

        # detect signals
        action, votes, conf = detect_smc_signals(df, funding, oi_now, oi_prev)
        score = smc_quality_score(votes, conf)
        last = df.iloc[-1]

        # suggest levels
        levels = suggest_levels(df, action, last)
        entry, sl, tp1, tp2, tp3 = (levels + (None,))[:5] if levels else (None,)*5

        logger.info("S:%s action=%s score=%s votes=%s conf=%.2f", symbol, action, score, votes, conf)

        # filter to avoid spam: require score >= 55 and action != WATCH
        if action != "WATCH" and score >= 55:
            # format message
            lines = [
                f"⚡ *FUTURES SMC SIGNAL*",
                f"Symbol: `{symbol}`",
                f"Action: *{action}*",
                f"Last price: `{last['close']:.6f}`",
                f"Score: *{score}/100*  Confidence: *{conf:.2f}*",
                f"Funding Rate: `{funding:.6f}`  OpenInterest: `{oi_now:.0f}`",
                f"Patterns: {', '.join(votes) or '—'}",
                ""
            ]
            if entry:
                lines += [
                    f"Entry: `{entry:.6f}`",
                    f"Stop-Loss: `{sl:.6f}`",
                    f"TP1: `{tp1:.6f}`  TP2: `{tp2:.6f}`  TP3: `{tp3:.6f}`",
                ]
            # advice text
            lines.append("_Note: this is a signal idea, not trading advice. Check liquidity & timeframe alignment._")
            msg = "\n".join(lines)
            send_telegram(msg)
            # save state
            state.setdefault("signals", {})[symbol] = {
                "time": str(datetime.now(timezone.utc)),
                "action": action,
                "score": score,
                "conf": conf,
                "votes": votes,
                "price": float(last["close"])
            }
            save_state(state)

    except Exception as e:
        logger.exception("analyze_and_alert error %s: %s", symbol, e)

# ---------------- SCAN MANAGER ----------------
def get_symbols_to_scan():
    if SYMBOLS_ENV:
        return [s.strip().upper() for s in SYMBOLS_ENV.split(",") if s.strip()]
    # otherwise pull top movers
    syms = fetch_top_movers(limit=80)
    if not syms:
        # fallback baseline
        return ["BTCUSDT","ETHUSDT","SOLUSDT"]
    return syms

def scan_all_symbols():
    symbols = get_symbols_to_scan()
    logger.info("Starting scan for %d symbols", len(symbols))
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
        list(exe.map(analyze_and_alert, symbols))
    state["last_scan"] = str(datetime.now(timezone.utc))
    save_state(state)
    logger.info("Scan finished at %s", state["last_scan"])

# ---------------- AUTO LOOP ----------------
def run_loop():
    while True:
        try:
            scan_all_symbols()
        except Exception:
            logger.exception("scan loop error")
        time.sleep(SCAN_INTERVAL)

# ---------------- FLASK (health + manual trigger) ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({"ok": True, "last_scan": state.get("last_scan")})

@app.route("/trigger_scan")
def trigger_scan():
    Thread(target=scan_all_symbols, daemon=True).start()
    return jsonify({"ok": True, "msg": "scan started"})

# ---------------- START background scanner ----------------
def start_background():
    t = Thread(target=run_loop, daemon=True)
    t.start()
    logger.info("Background scanner started (interval %s sec)", SCAN_INTERVAL)

# If run as main, start loop; if imported by gunicorn, we still start background thread
start_background()

if __name__ == "__main__":
    # run flask for local debugging
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))