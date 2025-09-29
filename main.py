#!/usr/bin/env python3
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
from binance.streams import ThreadedWebsocketManager
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
PORT = int(os.getenv("PORT", "5000"))
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
STATE_FILE = "state.json"
CONF_THRESHOLD_MEDIUM = 0.3
MIN_SCORE_TO_ALERT = 0.60
PLOT_CANDLES = 400

# ---------------- BINANCE CLIENT ----------------
binance_client = Client(api_key=os.getenv("BINANCE_API_KEY", ""), api_secret=os.getenv("BINANCE_API_SECRET", ""))

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

def send_telegram(text: str, photo=None, tries=1):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram token / chat_id not set, skipping send.")
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
                logger.warning("PIL processing failed, sending raw bytes: %s", e)
                files = {'photo': ('signal.png', photo, 'image/png')}

            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=15)
    except requests.exceptions.ReadTimeout as e:
        logger.warning("send_telegram timeout: %s", e)
        if tries > 0:
            time.sleep(2)
            send_telegram(text, photo=photo, tries=tries-1)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

# ---------------- FETCH / KLINES ----------------
BINANCE_REST_URL = "https://fapi.binance.com/fapi/v1/klines"

def fetch_klines_rest(symbol, interval="3m", limit=1000):
    try:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        resp = requests.get(BINANCE_REST_URL, params=params, timeout=12)
        resp.raise_for_status()
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
        logger.exception("REST fetch error for %s (%s): %s", symbol, interval, e)
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
def apply_pro_features(df: pd.DataFrame, symbol_for_multitf=None) -> pd.DataFrame:
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
    df["accumulation_zone"] = (
        (df["range"] < df["range"].rolling(20).mean() * 0.5) &
        (df["volume"] > df["vol_ma20"])
    )
    df["climax_spike"] = (df["volume"] > 3 * df["vol_ma20"]) & (abs(df["body"]) > 1.5 * df["range"])
    df["false_break_reversal"] = ((df["high"] > df["resistance"]) & (df["close"] < df["resistance"])) | \
                                 ((df["low"] < df["support"]) & (df["close"] > df["support"]))
    df["trend_exhaustion"] = ((df["trend_up"] & (df["atr"] < df["atr"].rolling(14).mean())) |
                              (df["trend_down"] & (df["atr"] < df["atr"].rolling(14).mean())))
    df["volume_divergence"] = ((df["close"].diff() > 0) & (df["volume"] < df["vol_ma20"])) | \
                              ((df["close"].diff() < 0) & (df["volume"] < df["vol_ma20"]))
    df["long_wick_rejection"] = ((df["upper_shadow"] > 2 * abs(df["body"])) & (df["close"] < df["open'])) | \
                                ((df["lower_shadow"] > 2 * abs(df["body'])) & (df["close"] > df["open']))
    df["atr_breakout"] = (df["range"] > df["atr"].rolling(20).mean() * 1.5)
    df["inside_bar"] = (df["high"] < df["high"].shift(1)) & (df["low"] > df["low"].shift(1))
    df["outside_bar"] = (df["high"] > df["high"].shift(1)) & (df["low"] < df["low"].shift(1))
    df["closing_momentum"] = df["close"].diff() > df["close"].diff().rolling(5).mean()
    df["volume_spike_reversal"] = (df["vol_spike"] & (((df["body"] < 0) & df["trend_up"]) | ((df["body"] > 0) & df["trend_down'])))
    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["ema_cross_up"] = (df["ema20"] > df["ema50"]) & (df["ema20"].shift(1) <= df["ema50"].shift(1))
    df["ema_cross_down"] = (df["ema20"] < df["ema50"]) & (df["ema20"].shift(1) >= df["ema50"].shift(1))
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["rsi_long"] = df["rsi"] < 30
    df["rsi_short"] = df["rsi"] > 70
    df["macd"] = ta.trend.macd(df["close"])
    df["macd_signal"] = ta.trend.macd_signal(df["close"])
    df["macd_long"] = df["macd"] > df["macd_signal"]
    df["macd_short"] = df["macd"] < df["macd_signal"]
    df["volatility_spike"] = df["atr"] > 2 * df["atr"].rolling(50).mean()
    try:
        df15 = fetch_klines_rest("BTCUSDT", interval="15m", limit=200)
        if df15 is not None and len(df15) > 50:
            ma15 = df15["close"].rolling(20).mean()
            trend15_up = df15["close"].iloc[-1] > ma15.iloc[-1]
            df["multi_tf_conf"] = (df["close"] > df["trend_ma"]) == trend15_up
        else:
            df["multi_tf_conf"] = False
    except Exception:
        df["multi_tf_conf"] = False
    df["power_signal_long"] = df["ema_cross_up"] & df["rsi_long"] & df["vol_spike"]
    df["power_signal_short"] = df["ema_cross_down"] & df["rsi_short"] & df["vol_spike"]
    df["power_reversal"] = ((df["body"] > 0) & (df["close"] > df["resistance"]) & df["vol_spike"]) | \
                           ((df["body"] < 0) & (df["close"] < df["support"]) & df["vol_spike"])
    return df

# ---------------- SIGNAL DETECTION ----------------
def detect_signal_pro(df: pd.DataFrame):
    last = df.iloc[-1]
    votes = []
    confidence = 0.5
    all_signals = [
        ("liquidity_grab_long",0.08), ("liquidity_grab_short",0.08),
        ("bull_trap",0.05), ("bear_trap",0.05),
        ("false_break_high",0.05), ("false_break_low",0.05),
        ("volume_cluster",0.05), ("breakout_cont_long",0.07), ("breakout_cont_short",0.07),
        ("imbalance_up",0.05), ("imbalance_down",0.05), ("squeeze",0.03),
        ("trend_up",0.05), ("trend_down",0.05), ("long_lower_wick",0.04),
        ("long_upper_wick",0.04), ("retest_support",0.05), ("retest_resistance",0.05),
        ("delta_div_long",0.06), ("delta_div_short",0.06),
        ("combo_bullish",0.1), ("combo_bearish",0.1), ("accumulation_zone",0.03),
        ("climax_spike",0.07), ("false_break_reversal",0.06), ("trend_exhaustion",0.05),
        ("volume_divergence",0.05), ("long_wick_rejection",0.04),
        ("atr_breakout",0.05), ("inside_bar",0.03), ("outside_bar",0.03),
        ("closing_momentum",0.04), ("volume_spike_reversal",0.06),
        ("ema_cross_up",0.08), ("ema_cross_down",0.08),
        ("rsi_long",0.06), ("rsi_short",0.06),
        ("macd_long",0.05), ("macd_short",0.05),
        ("volatility_spike",0.05), ("multi_tf_conf",0.06),
        ("power_signal_long",0.15), ("power_signal_short",0.15),
        ("power_reversal",0.12)
    ]
    for s, inc in all_signals:
        if last.get(s, False):
            votes.append(s)
            confidence += inc
    action = "WATCH"
    if "power_signal_long" in votes or "power_reversal" in votes and last.get("body", 0) > 0:
        action = "LONG"
    elif "power_signal_short" in votes or "power_reversal" in votes and last.get("body", 0) < 0:
        action = "SHORT"
    elif any(x in votes for x in ["combo_bullish","breakout_cont_long","delta_div_long","climax_spike","volume_spike_reversal"]):
        action = "LONG"
    elif any(x in votes for x in ["combo_bearish","breakout_cont_short","delta_div_short","trend_exhaustion","false_break_reversal"]):
        action = "SHORT"
    else:
        near_resistance = last["close"] >= (last.get("resistance", 0) or 0) * 0.98 if not pd.isna(last.get("resistance", np.nan)) else False
        near_support = last["close"] <= (last.get("support", 0) or 0) * 1.02 if not pd.isna(last.get("support", np.nan)) else False
        if near_resistance: action = "SHORT"
        if near_support: action = "LONG"
    confidence = min(max(confidence,0.05),1.0)
    return action, votes, last, confidence

def calculate_quality_score_pro(df, votes, confidence):
    score = confidence
    score += min(len(votes)*0.01, 0.1)
    return min(score, 1.0)

def calculate_levels(last, action):
    entry = last["close"]
    sl = last["support"] if action=="LONG" else last["resistance"]
    tp = entry + (entry - sl)*2 if action=="LONG" else entry - (sl - entry)*2
    return entry, sl, tp

# ---------------- PLOT ----------------
def plot_signal_chart(df, symbol, entry, sl, tp, action):
    df_plot = df.tail(PLOT_CANDLES).copy()
    mc = mpf.make_marketcolors(up='green', down='red', wick='inherit', volume='in')
    s  = mpf.make_mpf_style(marketcolors=mc)
    ap = [
        mpf.make_addplot([entry]*len(df_plot), color='blue'),
        mpf.make_addplot([sl]*len(df_plot), color='red'),
        mpf.make_addplot([tp]*len(df_plot), color='green')
    ]
    fig, axlist = mpf.plot(df_plot, type='candle', style=s, addplot=ap, returnfig=True, figsize=(12,6))
    price_ax = axlist[0]
    last_candle = df_plot.iloc[-1]
    color = "green" if action == "LONG" else "red"
    price_ax.annotate(
        action,
        xy=(df_plot.index[-1], last_candle["close"]),
        xytext=(df_plot.index[-1], last_candle["close"] * (1.02 if action=="LONG" else 0.98)),
        arrowprops=dict(facecolor=color, shrink=0.05),
        ha="center", fontsize=9, color=color
    )
    try:
        x0 = df_plot.index[-2]; x1 = df_plot.index[-1]
        ymin, ymax = last_candle["low"], last_candle["high"]
        price_ax.axvspan(x0, x1, color=color, alpha=0.1)
    except Exception as e:
        logger.debug("Highlight error: %s", e)
    buf = io.BytesIO()
    fig.savefig(buf, format='PNG', bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

# ---------------- WEBSOCKET ----------------
ws_manager = None
def handle_socket(msg):
    if msg.get("e") != "kline":
        return
    k = msg["k"]
    symbol = k["s"]
    try:
        df = pd.DataFrame([{
            "open_time": pd.to_datetime(k["t"], unit="ms"),
            "open": float(k["o"]), "high": float(k["h"]),
            "low": float(k["l"]), "close": float(k["c"]),
            "volume": float(k["v"])
        }])
        df.set_index("open_time", inplace=True)
        df = apply_pro_features(df)
        action, votes, last, confidence = detect_signal_pro(df)
        if action == "WATCH": return
        score = calculate_quality_score_pro(df, votes, confidence)
        entry, sl, tp = calculate_levels(last, action)
        rr = (tp - entry) / (entry - sl) if action == "LONG" else (entry - tp) / (sl - entry)
        if confidence >= CONF_THRESHOLD_MEDIUM and score >= MIN_SCORE_TO_ALERT and rr >= 1.5:
            chart = plot_signal_chart(df, symbol, entry, sl, tp, action)
            msg_text = (
                f"⚡ LIVE WS SIGNAL\n"
                f"Symbol: {symbol}\n"
                f"Action: {action}\n"
                f"Entry: {entry:.6f}\n"
                f"TP: {tp:.6f}\n"
                f"SL: {sl:.6f}\n"
                f"R/R: {rr:.2f}\n"
                f"Conf: {confidence:.2f}\n"
                f"Score: {score:.2f}\n"
                f"Patterns: {', '.join(votes)}"
            )
            send_telegram(msg_text, photo=chart)
    except Exception as e:
        logger.exception("WebSocket handle error: %s", e)

def start_websocket(symbols):
    global ws_manager
    ws_manager = ThreadedWebsocketManager()
    ws_manager.start()
    for s in symbols:
        ws_manager.start_kline_socket(callback=handle_socket, symbol=s.lower(), interval="3m")
    logger.info("WebSocket started for symbols: %s", symbols)

# ---------------- REST SCAN ----------------
def scan_symbols_rest(symbols):
    for s in symbols:
        df = fetch_klines_rest(s, "3m", limit=200)
        if df is None or len(df)<10: continue
        df = apply_pro_features(df)
        action, votes, last, confidence = detect_signal_pro(df)
        if action=="WATCH": continue
        score = calculate_quality_score_pro(df, votes, confidence)
        entry, sl, tp = calculate_levels(last, action)
        rr = (tp - entry) / (entry - sl) if action=="LONG" else (entry - tp) / (sl - entry)
        if confidence >= CONF_THRESHOLD_MEDIUM and score >= MIN_SCORE_TO_ALERT and rr >= 1.5:
            chart = plot_signal_chart(df, s, entry, sl, tp, action)
            msg_text = (
                f"⚡ REST SIGNAL\nSymbol: {s}\nAction: {action}\n"
                f"Entry: {entry:.6f}\nTP: {tp:.6f}\nSL: {sl:.6f}\n"
                f"R/R: {rr:.2f}\nConf: {confidence:.2f}\nScore: {score:.2f}\n"
                f"Patterns: {', '.join(votes)}"
            )
            send_telegram(msg_text, photo=chart)

# ---------------- MAIN ----------------
if __name__ == "__main__":
    top_symbols = fetch_top_symbols(limit=30)
    threading.Thread(target=start_websocket, args=(["BTCUSDT","ETHUSDT"],), daemon=True).start()
    while True:
        try:
            scan_symbols_rest(top_symbols)
            time.sleep(180)
        except Exception as e:
            logger.exception("Main loop error: %s", e)
            time.sleep(10)