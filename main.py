import os, json, logging, requests, io, time, psutil
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import mplfinance as mpf
import ta
from datetime import datetime, timezone
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
from binance.client import Client
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
    except: pass

# ---------------- DATA ----------------
def fetch_klines(symbol,interval="15m",limit=500):
    try:
        url="https://fapi.binance.com/fapi/v1/klines"
        resp=requests.get(url,params={"symbol":symbol,"interval":interval,"limit":limit},timeout=5)
        data=resp.json()
        df=pd.DataFrame(data,columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"])
        for col in ["open","high","low","close","volume"]: df[col]=df[col].astype(float)
        df["open_time"]=pd.to_datetime(df["open_time"],unit="ms")
        df.set_index("open_time",inplace=True)
        return df
    except: return None

# ---------------- FEATURES ----------------
def extract_features(df:pd.DataFrame):
    df=df.copy()
    df["ema20"]=df["close"].ewm(span=20).mean()
    df["ema50"]=df["close"].ewm(span=50).mean()
    df["ema_diff"]=df["ema20"]-df["ema50"]
    df["adx"]=ta.trend.ADXIndicator(df['high'],df['low'],df['close'],window=14).adx()
    df["rsi"]=ta.momentum.RSIIndicator(df['close'],window=14).rsi()
    df["macd"]=ta.trend.MACD(df['close']).macd()
    df["macd_signal"]=ta.trend.MACD(df['close']).macd_signal()
    df["macd_hist"]=df["macd"]-df["macd_signal"]
    df["atr"]=ta.volatility.AverageTrueRange(df['high'],df['low'],df['close'],window=14).average_true_range()
    df["bb_width"]=ta.volatility.BollingerBands(df['close']).bollinger_hband()-ta.volatility.BollingerBands(df['close']).bollinger_lband()
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
        df = fetch_klines(symbol, interval=tf, limit=limit)
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

# ---------------- SIGNAL EVALUATION ----------------
def evaluate_signals(symbol, look_ahead=10):
    df = fetch_klines(symbol, interval="15m", limit=look_ahead+1)
    if df is None or len(df)<look_ahead: return
    for sig in history.get("signals", []):
        if sig.get("evaluated"): continue
        if sig.get("symbol") != symbol: continue
        entry = sig["entry"]
        sl = sig["sl"]
        tps = [sig["tp1"], sig["tp2"], sig["tp3"]]
        action = sig["action"]
        future = df.iloc[-look_ahead:]
        success = 0
        if action == "LONG":
            if (future["high"]>=max(tps)).any(): success = 1
            elif (future["low"]<=sl).any(): success = 0
        else:
            if (future["low"]<=min(tps)).any(): success = 1
            elif (future["high"]>=sl).any(): success = 0
        sig["success"] = success
        sig["evaluated"] = True
    save_json_safe(HISTORY_FILE, history)
    train_ml_model()

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
    evaluate_signals(symbol)

# ---------------- DYNAMIC TOP SYMBOLS ----------------
def fetch_top_symbols(limit=TOP_SYMBOL_LIMIT):
    try:
        tickers = binance_client.futures_ticker()
        usdt_pairs = [t for t in tickers if t['symbol'].endswith("USDT")]
        scores=[]
        for t in usdt_pairs:
            try:
                change_pct=abs(float(t.get("priceChangePercent",0)))
                vol=float(t.get("quoteVolume",0))
                scores.append((t['symbol'],change_pct*0.6+vol*0.4))
            except: continue
        sorted_symbols=[s[0] for s in sorted(scores,key=lambda x:x[1],reverse=True)[:limit]]
        state["top_symbols"]=sorted_symbols
        save_json_safe(STATE_FILE,state)
        return sorted_symbols
    except: return ["BTCUSDT","ETHUSDT","BNBUSDT"]

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
def home(): return jsonify({"status":"ok","time":str(datetime.now(timezone.utc)),"signals":len(state["signals"])})

# ---------------- MAIN ----------------
if __name__=="__main__":
    logger.info("Starting adaptive ML trading bot")
    Thread(target=scan_all_symbols,daemon=True).start()
    app.run(host="0.0.0.0",port=PORT)