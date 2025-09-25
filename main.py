import os, json, logging, requests, io, time
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import mplfinance as mpf
import ta
from datetime import datetime, timezone, timedelta
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify
from binance.client import Client
from binance.streams import ThreadedWebsocketManager
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
import asyncio

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
TIMEFRAMES = ["15m","1h","4h"]
ATR_THRESHOLD = 0.002

# ---------------- GLOBALS ----------------
binance_client = None
klines_data = {}
twm = None
state = {"signals": {}, "last_scan": None, "top_symbols": []}
history = {"signals": []}
json_lock = Lock()
imputer = SimpleImputer(strategy='mean')
scaler = StandardScaler()
ml_model = LogisticRegression()

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
        with json_lock:
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

# ---------------- BINANCE ----------------
def init_binance_client():
    global binance_client
    if binance_client is None:
        binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
        logger.info("Binance client initialized")
    return binance_client

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
        logger.warning(f"[ERROR] fetch_klines {symbol}: {e}")
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
    vectors = np.array(vectors)
    vectors = vectors[~np.isnan(vectors).any(axis=1)]
    return vectors

# ---------------- ML ----------------
def generate_labels(df: pd.DataFrame):
    df = df.copy()
    df['future_return'] = df['close'].shift(-1)/df['close'] - 1
    df['label'] = (df['future_return'] > ATR_THRESHOLD).astype(int)
    return df

def fit_scaler_and_model(symbols):
    all_X, all_y = [], []
    valid_symbols = []
    for s in symbols:
        df = fetch_klines(s, "1m", 1000)
        if df is None or len(df) < ROLLING_WINDOW + 1:
            continue
        df = extract_features(df)
        df = generate_labels(df)
        vecs = get_rolling_vector(df)
        labels = df['label'].iloc[ROLLING_WINDOW:].values
        min_len = min(len(vecs), len(labels))
        if min_len == 0: 
            continue
        vecs = vecs[-min_len:]
        labels = labels[-min_len:]
        all_X.append(vecs)
        all_y.append(labels)
        valid_symbols.append(s)
    if all_X:
        X = np.vstack(all_X)
        y = np.hstack(all_y)
        X = imputer.fit_transform(X)
        scaler.fit(X)
        X_scaled = scaler.transform(X)
        ml_model.fit(X_scaled, y)
        logger.info(f"[ML] Trained on {X.shape[0]} samples for symbols: {valid_symbols}")
    else:
        logger.warning("[ML] Not enough data to train model")

# ---------------- SIGNAL ----------------
def detect_signal(symbol):
    df = get_latest_df(symbol)
    if df is None or len(df) < ROLLING_WINDOW:
        return None
    df = extract_features(df)
    vecs = get_rolling_vector(df)
    if len(vecs) == 0:
        return None
    last_vec = vecs[-1].reshape(1,-1)
    last_vec_scaled = scaler.transform(last_vec)
    prob = ml_model.predict_proba(last_vec_scaled)[0,1]
    last = df.iloc[-1]
    if prob < 0.7:
        return None
    action = "LONG" if last["ema_diff"] > 0 else "SHORT"
    entry = last["close"]
    atr = last["atr"]
    sl = entry - 1.5*atr if action=="LONG" else entry + 1.5*atr
    tp1 = entry + 1.5*atr if action=="LONG" else entry - 1.5*atr
    tp2 = entry + 3*atr if action=="LONG" else entry - 3*atr
    tp3 = entry + 5*atr if action=="LONG" else entry - 5*atr
    return {"symbol":symbol,"action":action,"entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"confidence":prob}

def plot_signal(df, signal):
    addplots=[]
    for tp in ["tp1","tp2","tp3"]:
        addplots.append(mpf.make_addplot([signal[tp]]*len(df), linestyle="--", color='green'))
    addplots.append(mpf.make_addplot([signal["sl"]]*len(df), linestyle="--", color='red'))
    addplots.append(mpf.make_addplot([signal["entry"]]*len(df), linestyle="--", color='blue'))
    fig, ax = mpf.plot(df.tail(200), type='candle', style='yahoo', addplot=addplots, returnfig=True)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf

def analyze_all_symbols():
    symbols = ["BTCUSDT","ETHUSDT","BNBUSDT"]
    for s in symbols:
        signal = detect_signal(s)
        if signal:
            df = get_latest_df(s)
            photo = plot_signal(df, signal)
            msg = f"⚡ Signal {signal['symbol']}\nAction: {signal['action']}\nEntry: {signal['entry']:.2f}\nSL: {signal['sl']:.2f}\nTP1: {signal['tp1']:.2f}\nConf: {signal['confidence']:.2f}"
            send_telegram(msg, photo)
            state["signals"][s] = signal
            history["signals"].append(signal)
    save_json_safe(STATE_FILE, state)
    save_json_safe(HISTORY_FILE, history)

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status":"ok", "time":str(datetime.now(timezone.utc)), "signals":len(state["signals"])})

@app.route("/scan", methods=["POST"])
def scan_endpoint():
    Thread(target=analyze_all_symbols, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan started"})

# ---------------- STARTUP ----------------
def startup_tasks():
    init_binance_client()
    symbols = ["BTCUSDT","ETHUSDT","BNBUSDT"]
    fit_scaler_and_model(symbols)
    start_ws(symbols, "1m")
    send_telegram("⚡ Bot started and ready! Monitoring symbols: " + ", ".join(symbols))
    Thread(target=analyze_all_symbols, daemon=True).start()

if __name__ == "__main__":
    logger.info("Starting bot...")
    startup_tasks()
    app.run(host="0.0.0.0", port=PORT)