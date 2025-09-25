import os
import json
import logging
import requests
import io
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import mplfinance as mpf
import ta
from datetime import datetime, timezone, timedelta
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify
from binance.client import Client
from binance.streams import ThreadedWebsocketManager
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
import asyncio
import time

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ml-adaptive-bot")

# ---------------- CONFIG ----------------
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "5000"))
STATE_FILE = "state.json"
HISTORY_FILE = "history.json"
ROLLING_WINDOW = 10
PARALLEL_WORKERS = 4
TIMEFRAMES = ["15m", "1h", "4h"]

# ---------------- CLIENT ----------------
binance_client = None
def init_binance_client():
    global binance_client
    if binance_client is None:
        binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
        logger.info("Binance client initialized")
    return binance_client

# ---------------- GLOBALS ----------------
klines_data = {}
twm = None
state = {"signals": {}, "last_scan": None, "top_symbols": []}
history = {"signals": []}

# ---------------- UTILS ----------------
def load_json_safe(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load {path}: {e}")
    return default

def save_json_safe(path, data):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"Failed to save {path}: {e}")

state = load_json_safe(STATE_FILE, state)
history = load_json_safe(HISTORY_FILE, history)

def escape_md_v2(text):
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    for c in escape_chars:
        text = text.replace(c, f"\\{c}")
    return text

def send_telegram(msg, photo=None, max_retries=3):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram token or chat_id not set, skipping send.")
        return False
    for attempt in range(1, max_retries + 1):
        try:
            if photo:
                files = {'photo': ('signal.png', photo, 'image/png')}
                data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(msg), 'parse_mode': 'MarkdownV2'}
                resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",
                                     data=data, files=files, timeout=10)
            else:
                payload = {"chat_id": CHAT_ID, "text": escape_md_v2(msg), "parse_mode": "MarkdownV2"}
                resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                     json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info(f"Telegram message sent successfully (attempt {attempt})")
                return True
        except Exception as e:
            logger.warning(f"Telegram send exception (attempt {attempt}): {e}")
    logger.error("Telegram send failed after max retries")
    return False

# ---------------- KLINES ----------------
def fetch_klines(symbol="BTCUSDT", interval="1m", limit=500):
    try:
        df = pd.DataFrame(binance_client.get_klines(symbol=symbol, interval=interval, limit=limit),
                          columns=["time","open","high","low","close","volume",
                                   "close_time","qav","num_trades","taker_base_vol","taker_quote_vol","ignore"])
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df.set_index("time", inplace=True)
        df = df[["open","high","low","close","volume"]].astype(float)
        klines_data[symbol] = df
        return df
    except Exception as e:
        logger.error(f"[ERROR] fetch_klines {symbol}: {e}")
        return None

def handle_socket(msg):
    if msg.get("e") == "kline":
        k = msg["k"]
        symbol = msg["s"]
        t = pd.to_datetime(k["t"], unit="ms")
        candle = [float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])]
        if symbol not in klines_data: return
        df = klines_data[symbol]
        df.loc[t] = candle
        klines_data[symbol] = df.tail(1000)

def start_ws(symbols=None, interval="1m"):
    global twm
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    twm.start()
    if symbols is None:
        symbols = ["BTCUSDT"]
    for s in symbols:
        if s not in klines_data:
            fetch_klines(s, interval, 500)
        twm.start_kline_socket(callback=handle_socket, symbol=s, interval=interval)
    logger.info(f"[WS] Started for {symbols}")

def get_latest_df(symbol):
    return klines_data.get(symbol) or fetch_klines(symbol)

# ---------------- FEATURES ----------------
def extract_features(df: pd.DataFrame):
    df = df.copy()
    df["ema20"] = df["close"].ewm(span=20).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()
    df["ema_diff"] = df["ema20"] - df["ema50"]
    df["adx"] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
    df["rsi"] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    macd = ta.trend.MACD(df['close'])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    df["atr"] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
    bb = ta.volatility.BollingerBands(df['close'])
    df["bb_width"] = bb.bollinger_hband() - bb.bollinger_lband()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_zscore"] = (df["volume"] - df["vol_ma20"]) / df["vol_ma20"].rolling(20).std()
    df["hammer"] = (df["close"] > df["open"]) & ((df["low"] - df[["open","close"]].min(axis=1)) > 2*(df["close"] - df["open"]))
    df["shooting_star"] = (df["open"] > df["close"]) & ((df["high"] - df[["open","close"]].max(axis=1)) > 2*(df["open"] - df["close"]))
    df["support"] = df["low"].rolling(20).min()
    df["resistance"] = df["high"].rolling(20).max()
    df["false_break_high"] = (df["high"] > df["resistance"]) & (df["close"] < df["resistance"])
    df["false_break_low"] = (df["low"] < df["support"]) & (df["close"] > df["support"])
    df["retest_support"] = abs(df["close"] - df["support"]) / df["support"] < 0.003
    df["retest_resistance"] = abs(df["close"] - df["resistance"]) / df["resistance"] < 0.003
    df["accumulation_zone"] = (df["high"] - df["low"] < df["high"].rolling(20).max()*0.02) & (df["volume"] > df["vol_ma20"])
    df["squeeze"] = df["atr"] < df["atr"].rolling(50).mean()*0.7
    return df

def get_rolling_vector(df, window=ROLLING_WINDOW):
    features = ["ema_diff","adx","rsi","macd_hist","atr","bb_width","vol_zscore",
                "hammer","shooting_star","false_break_high","false_break_low",
                "retest_support","retest_resistance","accumulation_zone","squeeze"]
    vectors = []
    for i in range(window, len(df)):
        vec = df[features].iloc[i-window:i].values.flatten()
        vectors.append(vec)
    return np.array(vectors)

# ---------------- ML ----------------
scaler = StandardScaler()
ml_model = LogisticRegression()

def get_multi_tf_vector(dfs):
    vectors = []
    for tf in TIMEFRAMES:
        df = dfs.get(tf)
        if df is None: continue
        vec = get_rolling_vector(df)
        if len(vec) > 0:
            vectors.append(vec[-1])
    if vectors:
        return np.concatenate(vectors)
    return None

def fit_scaler_and_model(symbols):
    all_data = []
    for s in symbols:
        df = fetch_klines(s, "1m", 500)
        if df is None or len(df) < ROLLING_WINDOW:
            continue
        dfs = {tf: extract_features(df) for tf in TIMEFRAMES}
        vec = get_multi_tf_vector(dfs)
        if vec is not None:
            all_data.append(vec)
    if all_data:
        combined = np.vstack(all_data)
        scaler.fit(combined)
        ml_model.fit(combined, np.zeros(combined.shape[0]))
        logger.info(f"[Scaler+ML] Fitted on {combined.shape[0]} samples with {combined.shape[1]} features")
    else:
        logger.warning("[Scaler+ML] No data to fit scaler/model!")

def fetch_multi_tf_klines(symbol):
    dfs = {}
    df = klines_data.get(symbol)
    if df is None or len(df) < ROLLING_WINDOW:
        return None
    for tf in TIMEFRAMES:
        dfs[tf] = extract_features(df)
    return dfs

def detect_signal_ml(dfs, symbol):
    vec = get_multi_tf_vector(dfs)
    if vec is None: return None
    vec_scaled = scaler.transform(vec.reshape(1,-1))
    prob = ml_model.predict_proba(vec_scaled)[0,1] if hasattr(ml_model,"predict_proba") else 0.8
    last = dfs["15m"].iloc[-1]
    if prob < 0.7: return None
    action = "LONG" if last["ema_diff"] > 0 else "SHORT"
    atr = last["atr"]
    entry = last["close"]
    if action == "LONG":
        sl = entry - 1.5*atr; tp1=entry+1.5*atr; tp2=entry+3*atr; tp3=entry+5*atr
    else:
        sl = entry + 1.5*atr; tp1=entry-1.5*atr; tp2=entry-3*atr; tp3=entry-5*atr
    rr1 = (tp1-entry)/(entry-sl) if action=="LONG" else (entry-tp1)/(sl-entry)
    if rr1<2: return None
    return {"action":action,"entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,
            "confidence":prob,"rr1":rr1,"features":vec.tolist(),"symbol":symbol}

# ---------------- SIGNAL PLOTTING ----------------
def plot_signal(df,symbol,signal):
    addplots=[]
    for tp in ["tp1","tp2","tp3"]:
        addplots.append(mpf.make_addplot([signal[tp]]*len(df), linestyle="--", color='green'))
    addplots.append(mpf.make_addplot([signal["sl"]]*len(df), linestyle="--", color='red'))
    addplots.append(mpf.make_addplot([signal["entry"]]*len(df), linestyle="--", color='blue'))
    fig, ax = mpf.plot(df.tail(200), type='candle', style='yahoo',
                    title=f"{symbol}-{signal['action']}", addplot=addplots, returnfig=True)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf

# ---------------- ANALYSIS ----------------
def analyze_symbol(symbol):
    logger.info(f"Analyzing symbol: {symbol}")
    dfs = fetch_multi_tf_klines(symbol)
    if not dfs:
        logger.info(f"No data available for {symbol}")
        return
    signal = detect_signal_ml(dfs, symbol)
    if not signal:
        logger.info(f"No signal generated for {symbol}")
        return
    try:
        photo = plot_signal(dfs["15m"], symbol, signal)
    except Exception as e:
        logger.warning(f"Failed to plot signal for {symbol}: {e}")
        photo = None
    msg = (
        f"⚡ TRADE SIGNAL\n"
        f"Symbol: {symbol}\n"
        f"Action: {signal['action']}\n"
        f"Entry: {signal['entry']:.6f}\n"
        f"SL: {signal['sl']:.6f}\n"
        f"TP1: {signal['tp1']:.6f}\n"
        f"Confidence: {signal['confidence']:.2f}\n"
        f"R/R1: {signal['rr1']:.2f}\n"
        f"(Timeframes: {', '.join(dfs.keys())})"
    )
    send_telegram(msg, photo)
    state["signals"][symbol] = {"signal": signal, "time": str(datetime.now(timezone.utc))}
    save_json_safe(STATE_FILE, state)
    history["signals"].append(signal)
    save_json_safe(HISTORY_FILE, history)

# ---------------- TOP SYMBOLS ----------------
last_top_update = None
def fetch_top_symbols(limit=10, cache_minutes=10):
    global last_top_update
    if state.get("top_symbols") and last_top_update:
        if datetime.now(timezone.utc) - last_top_update < timedelta(minutes=cache_minutes):
            return state["top_symbols"]
    try:
        tickers = binance_client.futures_ticker()
        usdt_pairs = [t for t in tickers if t.get("symbol","").endswith("USDT")]
        scores = []
        for t in usdt_pairs:
            try:
                change_pct = abs(float(t.get("priceChangePercent",0)))
                vol = float(t.get("quoteVolume",0))
                score = change_pct*0.6 + vol*0.4
                scores.append((t["symbol"], score))
            except Exception: continue
        if not scores: return ["BTCUSDT","ETHUSDT","BNBUSDT"]
        sorted_symbols = [s[0] for s in sorted(scores, key=lambda x:x[1], reverse=True)[:limit]]
        state["top_symbols"] = sorted_symbols
        save_json_safe(STATE_FILE, state)
        last_top_update = datetime.now(timezone.utc)
        return sorted_symbols
    except Exception as e:
        logger.warning(f"Failed to fetch top symbols: {e}")
        return state.get("top_symbols") or ["BTCUSDT","ETHUSDT","BNBUSDT"]

# ---------------- MASTER SCAN ----------------
def scan_all_symbols():
    logger.info("Starting full symbols scan...")
    symbols = fetch_top_symbols()
    if not symbols:
        logger.warning("No symbols to scan")
        return
    total_symbols = len(symbols)
    logger.info(f"Symbols to scan: {total_symbols} -> {symbols}")
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
        futures = [exe.submit(analyze_symbol, s) for s in symbols]
        for idx, f in enumerate(futures, start=1):
            try:
                f.result()
            except Exception as e:
                logger.error(f"Error analyzing symbol: {e}")
            logger.info(f"Processed {idx}/{total_symbols} symbols")
            time.sleep(0.2)
    state["last_scan"] = str(datetime.now(timezone.utc))
    save_json_safe(STATE_FILE, state)
    logger.info("Full symbols scan completed")

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/scan", methods=["POST"])
def scan_endpoint():
    Thread(target=scan_all_symbols, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan started"})

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status":"ok", "time":str(datetime.now(timezone.utc)), "signals":len(state["signals"])})

# ---------------- STARTUP ----------------
def startup_tasks():
    init_binance_client()
    symbols = fetch_top_symbols(limit=10)
    if not symbols:
        logger.error("No symbols fetched at startup!")
        return
    fit_scaler_and_model(symbols)
    start_ws(symbols, "1m")
    try:
        send_telegram("⚡ Bot started and ready! Monitoring symbols: " + ", ".join(symbols))
    except Exception as e:
        logger.warning(f"Cannot send startup Telegram message: {e}")
    scan_all_symbols()

if os.getenv("RENDER", "false").lower() == "true":
    logger.info("🔥 Running on Render, launching startup tasks in background...")
    Thread(target=startup_tasks, daemon=True).start()

if __name__ == "__main__":
    logger.info("Starting bot locally...")
    startup_tasks()
    app.run(host="0.0.0.0", port=PORT)