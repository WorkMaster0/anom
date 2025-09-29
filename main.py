#!/usr/bin/env python3
# trade_bot.py -- Повний скрипт: старі фічі + ADX + funding/OI + multi-TF + глобальні патерни + RR>=3 + візуалізація
import os
import time
import json
import logging
import re
import threading
import io
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
import matplotlib.pyplot as plt
import requests
import ta
import mplfinance as mpf
import numpy as np
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
PORT = int(os.getenv("PORT", "5000"))
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
STATE_FILE = "state.json"
CONF_THRESHOLD_MEDIUM = 0.01
MIN_SCORE_TO_ALERT = 0.01
PLOT_CANDLES = 300  # <- як просив(а)

# Binance client (needs API keys in env)
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

# ---------------- TELEGRAM (з PIL обробкою фото) ----------------
MARKDOWNV2_ESCAPE = r"_*[]()~`>#+-=|{}.!"

def escape_md_v2(text: str) -> str:
    return re.sub(f"([{re.escape(MARKDOWNV2_ESCAPE)}])", r"\\\1", str(text))

def send_telegram(text: str, photo=None, tries=1):
    """Надіслати повідомлення/фото в Telegram; на помилку -- лог."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram token / chat_id not set, skipping send.")
        return
    try:
        if photo:
            # Відкриваємо та зберігаємо через PIL (щоб уникнути ANTIALIAS warning)
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
    """Fetch klines via REST. limit default 1000."""
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
    """Fetch top symbols by 24h change (futures tickers)."""
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

# ---------------- FUNDING & OI ----------------
def fetch_funding_rate(symbol):
    """Fetch recent funding rate (futures)."""
    try:
        fr = binance_client.futures_funding_rate(symbol=symbol, limit=1)
        if fr and len(fr) > 0:
            return float(fr[0].get("fundingRate", 0.0))
    except Exception as e:
        logger.debug("Funding rate fetch failed for %s: %s", symbol, e)
    return 0.0

def fetch_open_interest(symbol):
    """Fetch open interest for symbol (futures)."""
    try:
        oi = binance_client.futures_open_interest(symbol=symbol)
        return float(oi.get("openInterest", 0.0))
    except Exception as e:
        logger.debug("OI fetch failed for %s: %s", symbol, e)
    return 0.0

# ---------------- SIGNALS DEFINITION (global order for ML) ----------------
# Список сигналів з вагами (використовується у detect та ML)
SIGNALS_WITH_WEIGHTS = [
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
    ("power_reversal",0.12),
    # global pattern signals
    ("sideways", 0.05), ("at_range_top", 0.08), ("at_range_bottom", 0.08),
    ("triangle_top_touch", 0.10), ("triangle_bottom_touch", 0.10),
    ("funding_long_bias", 0.04), ("funding_short_bias", 0.04),
    ("strong_trend", 0.05)
]

# Helper: list of names in same order (for ML features)
SIGNAL_NAMES_ORDER = [s for s, w in SIGNALS_WITH_WEIGHTS]

# ---------------- FEATURE ENGINEERING (старі + нові фічі) ----------------
def apply_pro_features(df: pd.DataFrame, symbol_for_multitf=None) -> pd.DataFrame:
    """
    Обчислює багато характеристик (старі + нові).
    symbol_for_multitf: якщо хочемо підвантажити 15m trend для multi_tf_conf,
    передати символ (опціонально).
    """
    df = df.copy()

    # --- Support/Resistance (динамічні рівні) ---
    df["support"] = df["low"].rolling(20).min()
    df["resistance"] = df["high"].rolling(20).max()

    # --- Volume analysis ---
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > 1.5 * df["vol_ma20"]
    df["volume_cluster"] = df["volume"] > 2 * df["vol_ma20"]

    # --- Candle structure ---
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_shadow"] = df[["close", "open"]].min(axis=1) - df["low"]

    # --- Liquidity grabs ---
    df["liquidity_grab_long"] = (df["low"] < df["support"]) & (df["close"] > df["support"])
    df["liquidity_grab_short"] = (df["high"] > df["resistance"]) & (df["close"] < df["resistance"])

    # --- False breaks & traps ---
    df["false_break_high"] = (df["high"] > df["resistance"]) & (df["close"] < df["resistance"])
    df["false_break_low"] = (df["low"] < df["support"]) & (df["close"] > df["support"])
    df["bull_trap"] = (df["close"] < df["open"]) & (df["high"] > df["resistance"])
    df["bear_trap"] = (df["close"] > df["open"]) & (df["low"] < df["support"])

    # --- Retests ---
    df["retest_support"] = abs(df["close"] - df["support"]) / df["support"] < 0.003
    df["retest_resistance"] = abs(df["close"] - df["resistance"]) / df["resistance"] < 0.003

    # --- Trend ---
    df["trend_ma"] = df["close"].rolling(20).mean()
    df["trend_up"] = df["close"] > df["trend_ma"]
    df["trend_down"] = df["close"] < df["trend_ma"]

    # --- Wick exhaustion ---
    df["long_lower_wick"] = df["lower_shadow"] > 2 * abs(df["body"])
    df["long_upper_wick"] = df["upper_shadow"] > 2 * abs(df["body"])

    # --- Momentum / Imbalance ---
    df["imbalance_up"] = (df["body"] > 0) & (df["body"] > df["range"] * 0.6)
    df["imbalance_down"] = (df["body"] < 0) & (abs(df["body"]) > df["range"] * 0.6)

    # --- Volatility squeeze / ATR ---
    df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
    df["squeeze"] = df["atr"] < df["atr"].rolling(50).mean() * 0.7

    # --- Delta divergence (через об'єм) ---
    df["delta_div_long"] = (df["body"] > 0) & (df["volume"] < df["vol_ma20"])
    df["delta_div_short"] = (df["body"] < 0) & (df["volume"] < df["vol_ma20"])

    # --- Breakout continuation ---
    df["breakout_cont_long"] = (df["close"] > df["resistance"]) & (df["volume"] > df["vol_ma20"])
    df["breakout_cont_short"] = (df["close"] < df["support"]) & (df["volume"] > df["vol_ma20"])

    # --- Combo patterns ---
    df["combo_bullish"] = df["imbalance_up"] & df["vol_spike"] & df["trend_up"]
    df["combo_bearish"] = df["imbalance_down"] & df["vol_spike"] & df["trend_down"]

    # --- Accumulation zones ---
    df["accumulation_zone"] = (
        (df["range"] < df["range"].rolling(20).mean() * 0.5) &
        (df["volume"] > df["vol_ma20"])
    )

    # ---------------- НОВІ ФІЧІ ----------------
    # Climax spike
    df["climax_spike"] = (df["volume"] > 3 * df["vol_ma20"]) & (abs(df["body"]) > 1.5 * df["range"])

    # False break reversal
    df["false_break_reversal"] = ((df["high"] > df["resistance"]) & (df["close"] < df["resistance"])) | \
                                 ((df["low"] < df["support"]) & (df["close"] > df["support"]))

    # Trend exhaustion (ATR-based)
    df["trend_exhaustion"] = ((df["trend_up"] & (df["atr"] < df["atr"].rolling(14).mean())) |
                              (df["trend_down"] & (df["atr"] < df["atr"].rolling(14).mean())))

    # Volume divergence
    df["volume_divergence"] = ((df["close"].diff() > 0) & (df["volume"] < df["vol_ma20"])) | \
                              ((df["close"].diff() < 0) & (df["volume"] < df["vol_ma20"]))

    # Long wick rejection
    df["long_wick_rejection"] = ((df["upper_shadow"] > 2 * abs(df["body"])) & (df["close"] < df["open"])) | \
                                ((df["lower_shadow"] > 2 * abs(df["body"])) & (df["close"] > df["open"]))

    # ATR breakout
    df["atr_breakout"] = (df["range"] > df["atr"].rolling(20).mean() * 1.5)

    # Inside/Outside bar
    df["inside_bar"] = (df["high"] < df["high"].shift(1)) & (df["low"] > df["low"].shift(1))
    df["outside_bar"] = (df["high"] > df["high"].shift(1)) & (df["low"] < df["low"].shift(1))

    # Closing momentum
    df["closing_momentum"] = df["close"].diff() > df["close"].diff().rolling(5).mean()

    # Volume spike reversal
    df["volume_spike_reversal"] = (df["vol_spike"] & (((df["body"] < 0) & df["trend_up"]) | ((df["body"] > 0) & df["trend_down"])))

    # --- EMA / RSI / MACD / Volatility spike / Multi-TF / Power-signal ---
    # EMA cross
    df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
    df["ema_cross_up"] = (df["ema20"] > df["ema50"]) & (df["ema20"].shift(1) <= df["ema50"].shift(1))
    df["ema_cross_down"] = (df["ema20"] < df["ema50"]) & (df["ema20"].shift(1) >= df["ema50"].shift(1))

    # RSI extremes
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)
    df["rsi_long"] = df["rsi"] < 30
    df["rsi_short"] = df["rsi"] > 70

    # MACD
    df["macd"] = ta.trend.macd(df["close"])
    df["macd_signal"] = ta.trend.macd_signal(df["close"])
    df["macd_long"] = df["macd"] > df["macd_signal"]
    df["macd_short"] = df["macd"] < df["macd_signal"]

    # Volatility spike (ATR vs its moving average)
    df["volatility_spike"] = df["atr"] > 2 * df["atr"].rolling(50).mean()

    # Multi TF confirmation: use same symbol 15m trend as market context; fallback to BTCUSDT
    try:
        tgt = symbol_for_multitf or "BTCUSDT"
        df15 = fetch_klines_rest(tgt, interval="15m", limit=200)
        if df15 is not None and len(df15) > 50:
            ma15 = df15["close"].rolling(20).mean()
            trend15_up = df15["close"].iloc[-1] > ma15.iloc[-1]
            df["multi_tf_conf"] = (df["close"] > df["trend_ma"]) == trend15_up
        else:
            df["multi_tf_conf"] = False
    except Exception:
        df["multi_tf_conf"] = False

    # Power signal (EMA cross + RSI extreme + vol_spike)
    df["power_signal_long"] = df["ema_cross_up"] & df["rsi_long"] & df["vol_spike"]
    df["power_signal_short"] = df["ema_cross_down"] & df["rsi_short"] & df["vol_spike"]

    # power_reversal (альтернативна / додаткова)
    df["power_reversal"] = ((df["body"] > 0) & (df["close"] > df["resistance"]) & df["vol_spike"]) | \
                           ((df["body"] < 0) & (df["close"] < df["support"]) & df["vol_spike"])

    # --- ADX / strong trend ---
    df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    df["strong_trend"] = df["adx"] > 25

    # --- Funding + OI as latest snapshot in df (store as constant column) ---
    try:
        fr = fetch_funding_rate(symbol_for_multitf or "BTCUSDT")
        oi = fetch_open_interest(symbol_for_multitf or "BTCUSDT")
        df["funding_rate"] = fr
        df["open_interest"] = oi
        df["funding_long_bias"] = df["funding_rate"] > 0.01
        df["funding_short_bias"] = df["funding_rate"] < -0.01
    except Exception:
        df["funding_rate"] = 0.0
        df["open_interest"] = 0.0
        df["funding_long_bias"] = False
        df["funding_short_bias"] = False

    # ---------------- ГЛОБАЛЬНІ ПАТЕРНИ (на останніх PLOT_CANDLES свічках) ----------------
    gw = PLOT_CANDLES  # 300
    if len(df) > gw:
        sub = df.tail(gw)
        rng = sub["high"].max() - sub["low"].min()
        atr_mean = sub["atr"].mean() if not pd.isna(sub["atr"].mean()) else sub["range"].mean()
        # боковик: small range vs avg ATR
        df["sideways"] = rng < atr_mean * 4
        df["at_range_top"] = df["close"] > sub["high"].max() * 0.98
        df["at_range_bottom"] = df["close"] < sub["low"].min() * 1.02

        # Triangle / channel approx via polyfit on highs/lows
        try:
            highs = np.polyfit(np.arange(len(sub)), sub["high"].values, 1)
            lows = np.polyfit(np.arange(len(sub)), sub["low"].values, 1)
            # value at the last index
            up_line_val = np.poly1d(highs)(len(sub)-1)
            low_line_val = np.poly1d(lows)(len(sub)-1)
            df["triangle_top_touch"] = df["close"] >= up_line_val * 0.99
            df["triangle_bottom_touch"] = df["close"] <= low_line_val * 1.01
        except Exception:
            df["triangle_top_touch"] = False
            df["triangle_bottom_touch"] = False
    else:
        df["sideways"] = False
        df["at_range_top"] = False
        df["at_range_bottom"] = False
        df["triangle_top_touch"] = False
        df["triangle_bottom_touch"] = False

    return df

# ---------------- SIGNAL DETECTION ----------------
def detect_signal_pro(df: pd.DataFrame):
    """Голосуємо по паттернах, повертаємо action, votes, last_row, confidence"""
    last = df.iloc[-1]
    votes = []
    confidence = 0.5

    # Використовуємо глобальний порядок SIGNALS_WITH_WEIGHTS
    for s, inc in SIGNALS_WITH_WEIGHTS:
        try:
            if bool(last.get(s, False)):
                votes.append(s)
                confidence += inc
        except Exception:
            continue

    action = "WATCH"
    # Priority: power signals
    if ("power_signal_long" in votes) or (("power_reversal" in votes) and last.get("body", 0) > 0):
        action = "LONG"
    elif ("power_signal_short" in votes) or (("power_reversal" in votes) and last.get("body", 0) < 0):
        action = "SHORT"
    # Global patterns
    elif any(x in votes for x in ["combo_bullish","breakout_cont_long","delta_div_long","climax_spike","volume_spike_reversal"]):
        action = "LONG"
    elif any(x in votes for x in ["combo_bearish","breakout_cont_short","delta_div_short","trend_exhaustion","false_break_reversal"]):
        action = "SHORT"
    else:
        # proximity to major levels
        near_resistance = False
        near_support = False
        try:
            res = last.get("resistance", np.nan)
            sup = last.get("support", np.nan)
            if not pd.isna(res) and res:
                near_resistance = last["close"] >= res * 0.98
            if not pd.isna(sup) and sup:
                near_support = last["close"] <= sup * 1.02
        except Exception:
            pass
        if near_resistance:
            action = "SHORT"
        elif near_support:
            action = "LONG"

    confidence = max(0.0, min(1.0, confidence))
    return action, votes, last, confidence

# ---------------- QUALITY SCORE (нормалізований) ----------------
def calculate_quality_score_pro(df, votes, confidence):
    """
    Нормалізуємо score:
    - додаємо ваги за тип сигналів,
    - зменшуємо при великій кількості голосів (penalty),
    - штраф за суперечливі сигнали.
    """
    score = confidence
    strong_signals = {"combo_bullish","combo_bearish","liquidity_grab_long","liquidity_grab_short",
                      "delta_div_long","delta_div_short","breakout_cont_long","breakout_cont_short",
                      "climax_spike","volume_spike_reversal","false_break_reversal","trend_exhaustion",
                      "power_signal_long","power_signal_short","power_reversal"}
    medium_signals = {"bull_trap","bear_trap","false_break_high","false_break_low",
                      "volume_cluster","retest_support","retest_resistance","atr_breakout","closing_momentum",
                      "volatility_spike","multi_tf_conf"}
    weak_signals = {"trend_up","trend_down","long_lower_wick","long_upper_wick","squeeze","accumulation_zone",
                    "volume_divergence","long_wick_rejection","inside_bar","outside_bar","ema_cross_up","ema_cross_down",
                    "rsi_long","rsi_short","macd_long","macd_short"}

    for p in votes:
        if p in strong_signals:
            score += 0.15
        elif p in medium_signals:
            score += 0.07
        elif p in weak_signals:
            score += 0.03

    # penalty: якщо багато "шумних" сигналів, трохи знижуємо score
    penalty = max(0.0, (len(votes) - 3) * 0.03)
    score = score - penalty

    # штраф за суперечливі голоси (наприклад trend_up та trend_down одночасно)
    if "trend_up" in votes and "trend_down" in votes:
        score -= 0.1
    if "power_signal_long" in votes and "power_signal_short" in votes:
        score -= 0.2

    # нормалізуємо залежно від числа голосів, щоб уникнути частого 1.0
    denom = 1 + max(0, len(votes) / 3)
    score = score / denom

    score = max(0.0, min(1.0, score))
    return score

# ---------------- CALCULATE LEVELS (market entry + TP/SL) ----------------
def calculate_levels(last, action):
    """
    Market entry (last close). TP/SL базуються на support/resistance коли доступні,
    інакше на ATR (1.5 для SL, 3.0 для TP).
    """
    entry = float(last["close"])
    atr = float(last["atr"]) if not pd.isna(last.get("atr", np.nan)) else float(last["high"] - last["low"])

    if action == "LONG":
        # SL на support (з невеликим buffer), TP на resistance або ATR multiple
        if not pd.isna(last.get("support", np.nan)) and last.get("support", None) not in [None, np.nan]:
            sl = float(last["support"]) * 0.995
        else:
            sl = entry - 1.5 * atr
        if not pd.isna(last.get("resistance", np.nan)) and last.get("resistance", None) not in [None, np.nan]:
            tp = float(last["resistance"]) * 0.999
            # якщо TP дуже близько до entry — використати ATR
            if (tp - entry) < 0.5 * atr:
                tp = entry + 3.0 * atr
        else:
            tp = entry + 3.0 * atr

    elif action == "SHORT":
        if not pd.isna(last.get("resistance", np.nan)) and last.get("resistance", None) not in [None, np.nan]:
            sl = float(last["resistance"]) * 1.005
        else:
            sl = entry + 1.5 * atr
        if not pd.isna(last.get("support", np.nan)) and last.get("support", None) not in [None, np.nan]:
            tp = float(last["support"]) * 1.001
            if (entry - tp) < 0.5 * atr:
                tp = entry - 3.0 * atr
        else:
            tp = entry - 3.0 * atr
    else:
        sl = entry
        tp = entry

    return entry, sl, tp

# ---------------- PLOT (candles + support/resistance + global pattern shading) ----------------
def plot_signal_chart(df, symbol, entry, sl, tp, action):
    """
    Малюємо останні PLOT_CANDLES свічок, додаємо entry/sl/tp та підтримку/опір,
    а також верх/низ трикутника чи каналу (приблизно).
    Повертає байти PNG.
    """
    df_plot = df.tail(PLOT_CANDLES).copy()
    df_plot.index.name = "Date"

    # horizontal series
    support_series = pd.Series(df_plot["support"].values, index=df_plot.index)
    resistance_series = pd.Series(df_plot["resistance"].values, index=df_plot.index)

    addplots = [
        mpf.make_addplot(support_series, panel=0, type='line', width=0.5, linestyle=':', alpha=0.6),
        mpf.make_addplot(resistance_series, panel=0, type='line', width=0.5, linestyle=':', alpha=0.6)
    ]

    entry_color = 'green' if action == "LONG" else 'red'
    tp_color = 'blue'
    sl_color = 'orange'

    fig, axes = mpf.plot(
        df_plot,
        type='candle',
        style='charles',
        volume=True,
        addplot=addplots,
        title=f"{symbol} | {action}",
        returnfig=True,
        figsize=(14, 9),
        tight_layout=True
    )

    # axes -> list: axes[0] main, axes[2] volume (differs by mpf version)
    price_ax = axes[0] if isinstance(axes, (list, tuple)) else axes

    # horizontal lines
    price_ax.axhline(entry, color=entry_color, linestyle='--', linewidth=1.25, alpha=0.9, label='Entry')
    price_ax.axhline(tp, color=tp_color, linestyle='--', linewidth=1.0, alpha=0.9, label='TP')
    price_ax.axhline(sl, color=sl_color, linestyle='--', linewidth=1.0, alpha=0.9, label='SL')

    # shading (risk area)
    try:
        if action == "LONG":
            ymin = min(entry, sl)
            ymax = max(entry, tp)
            price_ax.axhspan(ymin, entry, color='red', alpha=0.07)   # risk zone
            price_ax.axhspan(entry, ymax, color='green', alpha=0.05)  # reward zone
        elif action == "SHORT":
            ymin = min(tp, entry)
            ymax = max(entry, sl)
            price_ax.axhspan(ymin, entry, color='green', alpha=0.05)  # reward
            price_ax.axhspan(entry, ymax, color='red', alpha=0.07)    # risk
    except Exception as e:
        logger.debug("Shading error: %s", e)

    # draw triangle/channel approx (using polyfit on tail)
    try:
        if len(df_plot) >= 20:
            sub = df_plot.tail(PLOT_CANDLES)
            x = np.arange(len(sub))
            highs = np.polyfit(x, sub["high"].values, 1)
            lows = np.polyfit(x, sub["low"].values, 1)
            top_vals = np.poly1d(highs)(x)
            bottom_vals = np.poly1d(lows)(x)
            price_ax.plot(sub.index, top_vals, linestyle='--', linewidth=1.0, alpha=0.7, label='Top pattern')
            price_ax.plot(sub.index, bottom_vals, linestyle='--', linewidth=1.0, alpha=0.7, label='Bottom pattern')
    except Exception as e:
        logger.debug("Pattern drawing failed: %s", e)

    # annotation: last price + time
    last_time = df_plot.index[-1].strftime("%Y-%m-%d %H:%M")
    last_price = df_plot["close"].iloc[-1]
    price_ax.text(0.01, 0.98, f"{last_time}  Price: {last_price:.6f}", transform=price_ax.transAxes, fontsize=9,
                  verticalalignment='top', bbox=dict(boxstyle="round", facecolor="white", alpha=0.6))

    # tidy up legend
    price_ax.legend(loc="upper right", fontsize=8)

    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)
    return buf.getvalue()

# ---------------- VISUALIZATION ГЛОБАЛЬНИХ ПАТЕРНІВ (окрема функція) ----------------
def plot_pattern_chart(df: pd.DataFrame, symbol: str, entry, sl, tp, action: str) -> bytes:
    """
    Малює глобальний патерн (останні PLOT_CANDLES) + лінії трикутника/каналу + entry/SL/TP.
    Повертає байти PNG.
    """
    sub = df.tail(PLOT_CANDLES).copy()
    plt.figure(figsize=(14, 8))
    plt.plot(sub.index, sub["close"], label="Close", linewidth=1.0)

    # Range lines (якщо боковик)
    try:
        if sub["high"].max() - sub["low"].min() < sub["atr"].mean() * 4:
            h = sub["high"].max()
            l = sub["low"].min()
            plt.axhline(h, color="orange", linestyle="--", label="Range High")
            plt.axhline(l, color="blue", linestyle="--", label="Range Low")

        # Triangle regression lines
        x = np.arange(len(sub))
        highs = np.polyfit(x, sub["high"].values, 1)
        lows = np.polyfit(x, sub["low"].values, 1)
        top_line = np.poly1d(highs)(x)
        bot_line = np.poly1d(lows)(x)
        plt.plot(sub.index, top_line, "r--", label="Triangle Top")
        plt.plot(sub.index, bot_line, "g--", label="Triangle Bottom")
    except Exception as e:
        logger.debug("plot_pattern_chart polyfit error: %s", e)

    # Entry/SL/TP
    plt.axhline(entry, color="gold", linestyle="--", label="Entry")
    plt.axhline(sl, color="red", linestyle="--", label="SL")
    plt.axhline(tp, color="green", linestyle="--", label="TP")

    # annotate last candle
    last_idx = sub.index[-1]
    last_price = sub["close"].iloc[-1]
    plt.scatter([last_idx], [last_price], color="black", s=20)
    plt.text(last_idx, last_price, f"  {last_price:.6f}", va="bottom", fontsize=9)

    plt.title(f"{symbol} | {action} | Global pattern ({PLOT_CANDLES} bars)")
    plt.legend()
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close()
    return buf.getvalue()

# ---------------- BACKTEST (pattern stats + binomial test + ML prep) ----------------
def backtest_patterns(limit_symbols=30):
    logger.info("=== BACKTEST STARTED ===")
    symbols = fetch_top_symbols(limit=limit_symbols)
    results = []
    all_wins = 0
    all_trades = 0
    interval = "3m"
    limit_per_call = 1000

    for symbol in symbols:
        df = fetch_klines_rest(symbol, interval=interval, limit=limit_per_call)
        if df is None or len(df) < 80:
            continue
        df = apply_pro_features(df, symbol_for_multitf=symbol)
        # simulate scanning through history
        for i in range(60, len(df)-1):
            sub_df = df.iloc[:i+1]
            action, votes, last, confidence = detect_signal_pro(sub_df)
            if action == "WATCH":
                continue
            # next candle (i+1)
            next_row = df.iloc[i+1]
            entry = float(last["close"])
            _, sl, tp = calculate_levels(last, action)
            if action == "LONG":
                win = next_row["high"] >= tp
            else:
                win = next_row["low"] <= tp
            results.append({"symbol": symbol, "action": action, "votes": ",".join(votes), "win": bool(win)})
            all_trades += 1
            if win:
                all_wins += 1

    baseline = (all_wins / all_trades) if all_trades > 0 else 0.5
    logger.info("Baseline winrate across all trades: %.3f (trades=%d wins=%d)", baseline, all_trades, all_wins)

    combos = {}
    for r in results:
        key = r["votes"]
        combos.setdefault(key, {"trades": 0, "wins": 0})
        combos[key]["trades"] += 1
        if r["win"]:
            combos[key]["wins"] += 1

    stats = []
    for k, v in combos.items():
        if v["trades"] < 5:
            continue
        wr = v["wins"] / v["trades"]
        try:
            pval = binomtest(v["wins"], v["trades"], baseline).pvalue
        except Exception:
            pval = 1.0
        stats.append({"pattern_combo": k, "trades": v["trades"], "winrate": wr, "baseline": baseline, "p_value": pval, "significance": pval < 0.05})

    df_stats = pd.DataFrame(stats).sort_values("winrate", ascending=False)
    df_stats.to_csv("patterns_stats.csv", index=False)
    logger.info("=== BACKTEST FINISHED ===")
    if not df_stats.empty:
        logger.info("Saved patterns_stats.csv (top 10):\n%s", df_stats.head(10).to_string(index=False))

    # Prepare ML training dataset (simple)
    try:
        from sklearn.linear_model import LogisticRegression
        import joblib

        features = []
        labels = []
        for r in results:
            votes_set = set(r["votes"].split(",")) if r["votes"] else set()
            feat = [1 if name in votes_set else 0 for name in SIGNAL_NAMES_ORDER]
            features.append(feat)
            labels.append(1 if r["win"] else 0)

        if features:
            model = LogisticRegression(max_iter=500)
            model.fit(features, labels)
            joblib.dump(model, "pattern_model.pkl")
            logger.info("ML model trained and saved (pattern_model.pkl)")
    except Exception as e:
        logger.warning("ML training skipped/failed: %s", e)

    # Send top5 to Telegram (summary)
    try:
        if not df_stats.empty:
            top5 = df_stats.head(5)
            msg = "📊 Backtest Top 5 Patterns (3m TF):\n"
            for _, row in top5.iterrows():
                combo_str = str(row['pattern_combo'])[:80]
                msg += f"- {combo_str} | WR={row['winrate']:.2f} | p={row['p_value']:.3f} | trades={int(row['trades'])}\n"
            send_telegram(msg)
    except Exception as e:
        logger.exception("Failed to send backtest summary: %s", e)

    return df_stats

# ---------------- LIVE ANALYSIS & ALERT ----------------
def analyze_and_alert(symbol: str):
    df = fetch_klines_rest(symbol, interval="3m", limit=1000)
    if df is None or len(df) < 120:
        return
    df = apply_pro_features(df, symbol_for_multitf=symbol)
    action, votes, last, confidence = detect_signal_pro(df)
    if action == "WATCH":
        return

    score = calculate_quality_score_pro(df, votes, confidence)
    entry, sl, tp = calculate_levels(last, action)

    # risk/reward
    rr = None
    try:
        if action == "LONG":
            rr = (tp - entry) / (entry - sl) if (entry - sl) != 0 else None
        else:
            rr = (entry - tp) / (sl - entry) if (sl - entry) != 0 else None
    except Exception:
        rr = None

    # Filters to avoid spam: confidence threshold & score threshold, and RR >= 3.0
    if not (confidence >= CONF_THRESHOLD_MEDIUM and score >= MIN_SCORE_TO_ALERT and (rr is None or rr >= 3.0)):
        logger.debug("Filtered out signal %s (conf=%.3f score=%.3f rr=%s)", symbol, confidence, score, rr)
        return

    # ML filter (if model exists)
    try:
        import joblib
        model = joblib.load("pattern_model.pkl")
        votes_set = set(votes)
        feat = [1 if name in votes_set else 0 for name in SIGNAL_NAMES_ORDER]
        prob = model.predict_proba([feat])[0][1]
        # soft threshold: require model probability reasonably > 0.5
        if prob < 0.55:
            logger.info("ML filter blocked signal for %s (p=%.2f)", symbol, prob)
            return
    except Exception:
        # no model or failed load -> continue
        pass

    emoji = "🟢" if action == "LONG" else "🔴"
    msg = (
        f"⚡ TRADE SIGNAL {emoji}\n"
        f"Symbol: {symbol}\n"
        f"Direction: {action}\n"
        f"Entry (market): {entry:.6f}\n"
        f"Take-Profit: {tp:.6f}\n"
        f"Stop-Loss: {sl:.6f}\n"
        f"R/R: {rr:.2f}\n"
        f"Confidence: {confidence:.2f}\n"
        f"Quality Score: {score:.2f}\n"
        f"Patterns: {', '.join(votes)}"
    )
    try:
        # choose pattern chart (global) for visualization
        chart = plot_pattern_chart(df, symbol, entry, sl, tp, action)
    except Exception as e:
        logger.exception("plotting failed for %s: %s", symbol, e)
        try:
            chart = plot_signal_chart(df, symbol, entry, sl, tp, action)
        except Exception as e2:
            logger.exception("fallback plotting failed for %s: %s", symbol, e2)
            chart = None

    send_telegram(msg, photo=chart)

    # save to state
    state.setdefault("signals", {})[symbol] = {
        "action": action,
        "entry": entry,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "confidence": confidence,
        "score": score,
        "patterns": votes,
        "time": str(last.name),
        "last_price": float(last["close"])
    }
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_json_safe(STATE_FILE, state)

# ---------------- SCAN (parallel) ----------------
def scan_all_symbols(limit=30):
    symbols = fetch_top_symbols(limit=limit)
    if not symbols:
        logger.warning("No symbols fetched for scan")
        return
    logger.info("Starting scan for %d symbols", len(symbols))
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
        list(exe.map(analyze_and_alert, symbols))
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    save_json_safe(STATE_FILE, state)
    logger.info("Scan finished at %s", state["last_scan"])

# ---------------- SIMPLE HTTP SERVER (for Render port binding) ----------------
def start_http():
    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            logger.debug("HTTP: " + format % args)
    port = PORT
    try:
        with socketserver.TCPServer(("", port), Handler) as httpd:
            logger.info("HTTP server listening on port %d", port)
            httpd.serve_forever()
    except Exception as e:
        logger.exception("HTTP server error: %s", e)

# ---------------- MAIN ----------------
if __name__ == "__main__":
    # Start HTTP server early so Render recognizes the port
    threading.Thread(target=start_http, daemon=True).start()

    logger.info("Starting bot: Backtest + Live (3m TF)")

    # Run backtest once at startup (non-blocking heavy; careful with rate limits)
    try:
        df_stats = backtest_patterns(limit_symbols=30)
    except Exception as e:
        logger.exception("Backtest failed: %s", e)

    # Live scanning loop
    try:
        while True:
            try:
                scan_all_symbols(limit=30)
            except Exception as e:
                logger.exception("scan_all_symbols error: %s", e)
            # sleep for 3 minutes
            time.sleep(3 * 60)
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt).")