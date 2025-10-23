import os
import time
import json
import logging
import re
from datetime import datetime, timezone
from threading import Thread
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import matplotlib.pyplot as plt
import requests
import ta
import mplfinance as mpf
import numpy as np
import io

from binance.client import Client

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("pretop-bot")

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "5000"))
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
EMA_SCAN_LIMIT = 500
STATE_FILE = "state.json"
CONF_THRESHOLD_MEDIUM = 0.3

# ---------------- BINANCE CLIENT ----------------
binance_client = Client(api_key="", api_secret="")

# ---------------- STATE ----------------
def load_json_safe(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.exception("load_json_safe error %s: %s", path, e)
    return default

def save_json_safe(path, data):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        logger.exception("save_json_safe error %s: %s", path, e)

state = load_json_safe(STATE_FILE, {"signals": {}, "last_scan": None})

# ---------------- TELEGRAM ----------------
MARKDOWNV2_ESCAPE = r"_*[]()~`>#+-=|{}.!"

def escape_md_v2(text: str) -> str:
    return re.sub(f"([{re.escape(MARKDOWNV2_ESCAPE)}])", r"\\\1", str(text))

def send_telegram(text: str, photo=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        if photo:
            files = {'photo': ('signal.png', photo, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=10)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

# ---------------- WEBSOCKET / REST MANAGER ----------------
from websocket_manager import WebSocketKlineManager

ALL_USDT = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT",
    "DOTUSDT","TRXUSDT","LTCUSDT","AVAXUSDT","SHIBUSDT","LINKUSDT","ATOMUSDT","XMRUSDT",
    "ETCUSDT","XLMUSDT","APTUSDT","NEARUSDT","FILUSDT","ICPUSDT","GRTUSDT","AAVEUSDT",
    "SANDUSDT","AXSUSDT","FTMUSDT","THETAUSDT","EGLDUSDT","MANAUSDT","FLOWUSDT","HBARUSDT",
    "ALGOUSDT","ZECUSDT","EOSUSDT","KSMUSDT","CELOUSDT","SUSHIUSDT","CHZUSDT","KAVAUSDT",
    "ZILUSDT","ANKRUSDT","RAYUSDT","GMTUSDT","UNIUSDT","APEUSDT","PEPEUSDT","OPUSDT",
    "XTZUSDT","ALPHAUSDT","BALUSDT","COMPUSDT","CRVUSDT","SNXUSDT","RSRUSDT",
    "LOKUSDT","GALUSDT","WLDUSDT","JASMYUSDT","ONEUSDT","ARBUSDT","ALICEUSDT","XECUSDT",
    "FLMUSDT","CAKEUSDT","IMXUSDT","HOOKUSDT","MAGICUSDT","STGUSDT","FETUSDT",
    "PEOPLEUSDT","ASTRUSDT","ENSUSDT","CTSIUSDT","GALAUSDT","RADUSDT","IOSTUSDT","QTUMUSDT",
    "NPXSUSDT","DASHUSDT","ZRXUSDT","HNTUSDT","ENJUSDT","TFUELUSDT","TWTUSDT",
    "NKNUSDT","GLMRUSDT","ZENUSDT","STORJUSDT","ICXUSDT","XVGUSDT","FLOKIUSDT","BONEUSDT",
    "TRBUSDT","C98USDT","MASKUSDT","1000SHIBUSDT","1000PEPEUSDT","AMBUSDT","VEGUSDT","QNTUSDT",
    "RNDRUSDT","CHRUSDT","API3USDT","MTLUSDT","ALPUSDT","LDOUSDT","AXLUSDT","FUNUSDT",
    "OGUSDT","ORCUSDT","XAUTUSDT","ARUSDT","DYDXUSDT","RUNEUSDT","FLUXUSDT",
    "AGLDUSDT","PERPUSDT","MLNUSDT","NMRUSDT","LRCUSDT","COTIUSDT","ACHUSDT",
    "CKBUSDT","ACEUSDT","TRUUSDT","IPSUSDT","QIUSDT","GLMUSDT","ARNXUSDT",
    "MIRUSDT","ROSEUSDT","OXTUSDT","SPELLUSDT","SUNUSDT","SYSUSDT","TAOUSDT",
    "TLMUSDT","VLXUSDT","WAXPUSDT","XNOUSDT"
]

BINANCE_REST_URL = "https://fapi.binance.com/fapi/v1/klines"

def fetch_klines_rest(symbol, interval="15m", limit=500):
    try:
        resp = requests.get(BINANCE_REST_URL, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=5)
        data = resp.json()
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"
        ])
        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        return df
    except Exception as e:
        logger.exception("REST fetch error for %s: %s", symbol, e)
        return None

ws_manager = WebSocketKlineManager(symbols=ALL_USDT, interval="15m")
Thread(target=ws_manager.start, daemon=True).start()

def fetch_klines(symbol, limit=500):
    df = ws_manager.get_klines(symbol, limit)
    if df is None or len(df) < 10:
        df = fetch_klines_rest(symbol, limit=limit)
    return df

# ---------------- PATTERN-BASED FEATURE ENGINEERING ----------------
def apply_all_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # Support/Resistance (динамічні рівні за останні 20 свічок)
    df["support"] = df["low"].rolling(20).min()
    df["resistance"] = df["high"].rolling(20).max()

    # Volume analysis
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > 1.5 * df["vol_ma20"]

    # Candle structure
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_shadow"] = df[["close", "open"]].min(axis=1) - df["low"]

    return df

# ---------------- PATTERN-BASED SIGNAL DETECTION ----------------
# ---------------- ADVANCED SIGNAL DETECTION (V2) ----------------
def detect_signal_v2(df: pd.DataFrame):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    votes = []
    confidence = 0.5  # базова впевненість

    # --- 1. Price Action (свічкові патерни) ---
    # Hammer / Shooting Star
    if last["lower_shadow"] > 2 * abs(last["body"]) and last["body"] > 0:
        votes.append("hammer_bull"); confidence += 0.1
    if last["upper_shadow"] > 2 * abs(last["body"]) and last["body"] < 0:
        votes.append("shooting_star"); confidence += 0.1

    # Engulfing
    if last["body"] > 0 and prev["body"] < 0 and last["close"] > prev["open"] and last["open"] < prev["close"]:
        votes.append("bullish_engulfing"); confidence += 0.1
    if last["body"] < 0 and prev["body"] > 0 and last["close"] < prev["open"] and last["open"] > prev["close"]:
        votes.append("bearish_engulfing"); confidence += 0.1

    # Doji
    if abs(last["body"]) < 0.1 * last["range"]:
        votes.append("doji"); confidence += 0.05

    # Tweezer Top/Bottom
    if abs(last["high"] - prev["high"]) < 0.001 * last["high"] and last["close"] < last["open"]:
        votes.append("tweezer_top"); confidence += 0.05
    if abs(last["low"] - prev["low"]) < 0.001 * last["low"] and last["close"] > last["open"]:
        votes.append("tweezer_bottom"); confidence += 0.05

    # Inside / Outside bar
    if last["high"] < prev["high"] and last["low"] > prev["low"]:
        votes.append("inside_bar"); confidence += 0.05
    if last["high"] > prev["high"] and last["low"] < prev["low"]:
        votes.append("outside_bar"); confidence += 0.05

    # Momentum exhaustion (3+ свічки в один бік)
    if all(df["close"].iloc[-i] > df["open"].iloc[-i] for i in range(1, 4)):
        votes.append("3_green"); confidence += 0.05
    if all(df["close"].iloc[-i] < df["open"].iloc[-i] for i in range(1, 4)):
        votes.append("3_red"); confidence += 0.05

    # --- 2. Volume & Liquidity ---
    if last["vol_spike"]:
        votes.append("volume_spike"); confidence += 0.05
    if last["volume"] > 2 * df["vol_ma20"].iloc[-1]:
        votes.append("climax_volume"); confidence += 0.05
    if last["volume"] < 0.5 * df["vol_ma20"].iloc[-1] and (
        last["close"] > last["resistance"] or last["close"] < last["support"]):
        votes.append("low_volume_breakout"); confidence -= 0.05

    # --- 3. Structure & Levels ---
    if prev["close"] > prev["resistance"] and last["close"] < last["resistance"]:
        votes.append("fake_breakout_short"); confidence += 0.05
    if prev["close"] < prev["support"] and last["close"] > last["support"]:
        votes.append("fake_breakout_long"); confidence += 0.05
    if prev["close"] < prev["resistance"] and last["close"] > last["resistance"]:
        votes.append("resistance_flip_support"); confidence += 0.05
    if prev["close"] > prev["support"] and last["close"] < last["support"]:
        votes.append("support_flip_resistance"); confidence += 0.05

    # Retest
    if abs(last["close"] - last["support"]) / last["support"] < 0.003 and last["body"] > 0:
        votes.append("support_retest"); confidence += 0.05
    if abs(last["close"] - last["resistance"]) / last["resistance"] < 0.003 and last["body"] < 0:
        votes.append("resistance_retest"); confidence += 0.05

    # Liquidity grab (свічка проколола рівень, але закрилась всередині)
    if last["low"] < last["support"] and last["close"] > last["support"]:
        votes.append("liquidity_grab_long"); confidence += 0.05
    if last["high"] > last["resistance"] and last["close"] < last["resistance"]:
        votes.append("liquidity_grab_short"); confidence += 0.05

    # --- 4. Trend & Context ---
    df["trend"] = df["close"].rolling(20).mean()
    if last["close"] > df["trend"].iloc[-1]:
        votes.append("above_trend"); confidence += 0.05
    else:
        votes.append("below_trend"); confidence += 0.05

    # --- Pre-top (як було) ---
    pretop = False
    if len(df) >= 10 and (last["close"] - df["close"].iloc[-10]) / df["close"].iloc[-10] > 0.10:
        pretop = True
        votes.append("pretop"); confidence += 0.1

    # --- Action ---
    action = "WATCH"
    near_resistance = last["close"] >= last["resistance"] * 0.98
    near_support = last["close"] <= last["support"] * 1.02
    if near_resistance:
        action = "SHORT"
    elif near_support:
        action = "LONG"

    # Clamp confidence
    confidence = max(0.0, min(1.0, confidence))
    return action, votes, pretop, last, confidence


# ---------------- MAIN ANALYZE FUNCTION ----------------
def analyze_and_alert(symbol: str):
    df = fetch_klines(symbol, limit=200)
    if df is None or len(df) < 40:
        return

    df = apply_all_features(df)

    action, votes, pretop, last, confidence = detect_signal_v2(df)

    # Entry / SL / TP
    entry = stop_loss = tp1 = tp2 = tp3 = None
    if action == "LONG":
        entry = last["support"] * 1.001
        stop_loss = last["support"] * 0.99
        tp1 = entry + (last["resistance"] - entry) * 0.33
        tp2 = entry + (last["resistance"] - entry) * 0.66
        tp3 = last["resistance"]
    elif action == "SHORT":
        entry = last["resistance"] * 0.999
        stop_loss = last["resistance"] * 1.01
        tp1 = entry - (entry - last["support"]) * 0.33
        tp2 = entry - (entry - last["support"]) * 0.66
        tp3 = last["support"]

    if action == "WATCH":
        return

    # R/R
    rr1 = (tp1 - entry)/(entry - stop_loss) if action=="LONG" else (entry - tp1)/(stop_loss - entry)
    rr2 = (tp2 - entry)/(entry - stop_loss) if action=="LONG" else (entry - tp2)/(stop_loss - entry)
    rr3 = (tp3 - entry)/(entry - stop_loss) if action=="LONG" else (entry - tp3)/(stop_loss - entry)

    logger.info(
        "Symbol=%s action=%s confidence=%.2f votes=%s pretop=%s RR1=%.2f RR2=%.2f RR3=%.2f",
        symbol, action, confidence, votes, pretop, rr1, rr2, rr3
    )

    # --- Фільтр: мінімум RR >= 2 ---
    if confidence >= CONF_THRESHOLD_MEDIUM and rr1 >= 2.0:
        reasons = []
        if "pretop" in votes: reasons.append("Pre-Top")
        if "fake_breakout_long" in votes or "fake_breakout_short" in votes: reasons.append("Fake Breakout")
        if "resistance_flip_support" in votes or "support_flip_resistance" in votes: reasons.append("S/R Flip")
        if "volume_spike" in votes: reasons.append("Volume Spike")
        if not reasons: reasons = ["Candle/Pattern Mix"]

        msg = (
            f"⚡ TRADE SIGNAL\n"
            f"Symbol: {symbol}\n"
            f"Action: {action}\n"
            f"Entry: {entry:.6f}\n"
            f"Stop-Loss: {stop_loss:.6f}\n"
            f"Take-Profit 1: {tp1:.6f} (RR {rr1:.2f})\n"
            f"Take-Profit 2: {tp2:.6f} (RR {rr2:.2f})\n"
            f"Take-Profit 3: {tp3:.6f} (RR {rr3:.2f})\n"
            f"Confidence: {confidence:.2f}\n"
            f"Reasons: {', '.join(reasons)}\n"
            f"Patterns: {', '.join(votes)}\n"
        )

        photo_buf = plot_signal_candles(df, symbol, action, tp1=tp1, tp2=tp2, tp3=tp3, sl=stop_loss, entry=entry)
        send_telegram(msg, photo=photo_buf)

        state.setdefault("signals", {})[symbol] = {
            "action": action, "entry": entry, "sl": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "rr1": rr1, "rr2": rr2, "rr3": rr3, "confidence": confidence,
            "time": str(last.name), "last_price": float(last["close"]), "votes": votes
        }
        save_json_safe(STATE_FILE, state)

# ---------------- PLOT UTILITY ----------------
def plot_signal_candles(df, symbol, action, tp1=None, tp2=None, tp3=None, sl=None, entry=None):
    addplots = []
    if tp1: addplots.append(mpf.make_addplot([tp1]*len(df), color='green', linestyle="--"))
    if tp2: addplots.append(mpf.make_addplot([tp2]*len(df), color='lime', linestyle="--"))
    if tp3: addplots.append(mpf.make_addplot([tp3]*len(df), color='darkgreen', linestyle="--"))
    if sl: addplots.append(mpf.make_addplot([sl]*len(df), color='red', linestyle="--"))
    if entry: addplots.append(mpf.make_addplot([entry]*len(df), color='blue', linestyle="--"))

    fig, ax = mpf.plot(
        df.tail(200), type='candle', style='yahoo',
        title=f"{symbol} - {action}", addplot=addplots, returnfig=True
    )
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf

# ---------------- FETCH TOP SYMBOLS ----------------
def fetch_top_symbols(limit=300):
    try:
        # Беремо всі USDT-пари ф'ючерсів
        tickers = binance_client.futures_ticker()  # Для USDT-M ф'ючерсів
        usdt_pairs = [t for t in tickers if t['symbol'].endswith("USDT")]
        sorted_pairs = sorted(
            usdt_pairs,
            key=lambda x: abs(float(x.get("priceChangePercent", 0))),
            reverse=True
        )
        top_symbols = [d["symbol"] for d in sorted_pairs[:limit]]
        logger.info("Top %d symbols fetched: %s", limit, top_symbols[:10])
        return top_symbols
    except Exception as e:
        logger.exception("Error fetching top symbols: %s", e)
        return []

# ---------------- MASTER SCAN ----------------
def scan_all_symbols():
    symbols = fetch_top_symbols(limit=300)
    if not symbols:
        logger.warning("No symbols fetched, falling back to ALL_USDT list")
        symbols = ALL_USDT
    logger.info("Starting scan for %d symbols", len(symbols))
    def safe_analyze(sym):
        try:
            analyze_and_alert(sym)
        except Exception as e:
            logger.exception("Error analyzing symbol %s: %s", sym, e)
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
        list(exe.map(safe_analyze, symbols))
    state["last_scan"] = str(datetime.now(timezone.utc))
    save_json_safe(STATE_FILE, state)
    logger.info("Scan finished at %s", state["last_scan"])

# ---------------- FLASK ----------------
from flask import Flask, request, jsonify
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "time": str(datetime.now(timezone.utc)),
        "signals": len(state.get("signals", {}))
    })

@app.route("/telegram_webhook/<token>", methods=["POST"])
def telegram_webhook(token):
    if token != TELEGRAM_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    update = request.get_json(force=True) or {}
    text = update.get("message", {}).get("text", "").lower().strip()
    if text.startswith("/scan"):
        send_telegram("⚡ Manual scan started.")
        Thread(target=scan_all_symbols, daemon=True).start()
    return jsonify({"ok": True})

# ---------------- MAIN ----------------
if __name__ == "__main__":
    logger.info("Starting pre-top detector bot")
    Thread(target=scan_all_symbols, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)