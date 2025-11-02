#!/usr/bin/env python3
"""
monolith_pretop.py
------------------
One-file production-ready pre-top detector.

Features:
- Async WebSocket-first kline manager (aiohttp)
- Async REST fallback with Redis cache (aioredis) and aiolimiter
- Adaptive rate-limiter (decrease on 429)
- Signal detection (EMA/RSI/MACD/ATR + candle/volume/levels)
- Plotting with mplfinance (run in executor) — fixed addplot length issues
- FastAPI app with Prometheus metrics and Telegram webhook
- Redis-backed state & cooldown to avoid duplicate signals
- Simple backtester skeleton
- Config via environment variables

Required env vars (recommended):
- TELEGRAM_TOKEN
- CHAT_ID
- BINANCE_API_KEY (optional)
- BINANCE_API_SECRET (optional)
- REDIS_URL (default redis://localhost:6379/0)

This is intentionally self-contained. At top of file you'll see a "requirements" and optional docker-compose snippet in the comments.
"""

# ----------------- Requirements (paste into requirements.txt) -----------------
# fastapi
# uvicorn[standard]
# aiohttp
# aioredis
# aiolimiter
# httpx
# pandas
# numpy
# matplotlib
# mplfinance
# ta
# python-binance
# prometheus-client
# python-dotenv
# python-multipart
# pytest (for tests if you add them)
# ------------------------------------------------------------------------------

import os
import asyncio
import json
import logging
import math
import random
import time
from collections import deque
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List, Dict
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import aioredis
from aiolimiter import AsyncLimiter
import httpx
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import mplfinance as mpf
import ta

from fastapi import FastAPI, BackgroundTasks
from prometheus_client import Counter, Gauge, make_asgi_app
import uvicorn

# ---------------- Logging ----------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("monolith-pretop")

# ---------------- Config ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "8000"))
WS_SYMBOLS = os.getenv("WS_SYMBOLS", "BTCUSDT,ETHUSDT").split(",")  # default WS subscriptions
SCAN_SYMBOLS = os.getenv("SCAN_SYMBOLS", ",".join(WS_SYMBOLS)).split(",")  # symbols to scan if REST used
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
BINANCE_REST_URL = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_WS_BASE = "wss://fstream.binance.com/stream?streams="
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REST_RATE_LIMIT_PER_MIN = int(os.getenv("REST_RATE_LIMIT_PER_MIN", "1800"))  # start value
REST_CACHE_TTL = int(os.getenv("REST_CACHE_TTL", "60"))  # seconds
AGGRESSION = float(os.getenv("AGGRESSION", "0.3"))
CONF_THRESHOLD_MEDIUM = float(os.getenv("CONF_THRESHOLD_MEDIUM", "0.3"))
SIGNAL_COOLDOWN_MIN = int(os.getenv("SIGNAL_COOLDOWN_MIN", "60"))
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "60"))
STATE_KEY = os.getenv("STATE_KEY", "pretop:state")
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "500"))
MPLF_DPI = int(os.getenv("MPLF_DPI", "150"))

# Adaptive rate control params
ADAPTIVE_DECAY_FACTOR = float(os.getenv("ADAPTIVE_DECAY_FACTOR", "0.7"))  # multiply rate by this on 429
ADAPTIVE_RECOVER_STEP = int(os.getenv("ADAPTIVE_RECOVER_STEP", "10"))    # increase rate by this per successful minute

# Threadpool for plotting (blocking)
_executor = ThreadPoolExecutor(max_workers=2)

# ---------------- Prometheus metrics ----------------
SCAN_COUNTER = Counter("pretop_scans_total", "Total scans performed")
SIGNAL_COUNTER = Counter("pretop_signals_total", "Total signals emitted")
LAST_SCAN_GAUGE = Gauge("pretop_last_scan_unix", "Last scan UNIX timestamp")

# ---------------- Redis client (initialized async) ----------------
_redis = None

async def get_redis():
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    return _redis

# ---------------- Async HTTP client (httpx) ----------------
_httpx_client = None
def get_httpx_client():
    global _httpx_client
    if _httpx_client is None:
        _httpx_client = httpx.AsyncClient(timeout=10.0)
    return _httpx_client

# ---------------- Rate limiter (async) ----------------
# We'll recreate limiter when adaptive rate changes
_rate_lock = asyncio.Lock()
_rate_per_min = REST_RATE_LIMIT_PER_MIN
_limiter = AsyncLimiter(_rate_per_min, 60)

async def set_rate_per_min(new_rate: int):
    global _rate_per_min, _limiter
    async with _rate_lock:
        _rate_per_min = max(1, int(new_rate))
        _limiter = AsyncLimiter(_rate_per_min, 60)
        logger.info("REST rate updated to %d req/min", _rate_per_min)

async def decrease_rate_on_429():
    global _rate_per_min
    new_rate = max(1, int(_rate_per_min * ADAPTIVE_DECAY_FACTOR))
    await set_rate_per_min(new_rate)
    logger.warning("Adaptive: decreased REST rate to %d due to 429", new_rate)

async def try_recover_rate():
    # linear recovery
    global _rate_per_min
    new_rate = _rate_per_min + ADAPTIVE_RECOVER_STEP
    await set_rate_per_min(new_rate)

# ---------------- WebSocket Kline Manager (async) ----------------
class AsyncWebSocketKlineManager:
    """
    Combined stream manager for Binance futures klines.
    Keeps in-memory store per symbol as pandas DataFrame (recent N).
    """
    def __init__(self, symbols: List[str], interval: str = "15m", max_rows: int = 2000):
        self.symbols = [s.upper() for s in symbols]
        self.interval = interval
        self.max_rows = max_rows
        self._store: Dict[str, pd.DataFrame] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._last_connect = 0.0

    def _build_url(self):
        streams = "/".join([f"{s.lower()}@kline_{self.interval}" for s in self.symbols])
        return BINANCE_WS_BASE + streams

    async def start(self):
        if self._running:
            return
        self._running = True
        self._session = aiohttp.ClientSession()
        self._ws_task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
        if self._session:
            await self._session.close()

    async def _run_loop(self):
        backoff = 1
        while self._running:
            try:
                url = self._build_url()
                self._last_connect = time.time()
                logger.info("Connecting WS to %s", url[:200])
                async with self._session.ws_connect(url, heartbeat=30, max_msg_size=0) as ws:
                    logger.info("WS connected")
                    backoff = 1
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_msg(msg.data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.warning("WS error: %s", msg)
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.info("WS closed")
                            break
            except Exception as e:
                logger.exception("WS loop error: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(60, backoff * 2)
            # small wait before reconnect
            await asyncio.sleep(1)

    async def _handle_msg(self, raw: str):
        try:
            payload = json.loads(raw)
            data = payload.get("data")
            if not data:
                return
            k = data.get("k")
            if not k:
                return
            symbol = k.get("s")
            if not symbol:
                return
            open_t = int(k.get("t"))
            row = {
                "open_time": pd.to_datetime(open_t, unit="ms"),
                "open": float(k.get("o")),
                "high": float(k.get("h")),
                "low": float(k.get("l")),
                "close": float(k.get("c")),
                "volume": float(k.get("v"))
            }
            async with self._lock:
                df = self._store.get(symbol)
                new_row = pd.DataFrame([row]).set_index("open_time")
                if df is None:
                    df = new_row
                else:
                    # upsert last index
                    if new_row.index[0] == df.index[-1]:
                        # replace last
                        df.iloc[-1] = new_row.iloc[0]
                    else:
                        df = pd.concat([df, new_row])
                self._store[symbol] = df.tail(self.max_rows)
        except Exception:
            logger.exception("Failed to handle WS message")

    async def get_klines(self, symbol: str, limit: int = 500) -> Optional[pd.DataFrame]:
        async with self._lock:
            df = self._store.get(symbol.upper())
            if df is None:
                return None
            return df.tail(limit).copy()

# ---------------- REST client with cache (Redis) ----------------
async def fetch_klines_rest_cached(symbol: str, interval: str = "15m", limit: int = 500, cache_ttl: int = REST_CACHE_TTL) -> Optional[pd.DataFrame]:
    """
    Uses Redis to cache DataFrame JSON. Rate-limited by AsyncLimiter; adaptive backoff on 429.
    """
    r = await get_redis()
    key = f"klines:{symbol}:{interval}:{limit}"
    cached = await r.get(key)
    if cached:
        try:
            df = pd.read_json(cached)
            if "open_time" in df.columns:
                df.set_index("open_time", inplace=True)
            return df
        except Exception:
            logger.exception("Failed to load cached klines for %s; ignoring cache", symbol)

    client = get_httpx_client()
    # wait for limiter slot
    async with _limiter:
        try:
            params = {"symbol": symbol, "interval": interval, "limit": limit}
            resp = await client.get(BINANCE_REST_URL, params=params)
            if resp.status_code == 429:
                # adaptive decrease
                logger.warning("REST 429 for %s", symbol)
                await decrease_rate_on_429()
                return None
            if resp.status_code >= 400:
                logger.error("REST error %s %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            df = pd.DataFrame(data, columns=[
                "open_time","open","high","low","close","volume",
                "close_time","quote_asset_volume","trades",
                "taker_buy_base","taker_buy_quote","ignore"
            ])
            for col in ["open","high","low","close","volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
            df.set_index("open_time", inplace=True)
            # cache to redis
            try:
                await r.set(key, df.to_json(date_format="iso"), ex=cache_ttl)
                # successful REST call -> try to recover rate a bit
                await try_recover_rate()
            except Exception:
                logger.exception("Redis set failed for %s", key)
            return df
        except httpx.RequestError:
            logger.exception("HTTPX request error for %s", symbol)
            return None

# ---------------- Utilities ----------------
def safe_json_dumps(obj):
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return str(obj)

async def send_telegram_message(text: str, photo_bytes: Optional[bytes] = None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto" if photo_bytes else f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        if photo_bytes:
            # multipart
            data = {"chat_id": CHAT_ID, "caption": text, "parse_mode": "MarkdownV2"}
            files = {"photo": ("signal.png", photo_bytes, "image/png")}
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, data=data, timeout=10, ssl=False, params=None) as resp:
                    if resp.status != 200:
                        logger.warning("Telegram sendPhoto status %s", resp.status)
        else:
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "MarkdownV2"}
            async with aiohttp.ClientSession() as sess:
                async with sess.post(url, json=payload, timeout=10, ssl=False) as resp:
                    if resp.status != 200:
                        logger.warning("Telegram sendMessage status %s", resp.status)
    except Exception:
        logger.exception("send_telegram_message error")

# MarkdownV2 escape
MDV2_SPECIAL = r'_*[]()~`>#+-=|{}.!'
import re
_mdv2_re = re.compile(r'([%s])' % re.escape(MDV2_SPECIAL))
def escape_md_v2(text: str) -> str:
    return _mdv2_re.sub(r'\\\1', str(text))

# ---------------- Feature engineering & signals (pure functions) ----------------
def apply_all_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().dropna()
    # candle metrics
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_shadow"] = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_shadow"] = df[["close", "open"]].min(axis=1) - df["low"]

    look = 20
    df["support"] = df["low"].rolling(look).min()
    df["resistance"] = df["high"].rolling(look).max()

    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["vol_spike"] = df["volume"] > 1.5 * df["vol_ma20"]

    # technicals via ta
    try:
        df["ema9"] = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
        df["ema21"] = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()
        df["rsi14"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
        macd = ta.trend.MACD(df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["atr14"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()
        bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
        df["bb_high"] = bb.bollinger_hband()
        df["bb_low"] = bb.bollinger_lband()
    except Exception:
        logger.exception("ta indicators failed")
    return df

def detect_signal_v2(df: pd.DataFrame) -> Tuple[str, List[str], bool, pd.Series, float]:
    df = df.copy().dropna()
    if len(df) < 20:
        last = df.iloc[-1] if len(df) else pd.Series()
        return "WATCH", [], False, last, 0.0

    last = df.iloc[-1]
    prev = df.iloc[-2]
    votes = []
    confidence = 0.45

    # hammer / shooting star
    if last["lower_shadow"] > 2 * max(abs(last["body"]), 1e-9) and last["body"] > 0:
        votes.append("hammer_bull"); confidence += 0.08
    if last["upper_shadow"] > 2 * max(abs(last["body"]), 1e-9) and last["body"] < 0:
        votes.append("shooting_star"); confidence += 0.08

    # engulfing
    if last["body"] > 0 and prev["body"] < 0 and last["close"] > prev["open"] and last["open"] < prev["close"]:
        votes.append("bullish_engulfing"); confidence += 0.06
    if last["body"] < 0 and prev["body"] > 0 and last["close"] < prev["open"] and last["open"] > prev["close"]:
        votes.append("bearish_engulfing"); confidence += 0.06

    # doji
    if abs(last["body"]) < 0.12 * max(last["range"], 1e-9):
        votes.append("doji"); confidence += 0.03

    # inside/outside
    if last["high"] < prev["high"] and last["low"] > prev["low"]:
        votes.append("inside_bar"); confidence += 0.03
    if last["high"] > prev["high"] and last["low"] < prev["low"]:
        votes.append("outside_bar"); confidence += 0.03

    # 3 candles
    if len(df) >= 3:
        if all(df["close"].iloc[-i] > df["open"].iloc[-i] for i in range(1, 4)):
            votes.append("3_green"); confidence += 0.03
        if all(df["close"].iloc[-i] < df["open"].iloc[-i] for i in range(1, 4)):
            votes.append("3_red"); confidence += 0.03

    # volume
    if last.get("vol_spike", False):
        votes.append("volume_spike"); confidence += 0.04
    if last["volume"] > 2 * df["vol_ma20"].iloc[-1]:
        votes.append("climax_volume"); confidence += 0.04

    # structure flips
    if prev["close"] > prev.get("resistance", 0) and last["close"] < last.get("resistance", 0):
        votes.append("fake_breakout_short"); confidence += 0.04
    if prev["close"] < prev.get("support", 0) and last["close"] > last.get("support", 0):
        votes.append("fake_breakout_long"); confidence += 0.04
    if prev["close"] < prev.get("resistance", 0) and last["close"] > last.get("resistance", 0):
        votes.append("resistance_flip_support"); confidence += 0.04
    if prev["close"] > prev.get("support", 0) and last["close"] < last.get("support", 0):
        votes.append("support_flip_resistance"); confidence += 0.04

    # retest/liquidity
    if last.get("support") and abs(last["close"] - last["support"]) / max(last["support"], 1e-9) < 0.003 and last["body"] > 0:
        votes.append("support_retest"); confidence += 0.03
    if last.get("resistance") and abs(last["close"] - last["resistance"]) / max(last["resistance"], 1e-9) < 0.003 and last["body"] < 0:
        votes.append("resistance_retest"); confidence += 0.03
    if last["low"] < last.get("support", 0) and last["close"] > last.get("support", 0):
        votes.append("liquidity_grab_long"); confidence += 0.03
    if last["high"] > last.get("resistance", 0) and last["close"] < last.get("resistance", 0):
        votes.append("liquidity_grab_short"); confidence += 0.03

    # trend via ema
    trend_val = df.get("ema21", pd.Series(df["close"].rolling(20).mean())).iloc[-1]
    if last["close"] > trend_val:
        votes.append("above_trend"); confidence += 0.02
    else:
        votes.append("below_trend"); confidence += 0.02

    # rsi/macd
    if "rsi14" in df.columns and last["rsi14"] < 30:
        votes.append("rsi_oversold"); confidence += 0.03
    if "rsi14" in df.columns and last["rsi14"] > 70:
        votes.append("rsi_overbought"); confidence += 0.03
    if "macd" in df.columns and "macd_signal" in df.columns:
        if last["macd"] > last["macd_signal"] and prev["macd"] <= prev["macd_signal"]:
            votes.append("macd_cross_up"); confidence += 0.03
        if last["macd"] < last["macd_signal"] and prev["macd"] >= prev["macd_signal"]:
            votes.append("macd_cross_down"); confidence += 0.03

    # pretop detection
    pretop = False
    if len(df) >= 10 and (last["close"] - df["close"].iloc[-10]) / max(df["close"].iloc[-10], 1e-9) > 0.10:
        pretop = True
        votes.append("pretop"); confidence += 0.06

    # action by proximity
    near_resistance = last.get("resistance") and last["close"] >= last["resistance"] * 0.985
    near_support = last.get("support") and last["close"] <= last["support"] * 1.015
    action = "WATCH"
    if near_resistance:
        action = "SHORT"
    elif near_support:
        action = "LONG"

    # aggression
    if AGGRESSION > 0:
        if random.random() < 0.02 + 0.10 * AGGRESSION:
            confidence += 0.08 * AGGRESSION
            votes.append("random_nudge")
        if action == "WATCH" and AGGRESSION > 0.6:
            if last["close"] >= last.get("resistance", 0) * 0.97:
                action = "SHORT"; votes.append("aggressive_near_res")
            if last["close"] <= last.get("support", 1e18) * 1.03:
                action = "LONG"; votes.append("aggressive_near_sup")

    confidence = max(0.0, min(1.0, confidence))
    return action, votes, pretop, last, confidence

# ---------------- Plotter (run in executor) ----------------
def _plot_sync(df: pd.DataFrame, symbol: str, action: str, tp1=None, tp2=None, tp3=None, sl=None, entry=None) -> bytes:
    # avoid modifying original
    df_tail = df.tail(200).copy()
    N = len(df_tail)
    if N == 0:
        return b""
    # create addplots with correct length
    def line(y):
        return [y] * N if y is not None else None
    addplots = []
    if tp1 is not None: addplots.append(mpf.make_addplot(line(tp1), linestyle="--"))
    if tp2 is not None: addplots.append(mpf.make_addplot(line(tp2), linestyle="--"))
    if tp3 is not None: addplots.append(mpf.make_addplot(line(tp3), linestyle="--"))
    if sl is not None:  addplots.append(mpf.make_addplot(line(sl), linestyle="--"))
    if entry is not None: addplots.append(mpf.make_addplot(line(entry), linestyle="--"))

    fig, ax = mpf.plot(df_tail, type='candle', style='yahoo', title=f"{symbol} - {action}", addplot=addplots, returnfig=True)
    buf = None
    try:
        import io
        bufio = io.BytesIO()
        fig.savefig(bufio, format='png', bbox_inches='tight', dpi=MPLF_DPI)
        bufio.seek(0)
        buf = bufio.read()
    finally:
        plt.close(fig)
    return buf or b""

async def plot_signal_bytes(df: pd.DataFrame, symbol: str, action: str, tp1=None, tp2=None, tp3=None, sl=None, entry=None) -> bytes:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _plot_sync, df, symbol, action, tp1, tp2, tp3, sl, entry)

# ---------------- State management (Redis-based) ----------------
async def get_state():
    r = await get_redis()
    raw = await r.get(STATE_KEY)
    if not raw:
        return {"signals": {}, "last_scan": None}
    try:
        return json.loads(raw)
    except Exception:
        logger.exception("Failed to parse state JSON")
        return {"signals": {}, "last_scan": None}

async def save_state(state_obj):
    r = await get_redis()
    try:
        await r.set(STATE_KEY, json.dumps(state_obj, default=str))
    except Exception:
        logger.exception("Failed to save state")

async def can_send_signal(symbol: str, cooldown_min: int = SIGNAL_COOLDOWN_MIN) -> bool:
    state_obj = await get_state()
    sig = state_obj.get("signals", {}).get(symbol)
    if not sig:
        return True
    last_time = sig.get("time")
    if not last_time:
        return True
    try:
        last_dt = datetime.fromisoformat(last_time)
        return datetime.now(timezone.utc) - last_dt >= timedelta(minutes=cooldown_min)
    except Exception:
        return True

# ---------------- Analyzer orchestration ----------------
async def analyze_symbol(symbol: str, ws_mgr: AsyncWebSocketKlineManager):
    # try ws-first
    df = None
    if ws_mgr:
        try:
            df = await ws_mgr.get_klines(symbol, limit=KLINES_LIMIT)
        except Exception:
            logger.exception("WS get_klines failed for %s", symbol)
            df = None
    if df is None:
        df = await fetch_klines_rest_cached(symbol, limit=KLINES_LIMIT)
    if df is None or len(df) < 40:
        logger.debug("Not enough data for %s", symbol)
        return

    df = apply_all_features(df)
    action, votes, pretop, last, confidence = detect_signal_v2(df)
    if action == "WATCH":
        return

    # compute entry/sl/tps robustly
    entry = stop_loss = tp1 = tp2 = tp3 = None
    try:
        if action == "LONG":
            entry = (last.get("support") or last["close"]) * 1.001
            stop_loss = entry - max(1.5 * last.get("atr14", 0.0), entry * 0.01)
            tp1 = entry + (last.get("resistance", entry) - entry) * 0.33
            tp2 = entry + (last.get("resistance", entry) - entry) * 0.66
            tp3 = last.get("resistance") or entry * 1.06
        elif action == "SHORT":
            entry = (last.get("resistance") or last["close"]) * 0.999
            stop_loss = entry + max(1.5 * last.get("atr14", 0.0), entry * 0.01)
            tp1 = entry - (entry - last.get("support", entry)) * 0.33
            tp2 = entry - (entry - last.get("support", entry)) * 0.66
            tp3 = last.get("support") or entry * 0.94
    except Exception:
        logger.exception("Entry/SL/TP calc failed for %s", symbol)
        return

    # rr
    try:
        if action == "LONG":
            denom = (entry - stop_loss) if (entry - stop_loss) != 0 else 1e-9
            rr1 = (tp1 - entry) / denom
        else:
            denom = (stop_loss - entry) if (stop_loss - entry) != 0 else 1e-9
            rr1 = (entry - tp1) / denom
    except Exception:
        logger.exception("RR calc failed")
        return

    # aggression-adjusted threshold
    rr_min_required = max(0.5, 2.0 - 1.5 * AGGRESSION)
    allow_signal = (confidence >= CONF_THRESHOLD_MEDIUM and rr1 >= rr_min_required)
    if not allow_signal and AGGRESSION > 0.7 and random.random() < 0.02 * AGGRESSION:
        allow_signal = True
        votes.append("aggressive_override")

    if not allow_signal:
        logger.debug("Signal not allowed for %s (conf=%.2f rr1=%.2f)", symbol, confidence, rr1)
        return

    if not await can_send_signal(symbol):
        logger.debug("Cooldown blocks signal for %s", symbol)
        return

    # prepare message
    reasons = votes or ["pattern"]
    msg = (
        f"⚡ TRADE SIGNAL\n"
        f"Symbol: {symbol}\n"
        f"Action: {action}\n"
        f"Entry: {entry:.8f}\n"
        f"Stop-Loss: {stop_loss:.8f}\n"
        f"TP1: {tp1:.8f} (RR {rr1:.2f})\n"
        f"TP2: {tp2:.8f}\n"
        f"TP3: {tp3:.8f}\n"
        f"Confidence: {confidence:.2f}\n"
        f"Reasons: {', '.join(reasons)}\n"
    )
    msg_escaped = escape_md_v2(msg)

    # plotting (in executor)
    try:
        img = await plot_signal_bytes(df, symbol, action, tp1=tp1, tp2=tp2, tp3=tp3, sl=stop_loss, entry=entry)
    except Exception:
        logger.exception("Plotting error")
        img = None

    # send to telegram (async)
    await send_telegram_message(msg_escaped, photo_bytes=img)

    # persist to state
    state_obj = await get_state()
    state_obj.setdefault("signals", {})[symbol] = {
        "action": action, "entry": entry, "sl": stop_loss, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "confidence": confidence, "time": datetime.now(timezone.utc).isoformat(), "votes": votes
    }
    await save_state(state_obj)
    SIGNAL_COUNTER.inc()
    logger.info("Signal emitted for %s conf=%.2f", symbol, confidence)

# ---------------- Scan orchestration ----------------
async def scan_all_symbols(ws_mgr: AsyncWebSocketKlineManager, symbols: List[str], workers: int = PARALLEL_WORKERS):
    SCAN_COUNTER.inc()
    LAST_SCAN_GAUGE.set_to_current_time()
    logger.info("Starting scan for %d symbols (workers=%d)", len(symbols), workers)
    sem = asyncio.Semaphore(workers)
    async def worker(sym):
        async with sem:
            try:
                await analyze_symbol(sym, ws_mgr)
            except Exception:
                logger.exception("analyze_symbol failed for %s", sym)
    tasks = [asyncio.create_task(worker(sym)) for sym in symbols]
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Scan completed")

# ---------------- Backtester skeleton ----------------
def backtest_on_df(df: pd.DataFrame):
    """
    Simple backtest skeleton. For each bar, call detect_signal_v2(window) and simulate until TP/SL.
    This is a skeleton — you can expand with fees, position sizing, slippage.
    """
    trades = []
    for idx in range(30, len(df)):
        window = df.iloc[:idx+1].copy()
        window = apply_all_features(window)
        action, votes, pretop, last, confidence = detect_signal_v2(window)
        if action == "WATCH":
            continue
        # naive simulation: test following bars
        entry = last["close"]
        if action == "LONG":
            sl = entry * 0.99
            tp = entry * 1.02
            for fut_idx in range(idx+1, min(idx+50, len(df))):
                h = df.iloc[fut_idx]["high"]
                l = df.iloc[fut_idx]["low"]
                if h >= tp:
                    trades.append({"symbol": "backtest", "entry_idx": idx, "exit_idx": fut_idx, "result": "win"})
                    break
                if l <= sl:
                    trades.append({"symbol": "backtest", "entry_idx": idx, "exit_idx": fut_idx, "result": "loss"})
                    break
        else:
            sl = entry * 1.01
            tp = entry * 0.98
            for fut_idx in range(idx+1, min(idx+50, len(df))):
                h = df.iloc[fut_idx]["high"]
                l = df.iloc[fut_idx]["low"]
                if l <= tp:
                    trades.append({"symbol": "backtest", "entry_idx": idx, "exit_idx": fut_idx, "result": "win"})
                    break
                if h >= sl:
                    trades.append({"symbol": "backtest", "entry_idx": idx, "exit_idx": fut_idx, "result": "loss"})
                    break
    return trades

# ---------------- FastAPI App ----------------
app = FastAPI(title="Monolith Pretop Bot")
# mount prometheus /metrics
app.mount("/metrics", make_asgi_app())

# instantiate WS manager globally
ws_manager = AsyncWebSocketKlineManager(symbols=WS_SYMBOLS, interval="15m", max_rows=2000)

@app.on_event("startup")
async def on_startup():
    logger.info("Starting application...")
    # redis warmup
    try:
        await get_redis()
    except Exception:
        logger.exception("Redis connection on startup failed")
    # start ws manager
    try:
        await ws_manager.start()
    except Exception:
        logger.exception("WS start failed")
    # schedule periodic scanner
    asyncio.create_task(periodic_scan_loop())

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await ws_manager.stop()
    except Exception:
        logger.exception("WS stop failed")
    # close httpx client
    global _httpx_client
    if _httpx_client:
        await _httpx_client.aclose()
    # close redis
    global _redis
    if _redis:
        await _redis.close()

@app.get("/")
async def root():
    state_obj = await get_state()
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat(), "signals": len(state_obj.get("signals", {}))}

@app.post("/telegram_webhook/{token}")
async def telegram_webhook(token: str, background: BackgroundTasks, payload: dict):
    if token != TELEGRAM_TOKEN:
        return {"ok": False, "error": "invalid token"}
    text = (payload.get("message", {}) or {}).get("text", "") or ""
    if text.startswith("/scan"):
        background.add_task(asyncio.create_task, scan_all_symbols(ws_manager, SCAN_SYMBOLS, PARALLEL_WORKERS))
        return {"ok": True, "msg": "scan started"}
    if text.startswith("/state"):
        st = await get_state()
        top = sorted(st.get("signals", {}).items(), key=lambda x: x[1].get("confidence", 0), reverse=True)[:5]
        summary = "\n".join([f"{k}: {v.get('action')} conf={v.get('confidence'):.2f}" for k, v in top])
        await send_telegram_message(escape_md_v2("Top signals:\n" + summary))
        return {"ok": True}
    return {"ok": True}

async def periodic_scan_loop():
    while True:
        try:
            await scan_all_symbols(ws_manager, SCAN_SYMBOLS, PARALLEL_WORKERS)
        except Exception:
            logger.exception("Periodic scan error")
        await asyncio.sleep(SCAN_INTERVAL_SEC)

# ---------------- Run ----------------
if __name__ == "__main__":
    logger.info("Launching monolith pretop bot")
    uvicorn.run("monolith_pretop:app", host="0.0.0.0", port=PORT, log_level="info")