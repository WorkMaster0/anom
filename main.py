import os
import time
import json
import logging
import re
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import matplotlib.pyplot as plt
import requests
import ta
import mplfinance as mpf
import numpy as np
import io
from binance.client import Client
from scipy.stats import binomtest
import http.server
import socketserver
from PIL import Image

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("trade-bot")

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
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
            try:
                img = Image.open(io.BytesIO(photo))
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                buf.seek(0)
                files = {'photo': ('signal.png', buf, 'image/png')}
            except Exception as e:
                logger.warning("PIL resize failed, sending original: %s", e)
                files = {'photo': ('signal.png', photo, 'image/png')}

            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=15)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

# ---------------- FETCH / KLINES ----------------
BINANCE_REST_URL = "https://fapi.binance.com/fapi/v1/klines"

def fetch_klines_rest(symbol, interval="3m", limit=500):
    try:
        resp = requests.get(BINANCE_REST_URL, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
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

def fetch_top_symbols(limit=30):
    try:
        tickers = binance_client.futures_ticker()
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

# ---------------- FEATURE ENGINEERING ----------------
def apply_pro_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    # --- (залишив старі фічі як є) ---

    # ---------------- НОВІ ФІЧІ ----------------
    # EMA Cross
    df["ema20"] = ta.trend.ema_indicator(df["close"], 20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], 50)
    df["ema_cross_up"] = (df["ema20"] > df["ema50"]) & (df["ema20"].shift(1) <= df["ema50"].shift(1))
    df["ema_cross_down"] = (df["ema20"] < df["ema50"]) & (df["ema20"].shift(1) >= df["ema50"].shift(1))

    # RSI Extreme
    rsi = ta.momentum.rsi(df["close"], window=14)
    df["rsi_long"] = rsi < 30
    df["rsi_short"] = rsi > 70

    # MACD Momentum
    macd = ta.trend.macd(df["close"])
    macd_signal = ta.trend.macd_signal(df["close"])
    df["macd_long"] = macd > macd_signal
    df["macd_short"] = macd < macd_signal

    # Volatility Spike
    atr = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    df["volatility_spike"] = atr > 2 * atr.rolling(50).mean()

    # Multi-timeframe Confirmation (3m vs 15m trend)
    try:
        df15 = fetch_klines_rest("BTCUSDT", interval="15m", limit=200)
        if df15 is not None and len(df15) > 50:
            df15["ma"] = df15["close"].rolling(20).mean()
            trend15 = df15["close"].iloc[-1] > df15["ma"].iloc[-1]
            df["multi_tf_conf"] = (df["close"] > df["close"].rolling(20).mean()) == trend15
        else:
            df["multi_tf_conf"] = False
    except:
        df["multi_tf_conf"] = False

    # POWER SIGNAL (унікальна комбінація)
    df["power_signal_long"] = df["ema_cross_up"] & df["rsi_long"] & df["vol_spike"]
    df["power_signal_short"] = df["ema_cross_down"] & df["rsi_short"] & df["vol_spike"]

    return df

# ---------------- SIGNAL DETECTION ----------------
def detect_signal_pro(df: pd.DataFrame):
    last = df.iloc[-1]
    votes = []
    confidence = 0.5

    all_signals = [
        # старі сигнали ...
        ("ema_cross_up", 0.08), ("ema_cross_down", 0.08),
        ("rsi_long", 0.07), ("rsi_short", 0.07),
        ("macd_long", 0.06), ("macd_short", 0.06),
        ("volatility_spike", 0.05),
        ("multi_tf_conf", 0.07),
        ("power_signal_long", 0.15), ("power_signal_short", 0.15),
    ]

    for signal, inc in all_signals:
        if last.get(signal, False):
            votes.append(signal)
            confidence += inc

    action = "WATCH"
    if "power_signal_long" in votes:
        action = "LONG"
    elif "power_signal_short" in votes:
        action = "SHORT"
    elif "ema_cross_up" in votes or "rsi_long" in votes or "macd_long" in votes:
        action = "LONG"
    elif "ema_cross_down" in votes or "rsi_short" in votes or "macd_short" in votes:
        action = "SHORT"

    confidence = max(0.0, min(1.0, confidence))
    return action, votes, last, confidence

# ---------------- QUALITY SCORE ----------------
def calculate_quality_score_pro(df, votes, confidence):
    score = confidence
    strong_signals = ["power_signal_long","power_signal_short","ema_cross_up","ema_cross_down","rsi_long","rsi_short","macd_long","macd_short"]
    medium_signals = ["volatility_spike","multi_tf_conf"]
    weak_signals = []

    for p in votes:
        if p in strong_signals: score += 0.1
        elif p in medium_signals: score += 0.05
        elif p in weak_signals: score += 0.02
    return min(score, 1.0)

# ---------------- PLOT ----------------
def plot_signal_chart(df, symbol, entry, sl, action):
    df_plot = df.tail(80).copy()
    df_plot.index.name = "Date"
    add_plots = [
        mpf.make_addplot([entry]*len(df_plot), color="green", linestyle="--"),
        mpf.make_addplot([sl]*len(df_plot), color="red", linestyle="--"),
    ]
    fig, ax = mpf.plot(
        df_plot,
        type="candle",
        style="charles",
        volume=True,
        addplot=add_plots,
        title=f"{symbol} | {action}",
        returnfig=True
    )
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()

# ---------------- BACKTEST ----------------
def backtest_patterns():
    logger.info("=== BACKTEST STARTED ===")
    symbols = fetch_top_symbols(limit=30)
    results = []
    all_wins = 0
    all_trades = 0
    interval = "3m"
    limit_per_call = 500

    for symbol in symbols:
        df = fetch_klines_rest(symbol, interval=interval, limit=limit_per_call*2)
        if df is None or len(df) < 50:
            continue
        df = apply_pro_features(df)
        for i in range(20, len(df)):
            sub_df = df.iloc[:i+1]
            action, votes, last, confidence = detect_signal_pro(sub_df)
            if action == "WATCH": continue
            entry = last["close"]
            sl = entry * (0.99 if action=="LONG" else 1.01)
            win = (last["close"] > entry) if action=="LONG" else (last["close"] < entry)
            results.append({"symbol": symbol,"action":action,"votes":",".join(votes),"win":win})
            all_trades +=1
            if win: all_wins +=1

    baseline = all_wins / all_trades if all_trades>0 else 0.5
    logger.info("Baseline winrate across all trades: %.2f", baseline)

    combos = {}
    for r in results:
        key = r["votes"]
        if key not in combos:
            combos[key] = {"trades":0,"wins":0}
        combos[key]["trades"] +=1
        if r["win"]: combos[key]["wins"]+=1

    stats = []
    for k,v in combos.items():
        wr = v["wins"]/v["trades"]
        pval = binomtest(v["wins"], v["trades"], baseline).pvalue
        stats.append({"pattern_combo":k,"trades":v["trades"],"winrate":wr,"baseline":baseline,"p_value":pval,"significance":pval<0.05})

    df_stats = pd.DataFrame(stats).sort_values("winrate",ascending=False)
    df_stats.to_csv("patterns_stats.csv", index=False)
    logger.info("=== BACKTEST FINISHED ===")
    return df_stats

# ---------------- LIVE BOT ----------------
def analyze_and_alert(symbol: str):
    df = fetch_klines_rest(symbol, limit=200)
    if df is None or len(df)<40: return
    df = apply_pro_features(df)
    action, votes, last, confidence = detect_signal_pro(df)
    if action=="WATCH": return
    entry = last["close"]
    sl = entry * (0.99 if action=="LONG" else 1.01)
    score = calculate_quality_score_pro(df, votes, confidence)
    rr1 = abs(last["close"]-entry)/(entry-sl)
    MIN_CONFIDENCE = CONF_THRESHOLD_MEDIUM
    MIN_SCORE = 0.65
    if confidence>=MIN_CONFIDENCE and score>=MIN_SCORE:
        emoji = "🟢" if action=="LONG" else "🔴"
        msg = (
            f"⚡ TRADE SIGNAL {emoji}\n"
            f"Symbol: {symbol}\n"
            f"Direction: {action}\n"
            f"Entry: {entry:.6f}\n"
            f"Stop-Loss: {sl:.6f}\n"
            f"Confidence: {confidence:.2f}\n"
            f"Quality Score: {score:.2f}\n"
            f"Patterns: {', '.join(votes)}"
        )
        chart = plot_signal_chart(df, symbol, entry, sl, action)
        send_telegram(msg, photo=chart)

def scan_all_symbols():
    symbols = fetch_top_symbols(limit=30)
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
        list(exe.map(analyze_and_alert, symbols))
    state["last_scan"]=str(datetime.now(timezone.utc))
    save_json_safe(STATE_FILE,state)

# ---------------- HTTP SERVER ----------------
def start_http():
    port = int(os.environ.get("PORT", 5000))
    Handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), Handler) as httpd:
        logger.info(f"HTTP server listening on port {port}")
        httpd.serve_forever()

# ---------------- MAIN ----------------
if __name__=="__main__":
    threading.Thread(target=start_http, daemon=True).start()
    logger.info("Starting bot: Backtest + Live (3m TF)")
    df_stats = backtest_patterns()
    while True:
        scan_all_symbols()
        time.sleep(3*60)  # 3m таймфрейм