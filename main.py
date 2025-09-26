# main.py
import os, json, logging, requests, time
import pandas as pd
import numpy as np
from threading import Thread, Lock
from flask import Flask, jsonify
from binance.client import Client
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ml-bot")

# ---------------- CONFIG ----------------
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", 5000))
STATE_FILE = "state.json"
HISTORY_FILE = "history.json"
SCAN_INTERVAL = 300  # 5 хв
ATR_THRESHOLD = 0.002

# ---------------- GLOBALS ----------------
binance_client = None
klines_data = {}
state = {"signals": {}, "last_scan": None}
history = {"signals": []}
lock = Lock()
imputer = SimpleImputer(strategy="mean")
scaler = StandardScaler()
ml_model = LogisticRegression()

# ---------------- UTILS ----------------
def load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed load {path}: {e}")
    return default

def save_json(path, data):
    try:
        with lock:
            with open(path + ".tmp", "w") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(path + ".tmp", path)
    except Exception as e:
        logger.warning(f"Failed save {path}: {e}")

state = load_json(STATE_FILE, state)
history = load_json(HISTORY_FILE, history)

def send_telegram(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured")
        return False
    try:
        payload = {"chat_id": CHAT_ID, "text": msg}
        resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                             json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info("Telegram message sent successfully")
            return True
        else:
            logger.error(f"Telegram failed {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Telegram exception: {e}")
        return False

# ---------------- BINANCE ----------------
def init_binance():
    global binance_client
    if not binance_client:
        binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
        logger.info("Binance client initialized")

def fetch_klines(symbol="BTCUSDT", interval="1m", limit=200):
    try:
        df = pd.DataFrame(binance_client.get_klines(symbol=symbol, interval=interval, limit=limit),
                          columns=["time","open","high","low","close","volume","close_time",
                                   "qav","num_trades","taker_base_vol","taker_quote_vol","ignore"])
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df.set_index("time", inplace=True)
        df = df[["open","high","low","close","volume"]].astype(float)
        klines_data[symbol] = df
        return df
    except Exception as e:
        logger.error(f"fetch_klines failed {symbol}: {e}")
        return None

# ---------------- FEATURES & ML ----------------
def extract_features(df):
    df = df.copy()
    df["ema20"] = df["close"].ewm(span=20).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()
    df["ema_diff"] = df["ema20"] - df["ema50"]
    df = df.fillna(0)
    return df

def fit_ml(symbols=["BTCUSDT","ETHUSDT","BNBUSDT"]):
    X_all, y_all = [], []
    for s in symbols:
        df = fetch_klines(s)
        if df is None or len(df) < 50:
            continue
        df = extract_features(df)
        df["future"] = df["close"].shift(-1)/df["close"] - 1
        df["label"] = (df["future"] > ATR_THRESHOLD).astype(int)
        X_all.append(df[["ema_diff"]].iloc[:-1].values)
        y_all.append(df["label"].iloc[:-1].values)
    if X_all:
        X = np.vstack(X_all)
        y = np.hstack(y_all)
        X = imputer.fit_transform(X)
        scaler.fit(X)
        ml_model.fit(scaler.transform(X), y)
        logger.info(f"[ML] Trained on {X.shape[0]} samples")
    else:
        logger.warning("[ML] No data to train")

def detect_signal(symbol):
    logger.info(f"Checking {symbol}")
    df = fetch_klines(symbol)
    if df is None: 
        return None
    df = extract_features(df)
    vec = df[["ema_diff"]].iloc[-1].values.reshape(1,-1)
    vec_scaled = scaler.transform(imputer.transform(vec))
    prob = ml_model.predict_proba(vec_scaled)[0,1]
    last = df.iloc[-1]
    if prob < 0.6:
        return None
    action = "LONG" if last["ema_diff"] > 0 else "SHORT"
    return {"symbol": symbol, "action": action, "entry": last["close"], "confidence": prob}

# ---------------- LOOP ----------------
def analyze_loop():
    while True:
        logger.info("=== Starting scan cycle ===")
        symbols = ["BTCUSDT","ETHUSDT","BNBUSDT"]
        for s in symbols:
            sig = detect_signal(s)
            if sig:
                logger.info(f"Signal found: {sig['symbol']} {sig['action']} @ {sig['entry']:.2f} (Conf: {sig['confidence']:.2f})")
                send_telegram(f"⚡ {sig['symbol']} {sig['action']} @ {sig['entry']:.2f} (Conf: {sig['confidence']:.2f})")
                state["signals"][s] = sig
                history["signals"].append(sig)
        state["last_scan"] = str(pd.Timestamp.utcnow())
        save_json(STATE_FILE, state)
        save_json(HISTORY_FILE, history)
        logger.info(f"=== Scan finished. Sleeping {SCAN_INTERVAL}s ===")
        time.sleep(SCAN_INTERVAL)

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({"status": "ok", "signals": len(state["signals"])})

@app.route("/scan", methods=["POST"])
def scan():
    Thread(target=analyze_loop, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan loop started"})

# ---------------- STARTUP ----------------
def startup():
    init_binance()
    fit_ml()
    Thread(target=analyze_loop, daemon=True).start()
    send_telegram("⚡ Bot started and ready!")
    logger.info("Bot started and ready!")

if __name__ == "__main__":
    startup()
    app.run(host="0.0.0.0", port=PORT)