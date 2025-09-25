import os, json, logging, requests, io
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import mplfinance as mpf
import ta
from datetime import datetime, timezone, timedelta
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
from binance.client import Client
from binance.streams import ThreadedWebsocketManager
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

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
SCAN_INTERVAL = 60
TOP_SYMBOL_LIMIT = 30
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "8"))
PARALLEL_WORKERS = 4
TIMEFRAMES = ["15m","1h","4h"]

# ---------------- CLIENTS ----------------
binance_client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
state = {"signals": {}, "last_scan": None, "top_symbols": []}
history = {"signals": []}

# ---------------- GLOBAL WS ----------------
klines_data = {}
twm = None

# ---------------- UTIL ----------------
def load_json_safe(path, default):
    try:
        if os.path.exists(path):
            with open(path,"r") as f: return json.load(f)
    except: pass
    return default

def save_json_safe(path, data):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path+".tmp"
        with open(tmp,"w") as f: json.dump(data,f,indent=2,default=str)
        os.replace(tmp,path)
    except: pass

state = load_json_safe(STATE_FILE,state)
history = load_json_safe(HISTORY_FILE,history)

def escape_md_v2(text:str)->str:
    for c in "_*[]()~`>#+-=|{}.!":
        text = text.replace(c,"\\"+c)
    return text

def send_telegram(msg,photo=None):
    if not TELEGRAM_TOKEN or not CHAT_ID: return
    try:
        if photo:
            files={'photo':('signal.png',photo,'image/png')}
            data={'chat_id':CHAT_ID,'caption':escape_md_v2(msg),'parse_mode':'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto",data=data,files=files,timeout=10)
        else:
            payload={"chat_id":CHAT_ID,"text":escape_md_v2(msg),"parse_mode":"MarkdownV2"}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",json=payload,timeout=10)
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")

# ---------------- WS + REST DATA ----------------
def fetch_klines(symbol="BTCUSDT", interval="1m", limit=500):
    """
    Отримати історичні свічки через REST (для старту)
    """
    try:
        raw = binance_client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(raw, columns=[
            "time", "open", "high", "low", "close", "volume",
            "close_time", "qav", "num_trades", "taker_base_vol", "taker_quote_vol", "ignore"
        ])
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df.set_index("time", inplace=True)
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        klines_data[symbol] = df
        return df
    except Exception as e:
        logger.error(f"[ERROR] fetch_klines {symbol}: {e}")
        return None

def handle_socket(msg):
    """
    Обробка нових свічок з WebSocket
    """
    if msg["e"] == "kline":
        k = msg["k"]
        symbol = msg["s"]
        candle_time = pd.to_datetime(k["t"], unit="ms")
        candle = {
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }

        if symbol not in klines_data:
            return

        df = klines_data[symbol]
        df.loc[candle_time] = [candle["open"], candle["high"], candle["low"], candle["close"], candle["volume"]]
        klines_data[symbol] = df.tail(1000)

def start_ws(symbols=None, interval="1m"):
    """
    Запускає WebSocket для списку символів
    """
    global twm
    twm = ThreadedWebsocketManager(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
    twm.start()

    if symbols is None:
        symbols = ["BTCUSDT", "ETHUSDT"]

    for s in symbols:
        fetch_klines(s, interval, 500)
        twm.start_kline_socket(callback=handle_socket, symbol=s, interval=interval)

    logger.info(f"[WS] Started for {symbols}")

def get_latest_df(symbol):
    """
    Отримати актуальний DataFrame по символу (оновлений через WebSocket або REST)
    """
    return klines_data.get(symbol) or fetch_klines(symbol)

# ---------------- FEATURES ----------------
def extract_features(df:pd.DataFrame):
    df=df.copy()
    df["ema20"]=df["close"].ewm(span=20).mean()
    df["ema50"]=df["close"].ewm(span=50).mean()
    df["ema_diff"]=df["ema20"]-df["ema50"]
    df["adx"]=ta.trend.ADXIndicator(df['high'],df['low'],df['close'],window=14).adx()
    df["rsi"]=ta.momentum.RSIIndicator(df['close'],window=14).rsi()
    macd=ta.trend.MACD(df['close'])
    df["macd"]=macd.macd()
    df["macd_signal"]=macd.macd_signal()
    df["macd_hist"]=df["macd"]-df["macd_signal"]
    df["atr"]=ta.volatility.AverageTrueRange(df['high'],df['low'],df['close'],window=14).average_true_range()
    bb=ta.volatility.BollingerBands(df['close'])
    df["bb_width"]=bb.bollinger_hband()-bb.bollinger_lband()
    df["vol_ma20"]=df["volume"].rolling(20).mean()
    df["vol_zscore"]=(df["volume"]-df["vol_ma20"])/df["vol_ma20"].rolling(20).std()
    df["hammer"]=(df["close"]>df["open"])&((df["low"]-df[["open","close"]].min(axis=1))>2*(df["close"]-df["open"]))
    df["shooting_star"]=(df["open"]>df["close"])&((df["high"]-df[["open","close"]].max(axis=1))>2*(df["open"]-df["close"]))
    df["support"]=df["low"].rolling(20).min()
    df["resistance"]=df["high"].rolling(20).max()
    df["false_break_high"]=(df["high"]>df["resistance"])&(df["close"]<df["resistance"])
    df["false_break_low"]=(df["low"]<df["support"])&(df["close"]>df["support"])
    df["retest_support"]=abs(df["close"]-df["support"])/df["support"]<0.003
    df["retest_resistance"]=abs(df["close"]-df["resistance"])/df["resistance"]<0.003
    df["accumulation_zone"]=(df["high"]-df["low"]<df["high"].rolling(20).max()*0.02)&(df["volume"]>df["vol_ma20"])
    df["squeeze"]=df["atr"]<df["atr"].rolling(50).mean()*0.7
    return df

def get_rolling_vector(df,window=ROLLING_WINDOW):
    features=["ema_diff","adx","rsi","macd_hist","atr","bb_width","vol_zscore","hammer","shooting_star",
              "false_break_high","false_break_low","retest_support","retest_resistance","accumulation_zone","squeeze"]
    vectors=[]
    for i in range(window,len(df)):
        vec=df[features].iloc[i-window:i].values.flatten()
        vectors.append(vec)
    return np.array(vectors)

# ---------------- ML ----------------
scaler=StandardScaler()
ml_model=LogisticRegression()

def train_ml_model():
    X, y = [], []
    for sig in history.get("signals", []):
        if "success" in sig:
            X.append(sig["features"])
            y.append(sig["success"])
    if not X: return
    X_scaled = scaler.fit_transform(X)
    ml_model.fit(X_scaled, y)
    logger.info("ML model retrained on %d signals", len(X))

# ---------------- MULTI-TF ----------------
def fetch_multi_tf_klines(symbol, limit=500):
    dfs = {}
    for tf in TIMEFRAMES:
        df = get_latest_df(symbol)
        if df is not None and len(df) >= ROLLING_WINDOW:
            dfs[tf] = extract_features(df)
    return dfs if dfs else None

def get_multi_tf_vector(dfs):
    vectors = []
    for tf in TIMEFRAMES:
        df = dfs.get(tf)
        if df is None: continue
        vec = get_rolling_vector(df)[-1].flatten()
        vectors.append(vec)
    if vectors:
        return np.concatenate(vectors)
    return None

# ---------------- SIGNAL GENERATION ----------------
def detect_signal_ml(dfs,symbol):
    vec = get_multi_tf_vector(dfs)
    if vec is None: return None
    vec_scaled = scaler.transform(vec.reshape(1,-1)) if hasattr(scaler,"transform") else vec.reshape(1,-1)
    prob = ml_model.predict_proba(vec_scaled)[0,1] if hasattr(ml_model,"predict_proba") else 0.8
    last = dfs["15m"].iloc[-1]
    if prob < 0.7: return None
    action = "LONG" if last["ema_diff"]>0 else "SHORT"
    atr = last["atr"]
    entry = last["close"]
    if action=="LONG":
        sl = entry-1.5*atr; tp1 = entry+1.5*atr; tp2 = entry+3*atr; tp3 = entry+5*atr
    else:
        sl = entry+1.5*atr; tp1 = entry-1.5*atr; tp2 = entry-3*atr; tp3 = entry-5*atr
    rr1 = (tp1-entry)/(entry-sl) if action=="LONG" else (entry-tp1)/(sl-entry)
    if rr1<2: return None
    return {"action":action,"entry":entry,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,
            "confidence":prob,"rr1":rr1,"features":vec.tolist(),"symbol":symbol}

# ---------------- PLOTTING ----------------
def plot_signal(df,symbol,signal):
    addplots=[]
    for tp in ["tp1","tp2","tp3"]:
        addplots.append(mpf.make_addplot([signal[tp]]*len(df),linestyle="--",color='green'))
    addplots.append(mpf.make_addplot([signal["sl"]]*len(df),linestyle="--",color='red'))
    addplots.append(mpf.make_addplot([signal["entry"]]*len(df),linestyle="--",color='blue'))
    fig,ax=mpf.plot(df.tail(200),type='candle',style='yahoo',title=f"{symbol}-{signal['action']}",addplot=addplots,returnfig=True)
    buf=io.BytesIO(); fig.savefig(buf,format='png',bbox_inches='tight'); buf.seek(0); plt.close(fig)
    return buf

# ---------------- ANALYSIS ----------------
def analyze_symbol(symbol):
    dfs = fetch_multi_tf_klines(symbol)
    if not dfs: return
    signal = detect_signal_ml(dfs,symbol)
    if not signal: return
    photo = plot_signal(dfs["15m"],symbol,signal)
    msg = f"⚡ TRADE SIGNAL\nSymbol: {symbol}\nAction: {signal['action']}\nEntry: {signal['entry']:.6f}\nSL: {signal['sl']:.6f}\nTP1: {signal['tp1']:.6f}\nConfidence: {signal['confidence']:.2f}\nR/R1: {signal['rr1']:.2f}\n(Timeframes: {', '.join(dfs.keys())})"
    send_telegram(msg,photo)
    state["signals"][symbol]={"signal":signal,"time":str(datetime.now(timezone.utc))}
    save_json_safe(STATE_FILE,state)
    history["signals"].append(signal)
    save_json_safe(HISTORY_FILE,history)

# ---------------- TOP SYMBOLS ----------------
last_top_update = None
def fetch_top_symbols(limit=200, cache_minutes=60):
    global last_top_update
    if state.get("top_symbols") and last_top_update:
        if datetime.now(timezone.utc) - last_top_update < timedelta(minutes=cache_minutes):
            return state["top_symbols"]

    try:
        tickers = binance_client.futures_ticker()
        usdt_pairs = [t for t in tickers if t.get("symbol","").endswith("USDT")]
        scores=[]
        for t in usdt_pairs:
            try:
                change_pct = abs(float(t.get("priceChangePercent",0)))
                vol = float(t.get("quoteVolume",0))
                score = change_pct*0.6 + vol*0.4
                scores.append((t["symbol"], score))
            except: continue
        if not scores: return ["BTCUSDT","ETHUSDT","BNBUSDT"]
        sorted_symbols = [s[0] for s in sorted(scores,key=lambda x:x[1],reverse=True)[:limit]]
        state["top_symbols"] = sorted_symbols
        save_json_safe(STATE_FILE,state)
        last_top_update = datetime.now(timezone.utc)
        return sorted_symbols
    except:
        return state.get("top_symbols") or ["BTCUSDT","ETHUSDT","BNBUSDT"]

# ---------------- MASTER SCAN ----------------
def scan_all_symbols():
    symbols = fetch_top_symbols()
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
        list(exe.map(analyze_symbol,symbols))
    state["last_scan"]=str(datetime.now(timezone.utc))
    save_json_safe(STATE_FILE,state)

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/scan",methods=["POST"])
def scan_endpoint():
    Thread(target=scan_all_symbols,daemon=True).start()
    return jsonify({"ok":True,"message":"Scan started"})
@app.route("/",methods=["GET"])
def home():
    return jsonify({"status":"ok","time":str(datetime.now(timezone.utc)),"signals":len(state["signals"])})

# ---------------- MAIN ----------------
import time

def background_loop():
    """
    Основний цикл для постійного сканування символів
    """
    while True:
        try:
            scan_all_symbols()
        except Exception as e:
            logger.error(f"[LOOP] Background loop error: {e}")
        time.sleep(SCAN_INTERVAL)

def start_background():
    """
    Запускає WS і цикл сканування навіть під Gunicorn
    """
    try:
        symbols = fetch_top_symbols(limit=10)
        Thread(target=start_ws, args=(symbols,"1m"), daemon=True).start()
        Thread(target=background_loop, daemon=True).start()
        train_ml_model()
        logger.info("✅ Background scanning & WS started.")
    except Exception as e:
        logger.error(f"[INIT] Failed to start background: {e}")

# Запускаємо відразу при імпорті (щоб працювало і під Gunicorn, і локально)
start_background()

if __name__ == "__main__":
    logger.info("🚀 Starting adaptive ML trading bot (dev mode)")
    app.run(host="0.0.0.0", port=PORT)