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

def send_telegram(text: str, photo=None, retries=3):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return

    try:
        if photo:
            # Зменшуємо графік, якщо він великий
            buf = io.BytesIO(photo)
            buf.seek(0)
            try:
                from PIL import Image
                img = Image.open(buf)
                max_size = (800, 600)
                img.thumbnail(max_size, Image.ANTIALIAS)
                buf2 = io.BytesIO()
                img.save(buf2, format="PNG")
                buf2.seek(0)
                photo = buf2.getvalue()
            except Exception as e:
                logger.warning("PIL resize failed, sending original: %s", e)

            files = {'photo': ('signal.png', photo, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            
            for attempt in range(1, retries+1):
                try:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=30)
                    return
                except requests.exceptions.ReadTimeout:
                    logger.warning("Telegram sendPhoto timeout, attempt %d/%d", attempt, retries)
                    time.sleep(2)
                except Exception as e:
                    logger.exception("Telegram sendPhoto error: %s", e)
                    break

        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            for attempt in range(1, retries+1):
                try:
                    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=30)
                    return
                except requests.exceptions.ReadTimeout:
                    logger.warning("Telegram sendMessage timeout, attempt %d/%d", attempt, retries)
                    time.sleep(2)
                except Exception as e:
                    logger.exception("Telegram sendMessage error: %s", e)
                    break
    except Exception as e:
        logger.exception("send_telegram unexpected error: %s", e)

# ---------------- FETCH / KLINES ----------------
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
    df["support"] = df["low"].rolling(20).min()
    df["resistance"] = df["high"].rolling(20).max()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > 1.5 * df["vol_ma20"]
    df["volume_cluster"] = df["volume"] > 2 * df["vol_ma20"]
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_shadow"] = df[["close", "open"]].min(axis=1) - df["low"]
    df["liquidity_grab_long"] = (df["low"] < df["support"]) & (df["close"] > df["support"])
    df["liquidity_grab_short"] = (df["high"] > df["resistance"]) & (df["close"] < df["resistance"])
    df["false_break_high"] = (df["high"] > df["resistance"]) & (df["close"] < df["resistance"])
    df["false_break_low"] = (df["low"] < df["support"]) & (df["close"] > df["support"])
    df["bull_trap"] = (df["close"] < df["open"]) & (df["high"] > df["resistance"])
    df["bear_trap"] = (df["close"] > df["open"]) & (df["low"] < df["support"])
    df["retest_support"] = abs(df["close"] - df["support"]) / df["support"] < 0.003
    df["retest_resistance"] = abs(df["close"] - df["resistance"]) / df["resistance"] < 0.003
    df["trend_ma"] = df["close"].rolling(20).mean()
    df["trend_up"] = df["close"] > df["trend_ma"]
    df["trend_down"] = df["close"] < df["trend_ma"]
    df["long_lower_wick"] = df["lower_shadow"] > 2 * abs(df["body"])
    df["long_upper_wick"] = df["upper_shadow"] > 2 * abs(df["body"])
    df["imbalance_up"] = (df["body"] > 0) & (df["body"] > df["range"] * 0.6)
    df["imbalance_down"] = (df["body"] < 0) & (abs(df["body"]) > df["range"] * 0.6)
    df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    df["squeeze"] = df["atr"] < df["atr"].rolling(50).mean() * 0.7
    df["delta_div_long"] = (df["body"] > 0) & (df["volume"] < df["vol_ma20"])
    df["delta_div_short"] = (df["body"] < 0) & (df["volume"] < df["vol_ma20"])
    df["breakout_cont_long"] = (df["close"] > df["resistance"]) & (df["volume"] > df["vol_ma20"])
    df["breakout_cont_short"] = (df["close"] < df["support"]) & (df["volume"] > df["vol_ma20"])
    df["combo_bullish"] = df["imbalance_up"] & df["vol_spike"] & df["trend_up"]
    df["combo_bearish"] = df["imbalance_down"] & df["vol_spike"] & df["trend_down"]
    df["accumulation_zone"] = (df["range"] < df["range"].rolling(20).mean() * 0.5) & (df["volume"] > df["vol_ma20"])
    return df

# ---------------- SIGNAL DETECTION ----------------
def detect_signal_pro(df: pd.DataFrame):
    last = df.iloc[-1]
    votes = []
    confidence = 0.5

    # signals
    for signal, inc in [("liquidity_grab_long",0.08), ("liquidity_grab_short",0.08), ("bull_trap",0.05),
                        ("bear_trap",0.05), ("false_break_high",0.05), ("false_break_low",0.05),
                        ("volume_cluster",0.05), ("breakout_cont_long",0.07), ("breakout_cont_short",0.07),
                        ("imbalance_up",0.05), ("imbalance_down",0.05), ("squeeze",0.03),
                        ("trend_up",0.05), ("trend_down",0.05), ("long_lower_wick",0.04),
                        ("long_upper_wick",0.04), ("retest_support",0.05), ("retest_resistance",0.05),
                        ("delta_div_long",0.06), ("delta_div_short",0.06),
                        ("combo_bullish",0.1), ("combo_bearish",0.1), ("accumulation_zone",0.03)]:
        if last.get(signal, False):
            votes.append(signal)
            confidence += inc

    action = "WATCH"
    if "combo_bullish" in votes or "breakout_cont_long" in votes or "delta_div_long" in votes:
        action = "LONG"
    elif "combo_bearish" in votes or "breakout_cont_short" in votes or "delta_div_short" in votes:
        action = "SHORT"
    else:
        near_resistance = last["close"] >= last["resistance"] * 0.98
        near_support = last["close"] <= last["support"] * 1.02
        if near_resistance: action = "SHORT"
        elif near_support: action = "LONG"

    confidence = max(0.0, min(1.0, confidence))
    return action, votes, last, confidence

# ---------------- QUALITY SCORE ----------------
def calculate_quality_score_pro(df, votes, confidence):
    score = confidence
    strong_signals = ["combo_bullish","combo_bearish","liquidity_grab_long","liquidity_grab_short",
                      "delta_div_long","delta_div_short","breakout_cont_long","breakout_cont_short"]
    medium_signals = ["bull_trap","bear_trap","false_break_high","false_break_low",
                      "volume_cluster","retest_support","retest_resistance"]
    weak_signals = ["trend_up","trend_down","long_lower_wick","long_upper_wick","squeeze","accumulation_zone"]

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
    interval = "15m"
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
            entry = last["support"]*1.001 if action=="LONG" else last["resistance"]*0.999
            sl = last["support"]*0.99 if action=="LONG" else last["resistance"]*1.01
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

    # Send top5 to Telegram
    if not df_stats.empty:
        top5 = df_stats.head(5)
        msg = "📊 Backtest Top 5 Patterns (3m, 15m TF):\n"
        for _, row in top5.iterrows():
            msg += f"- {row['pattern_combo'][:40]}... | WR={row['winrate']:.2f} | p={row['p_value']:.3f}\n"
        send_telegram(msg)

    return df_stats

# ---------------- LIVE BOT ----------------
def analyze_and_alert(symbol: str):
    df = fetch_klines_rest(symbol, limit=200)
    if df is None or len(df)<40: return
    df = apply_pro_features(df)
    action, votes, last, confidence = detect_signal_pro(df)
    if action=="WATCH": return
    entry = last["support"]*1.001 if action=="LONG" else last["resistance"]*0.999
    sl = last["support"]*0.99 if action=="LONG" else last["resistance"]*1.01
    score = calculate_quality_score_pro(df, votes, confidence)
    rr1 = abs(last["resistance"]-entry)/(entry-sl) if action=="LONG" else abs(last["support"]-entry)/(sl-entry)
    MIN_CONFIDENCE = CONF_THRESHOLD_MEDIUM
    MIN_SCORE = 0.65
    MIN_RR = 2.0
    if confidence>=MIN_CONFIDENCE and score>=MIN_SCORE and rr1>=MIN_RR:
        emoji = "🟢" if action=="LONG" else "🔴"
        msg = (
            f"⚡ TRADE SIGNAL {emoji}\n"
            f"Symbol: {symbol}\n"
            f"Direction: {action}\n"
            f"Entry: {entry:.6f}\n"
            f"Stop-Loss: {sl:.6f}\n"
            f"Risk/Reward: {rr1:.2f}\n"
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
    # Запускаємо веб-сервер у окремому потоці
    threading.Thread(target=start_http, daemon=True).start()

    # Бек-тест
    logger.info("Starting bot: Backtest + Live")
    df_stats = backtest_patterns()
    logger.info("Top combos:\n%s", df_stats.head(10))

    # Live-цикл
    while True:
        scan_all_symbols()
        time.sleep(15*60)  # 15m таймфрейм