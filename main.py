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
import mplfinance as mpf
import io

from binance.client import Client
from flask import Flask, request, jsonify

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
STATE_FILE = "state.json"
CONF_THRESHOLD = 0.3

SCAN_INTERVALS = ["5m", "15m", "1h"]  # порядок = пріоритет сигналу
ALWAYS_SEND = True
COOLDOWN_SECONDS = 0  # якщо ALWAYS_SEND=False

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

state = load_json_safe(STATE_FILE, {"signals": {}, "last_scan": None, "spreads": {}})

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

# ---------------- SYMBOL LIST ----------------
ALL_USDT = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT",
    "DOTUSDT","TRXUSDT","LTCUSDT","AVAXUSDT","SHIBUSDT","LINKUSDT","ATOMUSDT","XMRUSDT",
    "ETCUSDT","XLMUSDT","APTUSDT","NEARUSDT","FILUSDT","ICPUSDT","GRTUSDT","AAVEUSDT"
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

# ---------------- FEATURE ENGINEERING ----------------
def apply_all_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["support"] = df["low"].rolling(20).min()
    df["resistance"] = df["high"].rolling(20).max()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > 1.5 * df["vol_ma20"]
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - df[["close","open"]].max(axis=1)
    df["lower_shadow"] = df[["close","open"]].min(axis=1) - df["low"]
    df["trend"] = df["close"].rolling(20).mean()
    return df

# ---------------- SIGNAL DETECTION ----------------
def detect_signal(df: pd.DataFrame):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    votes = []
    confidence = 0.5
    action = "WATCH"

    if last["lower_shadow"] > 2*abs(last["body"]) and last["body"] > 0:
        votes.append("hammer_bull"); confidence += 0.1
    if last["upper_shadow"] > 2*abs(last["body"]) and last["body"] < 0:
        votes.append("shooting_star"); confidence += 0.1

    if last["close"] > df["trend"].iloc[-1]:
        votes.append("above_trend"); confidence += 0.05
    else:
        votes.append("below_trend"); confidence += 0.05

    near_resistance = last["close"] >= last["resistance"]*0.98
    near_support = last["close"] <= last["support"]*1.02
    if near_resistance:
        action = "SHORT"
    elif near_support:
        action = "LONG"

    confidence = max(0, min(1, confidence))
    return action, votes, last, confidence

# ---------------- PLOT ----------------
def plot_signal_candles(df, symbol, action, interval, tp=None, sl=None, entry=None):
    addplots=[]
    if tp: addplots.append(mpf.make_addplot([tp]*len(df), color='green', linestyle="--"))
    if sl: addplots.append(mpf.make_addplot([sl]*len(df), color='red', linestyle="--"))
    if entry: addplots.append(mpf.make_addplot([entry]*len(df), color='blue', linestyle="--"))
    fig, ax = mpf.plot(df.tail(100), type='candle', style='yahoo',
                       title=f"{symbol} - {action} [{interval}]", addplot=addplots, returnfig=True)
    buf=io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf

# ---------------- ANALYZE ----------------
def analyze_symbol(symbol, interval):
    df = fetch_klines_rest(symbol, interval=interval, limit=200)
    if df is None or len(df)<40: return None
    df = apply_all_features(df)
    action, votes, last, confidence = detect_signal(df)
    return {"symbol": symbol, "interval": interval, "action": action, "votes": votes,
            "confidence": confidence, "last": last, "df": df}

# ---------------- SEND SIGNAL ----------------
def send_signal(symbol_data):
    symbol = symbol_data["symbol"]
    interval = symbol_data["interval"]
    action = symbol_data["action"]
    votes = symbol_data["votes"]
    last = symbol_data["last"]
    confidence = symbol_data["confidence"]
    df = symbol_data["df"]

    last_state = state.get("signals", {}).get(symbol)
    now = datetime.now(timezone.utc)
    can_send = True
    if not ALWAYS_SEND and last_state:
        try:
            last_sent = pd.to_datetime(last_state.get("time"))
            diff = (now - last_sent.to_pydatetime()).total_seconds()
            if diff < COOLDOWN_SECONDS:
                can_send=False
        except Exception:
            can_send=True
    if not can_send:
        return

    msg = (f"⚡ SIGNAL {interval}\n"
           f"Symbol: {symbol}\nAction: {action}\nConfidence: {confidence:.2f}\n"
           f"Patterns: {', '.join(votes)}\nPrice: {last['close']}")
    photo = plot_signal_candles(df, symbol, action, interval)
    send_telegram(msg, photo)

    state.setdefault("signals", {})[symbol]={"interval": interval,"action": action,
                                             "confidence": confidence,"time": now.isoformat(),
                                             "last_price": float(last["close"]),
                                             "spread_start": float(last["close"])}
    save_json_safe(STATE_FILE, state)

# ---------------- SPREAD TRACKER ----------------
def track_spreads():
    while True:
        for symbol, sig in state.get("signals", {}).items():
            start_price = sig.get("spread_start")
            if not start_price: continue
            current_df = fetch_klines_rest(symbol, "1m", 1)
            if current_df is None or current_df.empty: continue
            current_price = current_df["close"].iloc[-1]
            if sig["action"]=="LONG" and current_price>=start_price:
                elapsed = datetime.now(timezone.utc) - pd.to_datetime(sig["time"])
                send_telegram(f"✅ Spread for {symbol} LONG closed in {elapsed}")
                state["signals"][symbol]["spread_start"]=None
                save_json_safe(STATE_FILE, state)
            if sig["action"]=="SHORT" and current_price<=start_price:
                elapsed = datetime.now(timezone.utc) - pd.to_datetime(sig["time"])
                send_telegram(f"✅ Spread for {symbol} SHORT closed in {elapsed}")
                state["signals"][symbol]["spread_start"]=None
                save_json_safe(STATE_FILE, state)
        time.sleep(30)

# ---------------- MASTER SCAN ----------------
def scan_all():
    symbols = ALL_USDT
    results=[]
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
        for tf in SCAN_INTERVALS:
            res=list(exe.map(lambda s: analyze_symbol(s, tf), symbols))
            results.append(res)
    # --- пріоритет сигналів ---
    sent_symbols=set()
    for tf in SCAN_INTERVALS:
        for r in results[SCAN_INTERVALS.index(tf)]:
            if r is None: continue
            if r["action"]!="WATCH" and r["symbol"] not in sent_symbols:
                send_signal(r)
                sent_symbols.add(r["symbol"])
    state["last_scan"]=str(datetime.now(timezone.utc))
    save_json_safe(STATE_FILE, state)
    logger.info("Scan completed")

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({"status":"ok","time":str(datetime.now(timezone.utc)),
                    "signals":len(state.get("signals",{}))})

@app.route("/telegram_webhook/<token>", methods=["POST"])
def telegram_webhook(token):
    if token != TELEGRAM_TOKEN:
        return jsonify({"ok":False,"error":"invalid token"}),403
    update = request.get_json(force=True) or {}
    text = update.get("message",{}).get("text","").lower().strip()
    if text.startswith("/scan"):
        send_telegram("⚡ Manual scan started")
        Thread(target=scan_all, daemon=True).start()
    if text.startswith("/status"):
        send_telegram(f"Signals: {len(state.get('signals',{}))}")
    return jsonify({"ok":True})

# ---------------- MAIN ----------------
if __name__=="__main__":
    logger.info("Starting pre-top bot")
    Thread(target=scan_all, daemon=True).start()
    Thread(target=track_spreads, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)