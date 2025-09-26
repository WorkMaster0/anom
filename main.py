import os, json, logging, requests, time
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import ta
from datetime import datetime, timezone
from threading import Thread, Lock
from flask import Flask, jsonify
from binance.client import Client
from binance.streams import ThreadedWebsocketManager
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.exceptions import NotFittedError

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ml-adaptive-bot")

# ---------------- CONFIG ----------------
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", 5000))
STATE_FILE = "state.json"
HISTORY_FILE = "history.json"
ROLLING_WINDOW = 10
SCAN_INTERVAL = 300
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

def send_telegram(msg, max_retries=3):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram token or chat_id not set, skipping send.")
        return False
    for attempt in range(1, max_retries + 1):
        try:
            payload = {"chat_id": CHAT_ID, "text": msg}
            resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                                 json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info(f"Telegram message sent (attempt {attempt})")
                return True
            else:
                logger.warning(f"Telegram error {resp.status_code}: {resp.text}")
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

import asyncio

def start_ws(symbols=None, interval="1m"):
    def run_ws():
        global twm
        asyncio.set_event_loop(asyncio.new_event_loop())  # <<< ДОДАНО
        twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
        twm.start()
        for s in symbols:
            if s not in klines_data:
                fetch_klines(s, interval, 500)
            twm.start_kline_socket(callback=handle_socket, symbol=s, interval=interval)
        logger.info(f"[WS] Started for {symbols}")
    Thread(target=run_ws, daemon=True).start()

# ---------------- FEATURES ----------------
def extract_features(df: pd.DataFrame):
    df = df.copy()
    df["ema20"] = df["close"].ewm(span=20).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()
    df["ema_diff"] = df["ema20"] - df["ema50"]
    df["adx"] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
    df["rsi"] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    macd = ta.trend.MACD(df['close'])
    df["macd_hist"] = macd.macd() - macd.macd_signal()
    df["atr"] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
    bb = ta.volatility.BollingerBands(df['close'])
    df["bb_width"] = bb.bollinger_hband() - bb.bollinger_lband()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_zscore"] = (df["volume"] - df["vol_ma20"]) / df["vol_ma20"].rolling(20).std()
    df = df.fillna(0)
    return df

# ---------------- ML ----------------
def fit_ml(symbols):
    all_X, all_y = [], []
    for s in symbols:
        df = fetch_klines(s, "1m", 500)
        if df is None or len(df) < ROLLING_WINDOW + 1:
            continue
        df = extract_features(df)
        df['future_return'] = df['close'].shift(-1)/df['close'] - 1
        df['label'] = (df['future_return'] > ATR_THRESHOLD).astype(int)
        vecs = df[["ema_diff","adx","rsi","macd_hist","atr","bb_width","vol_zscore"]].values
        labels = df['label'].values
        min_len = min(len(vecs), len(labels))
        all_X.append(vecs[-min_len:])
        all_y.append(labels[-min_len:])
    if all_X:
        X = np.vstack(all_X)
        y = np.hstack(all_y)
        X = imputer.fit_transform(X)
        scaler.fit(X)
        X_scaled = scaler.transform(X)
        ml_model.fit(X_scaled, y)
        logger.info(f"[ML] Trained on {X.shape[0]} samples")
    else:
        logger.warning("[ML] No data for training!")

# ---------------- SIGNAL ----------------
def detect_signal(symbol):
    df = fetch_klines(symbol, "1m", 500)
    if df is None:
        return None
    df = extract_features(df)
    vec = df[["ema_diff","adx","rsi","macd_hist","atr","bb_width","vol_zscore"]].iloc[-1].values.reshape(1, -1)
    vec_scaled = scaler.transform(imputer.transform(vec))
    prob = ml_model.predict_proba(vec_scaled)[0, 1]
    last = df.iloc[-1]

    # логування реальних ймовірностей
    logger.info(f"[ML] {symbol} -> prob={prob:.3f}")

    # ЗМЕНШЕНО поріг
    if prob < 0.55:
        return None

    action = "LONG" if last["ema_diff"] > 0 else "SHORT"
    entry = last["close"]
    atr = last["atr"]
    sl = entry - 1.5 * atr if action == "LONG" else entry + 1.5 * atr
    tp1 = entry + 1.5 * atr if action == "LONG" else entry - 1.5 * atr

    return {
        "symbol": symbol,
        "action": action,
        "entry": entry,
        "sl": sl,
        "tp1": tp1,
        "confidence": prob
    }

# ---------------- TOP SYMBOLS ----------------
def fetch_top_symbols(limit=50):
    try:
        tickers = binance_client.futures_ticker()
        usdt_pairs = [t for t in tickers if t.get("symbol", "").endswith("USDT")]
        valid_symbols = []
        for t in usdt_pairs:
            try:
                # Перевірка чи є реальні дані по символу
                binance_client.get_klines(symbol=t["symbol"], interval="1m", limit=1)
                valid_symbols.append(t)
            except:
                continue
        scores = []
        for t in valid_symbols:
            try:
                change_pct = abs(float(t.get("priceChangePercent", 0)))
                vol = float(t.get("quoteVolume", 0))
                score = change_pct * 0.6 + vol * 0.4
                scores.append((t["symbol"], score))
            except:
                continue
        sorted_symbols = [s[0] for s in sorted(scores, key=lambda x: x[1], reverse=True)[:limit]]
        return sorted_symbols
    except Exception as e:
        logger.warning(f"Failed fetch_top_symbols: {e}")
        return ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

# ---------------- ANALYZE LOOP ----------------
def analyze_loop():
    while True:
        try:
            logger.info("=== Starting scan cycle ===")
            symbols = fetch_top_symbols()
            for s in symbols:
                sig = detect_signal(s)
                if sig:
                    msg = f"⚡ Signal {sig['symbol']} {sig['action']} @ {sig['entry']:.2f} Conf: {sig['confidence']:.2f}"
                    send_telegram(msg)
                    logger.info(f"Signal found: {msg}")
                    state["signals"][s] = sig
                    history["signals"].append(sig)
                else:
                    logger.info(f"No signal for {s}")
            save_json_safe(STATE_FILE, state)
            save_json_safe(HISTORY_FILE, history)
            state["last_scan"] = str(datetime.now(timezone.utc))
            logger.info("=== Scan finished ===")
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            logger.error(f"Analyze loop crashed: {e}", exc_info=True)
            time.sleep(10)

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home_route():
    return jsonify({
        "status": "ok",
        "time": str(datetime.now(timezone.utc)),
        "signals": len(state["signals"])
    })

@app.route("/scan", methods=["POST"])
def scan_endpoint():
    Thread(target=analyze_loop, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan loop started"})


# ---------------- STARTUP ----------------
def startup():
    logger.info("=== Startup initiated ===")
    init_binance_client()
    symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
    fit_ml(symbols)
    start_ws(symbols)
    Thread(target=analyze_loop, daemon=True).start()
    send_telegram("⚡ Bot started and ready!")
    logger.info("Bot started and ready!")


# ВАЖЛИВО: викликаємо startup() тут, щоб воно працювало і під gunicorn
startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)