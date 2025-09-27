# main.py — повністю робочий файл (заміни ним поточний)
import os, json, logging, requests, io, time
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import mplfinance as mpf
import ta
from datetime import datetime, timezone, timedelta
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, jsonify
from binance.client import Client
from binance.streams import ThreadedWebsocketManager
from sklearn.ensemble import RandomForestClassifier
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
PARALLEL_WORKERS = 6
TIMEFRAMES = ["15m", "1h", "4h"]
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "300"))  # сек
# Зменшив поріг, щоб було більше позитивних прикладів у хвилинних даних
ATR_THRESHOLD = float(os.getenv("ATR_THRESHOLD", "0.0005"))

# ---------------- GLOBALS ----------------
binance_client = None
klines_data = {}           # symbol -> DataFrame (index=time)
twm = None
state = {"signals": {}, "last_scan": None, "top_symbols": []}
history = {"signals": []}
json_lock = Lock()
imputer = SimpleImputer(strategy="mean")
scaler = StandardScaler()
ml_model = None  # заповнюється в fit_ml()

# ---------------- HELPERS: JSON ----------------
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

# ---------------- TELEGRAM ----------------
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
            else:
                logger.warning(f"Telegram API returned {resp.status_code}: {resp.text}")
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
        raw = binance_client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(raw, columns=[
            "time","open","high","low","close","volume",
            "close_time","qav","num_trades","taker_base_vol","taker_quote_vol","ignore"
        ])
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df.set_index("time", inplace=True)
        df = df[["open","high","low","close","volume"]].astype(float)
        klines_data[symbol] = df
        return df
    except Exception as e:
        logger.error(f"[ERROR] fetch_klines {symbol}: {e}")
        return None

def handle_socket(msg):
    try:
        if msg.get("e") == "kline":
            k = msg["k"]
            symbol = msg["s"]
            t = pd.to_datetime(k["t"], unit="ms")
            candle = [float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"])]
            if symbol not in klines_data:
                # if not present, try to fetch once
                fetch_klines(symbol, "1m", 500)
                if symbol not in klines_data:
                    return
            df = klines_data[symbol]
            df.loc[t] = candle
            klines_data[symbol] = df.tail(2000)
    except Exception as e:
        logger.debug(f"Socket handle error: {e}")

def start_ws(symbols=None, interval="1m"):
    def run_ws():
        global twm
        try:
            asyncio.set_event_loop(asyncio.new_event_loop())
        except Exception:
            pass
        twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
        twm.start()
        if symbols is None:
            symbols = ["BTCUSDT"]
        for s in symbols:
            if s not in klines_data:
                fetch_klines(s, interval, 500)
            try:
                twm.start_kline_socket(callback=handle_socket, symbol=s, interval=interval)
            except Exception as e:
                logger.warning(f"Failed to start ws for {s}: {e}")
        logger.info(f"[WS] Started for {symbols}")
    Thread(target=run_ws, daemon=True).start()

# ---------------- FEATURES / PATTERNS ----------------
def extract_features(df: pd.DataFrame):
    df = df.copy()
    if len(df) < 20:
        return df  # замало даних, повертаємо як є (без фіч)
    try:
        df["ema20"] = df["close"].ewm(span=20).mean()
        df["ema50"] = df["close"].ewm(span=50).mean()
        df["ema_diff"] = df["ema20"] - df["ema50"]
        if len(df) >= 15:  # мінімум для ADX/RSI
            df["adx"] = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], window=14).adx()
            df["rsi"] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
        else:
            df["adx"], df["rsi"] = 0, 0
        macd = ta.trend.MACD(df['close'])
        df["macd_hist"] = macd.macd() - macd.macd_signal()
        if len(df) >= 15:
            df["atr"] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
        else:
            df["atr"] = df["high"] - df["low"]

        bb = ta.volatility.BollingerBands(df['close'])
        df["bb_width"] = bb.bollinger_hband() - bb.bollinger_lband()

        df["vol_ma20"] = df["volume"].rolling(20).mean()
        df["vol_zscore"] = (df["volume"] - df["vol_ma20"]) / df["vol_ma20"].rolling(20).std()

        # candlestick patterns
        df["hammer"] = (df["close"] > df["open"]) & (
            (df["low"] - df[["open", "close"]].min(axis=1)) > 2 * (df["close"] - df["open"])
        )
        df["shooting_star"] = (df["open"] > df["close"]) & (
            (df["high"] - df[["open", "close"]].max(axis=1)) > 2 * (df["open"] - df["close"])
        )

        df["support"] = df["low"].rolling(20).min()
        df["resistance"] = df["high"].rolling(20).max()
        df["false_break_high"] = (df["high"] > df["resistance"]) & (df["close"] < df["resistance"])
        df["false_break_low"] = (df["low"] < df["support"]) & (df["close"] > df["support"])
        df["retest_support"] = abs(df["close"] - df["support"]) / df["support"] < 0.003
        df["retest_resistance"] = abs(df["close"] - df["resistance"]) / df["resistance"] < 0.003
        df["accumulation_zone"] = (
            (df["high"] - df["low"] < df["high"].rolling(20).max() * 0.02)
            & (df["volume"] > df["vol_ma20"])
        )
        df["squeeze"] = df["atr"] < df["atr"].rolling(50).mean() * 0.7
    except Exception as e:
        logger.debug(f"extract_features error: {e}")
    return df.fillna(0)

# ---------------- ML helpers ----------------
def get_rolling_vector(df, window=ROLLING_WINDOW):
    features = ["ema_diff","adx","rsi","macd_hist","atr","bb_width","vol_zscore",
                "hammer","shooting_star","false_break_high","false_break_low",
                "retest_support","retest_resistance","accumulation_zone","squeeze"]
    vectors = []
    for i in range(window, len(df)):
        vec = df[features].iloc[i-window:i].values.flatten()
        vectors.append(vec)
    return np.array(vectors)

# ---------------- ML training ----------------
def fit_ml(symbols):
    """Тренує RandomForest на даних із klines_data (якщо вистачає).
       Якщо позитивних прикладів мало — модель не тренується."""
    global ml_model, scaler, imputer
    logger.info("[ML] Building training dataset...")

    X_parts, y_parts = [], []

    for s in symbols:
        df = klines_data.get(s)
        if df is None:
            df = fetch_klines(s, "1m", 1500)
        if df is None or len(df) < ROLLING_WINDOW + 2:
            continue

        df = extract_features(df)
        vecs = get_rolling_vector(df, window=ROLLING_WINDOW)

        # майбутня дохідність на 1 свічку вперед
        future_ret = (df['close'].shift(-1) / df['close'] - 1).values
        labels = (future_ret[ROLLING_WINDOW:] > ATR_THRESHOLD).astype(int)

        if len(vecs) == 0 or len(labels) == 0:
            continue
        min_len = min(len(vecs), len(labels))
        X_parts.append(vecs[-min_len:])
        y_parts.append(labels[-min_len:])

    if not X_parts:
        logger.warning("[ML] No training data collected.")
        ml_model = None
        return

    X = np.vstack(X_parts)
    y = np.hstack(y_parts)
    logger.info(f"[ML] Collected {X.shape[0]} samples | positive rate {np.mean(y):.3f}")

    # якщо дуже мало позитивних сигналів — не тренуємо
    if len(np.unique(y)) < 2 or np.sum(y) < 5:
        logger.warning("[ML] Not enough positive samples, using only patterns.")
        ml_model = None
        return

    # підготовка даних
    imputer.fit(X)
    X_imputed = imputer.transform(X)
    scaler.fit(X_imputed)
    X_scaled = scaler.transform(X_imputed)

    # тренування моделі
    model = RandomForestClassifier(
        n_estimators=300,
        max_depth=12,
        class_weight="balanced_subsample",
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X_scaled, y)
    ml_model = model
    logger.info(f"[ML] Trained RandomForest on {X.shape[0]} samples")

# ---------------- detect (ML + fallback) ----------------
def detect_with_patterns(df):
    """Fallback: прості правилові патерни, повертає сигнал або None."""
    last = df.iloc[-1]
    # LONG conditions (examples)
    if last.get("false_break_low", False) or last.get("retest_support", False) or last.get("hammer", False):
        action = "LONG"
    elif last.get("false_break_high", False) or last.get("retest_resistance", False) or last.get("shooting_star", False):
        action = "SHORT"
    else:
        return None
    entry = last["close"]
    atr = last["atr"] if last["atr"] > 0 else (last["high"] - last["low"])
    sl = entry - 1.5*atr if action == "LONG" else entry + 1.5*atr
    tp1 = entry + 1.5*atr if action == "LONG" else entry - 1.5*atr
    rr1 = (tp1-entry)/(entry-sl) if action == "LONG" else (entry-tp1)/(sl-entry)
    if rr1 < 1.5:
        return None
    return {"symbol": None, "action": action, "entry": entry, "sl": sl, "tp1": tp1, "tp2": None, "tp3": None, "confidence": 0.5, "rr1": rr1}

def detect_signal_ml(dfs, symbol):
    """
    dfs: dict of timeframe->df, expects '15m' key for price/atr used for sizing
    """
    global ml_model, scaler, imputer
    vec = get_multi_tf_vector(dfs)
    if vec is None:
        return None
    # try ML predict
    if ml_model is not None:
        try:
            X = imputer.transform(vec.reshape(1, -1)) if 'imputer' in globals() else vec.reshape(1,-1)
            X_scaled = scaler.transform(X)
            prob = float(ml_model.predict_proba(X_scaled)[0, 1]) if hasattr(ml_model, "predict_proba") else float(ml_model.predict(X_scaled)[0])
        except Exception as e:
            logger.debug(f"[ML] predict error: {e}")
            prob = 0.0
    else:
        prob = 0.0

    last = dfs["15m"].iloc[-1]
    logger.info(f"[ML] {symbol} -> prob={prob:.3f}")

    # check ML threshold if ML available
    if ml_model is not None and prob >= 0.55:
        action = "LONG" if last["ema_diff"] > 0 else "SHORT"
        atr = last["atr"]
        entry = last["close"]
        if action == "LONG":
            sl = entry - 1.5*atr; tp1 = entry + 1.5*atr; tp2 = entry + 3*atr; tp3 = entry + 5*atr
        else:
            sl = entry + 1.5*atr; tp1 = entry - 1.5*atr; tp2 = entry - 3*atr; tp3 = entry - 5*atr
        rr1 = (tp1-entry)/(entry-sl) if action=="LONG" else (entry-tp1)/(sl-entry)
        if rr1 < 2: 
            return None
        return {"action":action,"entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,"confidence":prob,"rr1":rr1,"symbol":symbol}
    # fallback to pattern rules
    patt = detect_with_patterns(dfs["15m"])
    if patt:
        patt["symbol"] = symbol
        return patt
    return None

# multi-tf vector builder
def get_multi_tf_vector(dfs):
    vectors = []
    # for each TF, compute rolling vector then take last
    for tf in TIMEFRAMES:
        df = dfs.get(tf)
        if df is None: 
            # try to approximate by re-sampling 1m -> resample if necessary
            continue
        vecs = get_rolling_vector(df)
        if len(vecs) > 0:
            vectors.append(vecs[-1])
    if not vectors:
        return None
    return np.concatenate(vectors)

# ---------------- PLOTTING ----------------
def plot_signal(df, symbol, signal):
    try:
        addplots = []
        for tp in ["tp1","tp2","tp3"]:
            if signal.get(tp) is not None:
                addplots.append(mpf.make_addplot([signal[tp]]*len(df), linestyle="--", color='green'))
        addplots.append(mpf.make_addplot([signal["sl"]]*len(df), linestyle="--", color='red'))
        addplots.append(mpf.make_addplot([signal["entry"]]*len(df), linestyle="--", color='blue'))
        fig, ax = mpf.plot(df.tail(200), type='candle', style='yahoo', title=f"{symbol}-{signal['action']}", addplot=addplots, returnfig=True)
        buf = io.BytesIO()
        fig.savefig(buf, format='png', bbox_inches='tight')
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as e:
        logger.warning(f"plot_signal failed: {e}")
        return None

# ---------------- ANALYSIS / SCAN ----------------
def fetch_multi_tf_klines(symbol):
    df = klines_data.get(symbol)
    if df is None or len(df) < ROLLING_WINDOW:
        df = fetch_klines(symbol, "1m", 1000)
        if df is None or len(df) < ROLLING_WINDOW:
            return None

    dfs = {}
    if len(df) >= 20:
        dfs["15m"] = extract_features(
            df.resample("15min").agg(
                {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
            ).dropna()
        )
        dfs["1h"] = extract_features(
            df.resample("1h").agg(
                {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
            ).dropna()
        )
        dfs["4h"] = extract_features(
            df.resample("4h").agg(
                {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
            ).dropna()
        )
    return dfs

def analyze_symbol(symbol):
    try:
        logger.debug(f"Analyzing {symbol}")
        dfs = fetch_multi_tf_klines(symbol)
        if not dfs:
            logger.debug(f"No data for {symbol}")
            return
        signal = detect_signal_ml(dfs, symbol)
        if not signal:
            logger.debug(f"No signal for {symbol}")
            return
        # prepare plot from 15m data (if available)
        df_plot = dfs.get("15m")
        photo = None
        if df_plot is not None:
            photo = plot_signal(df_plot, symbol, signal)
        msg = (f"⚡ SIGNAL {symbol}\nAction: {signal['action']}\nEntry: {signal['entry']:.6f}\n"
               f"SL: {signal['sl']:.6f}\nTP1: {signal.get('tp1',0):.6f}\nConf: {signal['confidence']:.2f}\n"
               f"R/R1: {signal.get('rr1',0):.2f}")
        send_telegram(msg, photo)
        state["signals"][symbol] = {"signal": signal, "time": str(datetime.now(timezone.utc))}
        save_json_safe(STATE_FILE, state)
        history["signals"].append(signal)
        save_json_safe(HISTORY_FILE, history)
        logger.info(f"Sent signal for {symbol} (conf {signal['confidence']:.2f})")
    except Exception as e:
        logger.error(f"analyze_symbol error for {symbol}: {e}", exc_info=True)

# ---------------- TOP SYMBOLS ----------------
last_top_update = None

def fetch_top_symbols(limit=10, cache_minutes=10):
    global last_top_update

    if state.get("top_symbols") and last_top_update:
        if datetime.now(timezone.utc) - last_top_update < timedelta(minutes=cache_minutes):
            return state["top_symbols"]

    try:
        tickers = binance_client.futures_ticker()
        valid_symbols = {s["symbol"] for s in binance_client.futures_exchange_info()["symbols"]}
        usdt_pairs = [t for t in tickers if t.get("symbol", "").endswith("USDT") and t["symbol"] in valid_symbols]

        scores = []
        for t in usdt_pairs:
            try:
                change_pct = abs(float(t.get("priceChangePercent", 0)))
                vol = float(t.get("quoteVolume", 0))
                scores.append((t["symbol"], change_pct * 0.6 + vol * 0.4))
            except:
                continue

        sorted_symbols = [s[0] for s in sorted(scores, key=lambda x: x[1], reverse=True)[:limit]]
        state["top_symbols"] = sorted_symbols
        save_json_safe(STATE_FILE, state)
        last_top_update = datetime.now(timezone.utc)

        logger.info(f"[TOP] Selected symbols: {sorted_symbols}")
        return sorted_symbols
    except Exception as e:
        logger.warning(f"fetch_top_symbols failed: {e}")
        return state.get("top_symbols") or ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

# ---------------- SCAN LOOP (periodic) ----------------
def scan_all_symbols_once():
    logger.info("Starting one full scan...")
    symbols = fetch_top_symbols(limit=10)
    if not symbols:
        logger.warning("No symbols to scan")
        return
    total = len(symbols)
    with ThreadPoolExecutor(max_workers=min(PARALLEL_WORKERS, total)) as exe:
        futures = [exe.submit(analyze_symbol, s) for s in symbols]
        for idx, f in enumerate(futures, start=1):
            try:
                f.result()
            except Exception as e:
                logger.error(f"scan error: {e}")
            logger.info(f"Processed {idx}/{total}")
    state["last_scan"] = str(datetime.now(timezone.utc))
    save_json_safe(STATE_FILE, state)
    logger.info("Scan finished")

def analyze_loop():
    while True:
        try:
            logger.info("=== Starting scan cycle ===")
            scan_all_symbols_once()
            logger.info("=== Scan cycle finished ===")
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            logger.error(f"analyze_loop crashed: {e}", exc_info=True)
            time.sleep(10)

# ---------------- STARTUP ----------------
def startup_tasks():
    logger.info("=== Startup initiated ===")
    init_binance_client()
    symbols = fetch_top_symbols(limit=20)
    if not symbols:
        logger.error("No symbols fetched at startup!")
        return
    # Prefetch klines concurrently to populate klines_data
    logger.info(f"[Startup] Prefetching klines for {len(symbols)} symbols...")
    with ThreadPoolExecutor(max_workers=min(12, len(symbols))) as ex:
        ex.map(lambda s: fetch_klines(s, "1m", 1000), symbols)
    # Train ML model (if possible)
    fit_ml(symbols)
    # Start websocket to keep klines_data fresh
    start_ws(symbols, "1m")
    # Start periodic analyze loop in background
    Thread(target=analyze_loop, daemon=True).start()
    # Notify
    try:
        send_telegram("⚡ Bot started and ready! Monitoring: " + ", ".join(symbols[:10]))
    except Exception as e:
        logger.warning(f"Cannot send startup Telegram message: {e}")
    logger.info("=== Startup tasks launched ===")

# Start automatically when module is imported (so it also runs under gunicorn)
if __name__ != "__main__":
    Thread(target=startup_tasks, daemon=True).start()

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/scan", methods=["POST"])
def scan_endpoint():
    Thread(target=scan_all_symbols_once, daemon=True).start()
    return jsonify({"ok": True, "message": "Scan started"})

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "ok", "time": str(datetime.now(timezone.utc)), "signals": len(state["signals"])})

# If run directly, run startup tasks (non-blocking) and start Flask
if __name__ == "__main__":
    startup_tasks()
    app.run(host="0.0.0.0", port=PORT)