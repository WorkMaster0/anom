#!/usr/bin/env python3
"""
pretop_smc_bot.py - full script

Heuristics-based Smart Money Concepts detector (order blocks & fair value gaps)
for MEXC spot symbols. NOT LuxAlgo (proprietary).

Features:
 - Scans MEXC spot USDT pairs on multiple timeframes (5m,15m,1h)
 - Detects SMC-like zones (order blocks & fair value gaps) using heuristics
 - Notifies via Telegram when zones are detected and when price reaches them
 - Sends TF chart with zone overlay to Telegram
 - Exposes simple Flask endpoints /scan and /status
"""

import os
import time
import json
import logging
import traceback
import requests
import io
from datetime import datetime, timezone

from threading import Thread
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import numpy as np
import mplfinance as mpf
import matplotlib.pyplot as plt

from flask import Flask, request, jsonify

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "5000"))

MEXC_REST_BASE = "https://api.mexc.com"
TF_LIST = ["5m", "15m", "1h"]
TF_PRIORITY = ["15m", "1h", "5m"]  # higher priority first
SCAN_WORKERS = 8
KLIMIT = 300
STATE_FILE = "state_smc.json"

# heuristic tuning
ORDER_BLOCK_LOOKBACK = 12
MIN_BODY_THRESHOLD = 0.002
FVG_MIN_GAP_PCT = 0.001
ZONE_PADDING_PCT = 0.002
COOLDOWN_MINUTES = 10

# ---------- LOGGING ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("pretop-smc-bot")

# ---------- STATE ----------
def load_state(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.exception("load_state: %s", e)
    return default

def save_state(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        logger.exception("save_state: %s", e)

state = load_state(STATE_FILE, {"zones": {}, "last_scan": None, "events": []})

# ---------- TELEGRAM HELPERS ----------
def escape_md_v2(text: str) -> str:
    # very small escape for markdown v2; fine to extend if needed
    replacements = [("\\","\\\\"), ("_","\\_"), ("*","\\*"), ("[","\\["), ("]","\\]"), ("(","\\("), (")","\\)"), ("~","\\~"),
                    ("`","\\`"), (">","\\>"), ("#","\\#"), ("+","\\+"), ("-","\\-"), ("=","\\="), ("|","\\|"), ("{","\\{"),
                    ("}","\\}"), (".","\\."), ("!","\\!")]
    for a,b in replacements:
        text = text.replace(a,b)
    return text

def send_telegram(text: str, photo_bytes: bytes = None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram not configured; message: %s", text[:140])
        return
    try:
        if photo_bytes is not None:
            files = {'photo': ('chart.png', photo_bytes, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
        if r.status_code != 200:
            logger.warning("Telegram API status %s: %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

# ---------- MEXC REST ----------
def get_spot_symbols(limit=400):
    try:
        r = requests.get(MEXC_REST_BASE + "/api/v3/ticker/24hr", timeout=10)
        data = r.json()
        syms = [d['symbol'] for d in data if d.get('symbol','').endswith("USDT")]
        return syms[:limit]
    except Exception as e:
        logger.exception("get_spot_symbols: %s", e)
        return ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","ADAUSDT"]

def fetch_klines(symbol: str, interval: str, limit: int = 500):
    try:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(MEXC_REST_BASE + "/api/v3/klines", params=params, timeout=12)
        arr = r.json()
        if not isinstance(arr, list):
            return None
        df = pd.DataFrame(arr, columns=[ "open_time","open","high","low","close","volume","close_time","qav","trades","taker_base","taker_quote","ignore" ])
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df.set_index("open_time", inplace=True)
        return df
    except Exception as e:
        logger.exception("fetch_klines error %s %s: %s", symbol, interval, e)
        return None

# ---------- SMC HEURISTICS ----------
def detect_order_blocks(df: pd.DataFrame):
    zones = []
    n = len(df)
    start = max(2, n - ORDER_BLOCK_LOOKBACK - 3)
    for i in range(start, n-1):
        c = df.iloc[i]; p = df.iloc[i-1]
        body = abs(c['close'] - c['open'])
        body_pct = body / c['close'] if c['close'] else 0
        if body_pct < MIN_BODY_THRESHOLD:
            continue
        # Bullish order block candidate
        if c['close'] > c['open'] and p['close'] < p['open'] and c['open'] < p['close']:
            low = min(c['open'], c['close'], p['low']); high = max(c['open'], c['close'], p['high'])
            zones.append({'type':'orderblock','side':'bull','zone':[low, high],'index':i})
        # Bearish order block
        if c['close'] < c['open'] and p['close'] > p['open'] and c['open'] > p['close']:
            low = min(c['open'], c['close'], p['low']); high = max(c['open'], c['close'], p['high'])
            zones.append({'type':'orderblock','side':'bear','zone':[low, high],'index':i})
    return zones

def detect_fvg(df: pd.DataFrame):
    zones = []
    n = len(df)
    start = max(0, n - ORDER_BLOCK_LOOKBACK - 4)
    for i in range(start, n-2):
        A = df.iloc[i]; B = df.iloc[i+1]; C = df.iloc[i+2]
        # bullish FVG
        if B['close'] < B['open'] and A['high'] < C['low']:
            gap_size = (C['low'] - A['high']) / (A['high'] if A['high'] else 1)
            if gap_size >= FVG_MIN_GAP_PCT:
                zones.append({'type':'fvg','side':'bull','zone':[A['high'], C['low']],'index':i+1})
        # bearish FVG
        if B['close'] > B['open'] and A['low'] > C['high']:
            gap_size = (A['low'] - C['high']) / (C['high'] if C['high'] else 1)
            if gap_size >= FVG_MIN_GAP_PCT:
                zones.append({'type':'fvg','side':'bear','zone':[C['high'], A['low']],'index':i+1})
    return zones

def expand_zone(zone):
    low, high = zone
    pad = (high - low) * ZONE_PADDING_PCT
    return [low - pad, high + pad]

# ---------- CHARTS ----------
def build_chart_bytes(df: pd.DataFrame, symbol: str, interval: str, zone=None):
    try:
        fig, axes = mpf.plot(df.tail(200), type='candle', style='yahoo', returnfig=True)
        ax = axes[0] if isinstance(axes, list) else axes
        if zone is not None:
            low, high = zone
            ax.axhspan(low, high, alpha=0.25, color='orange')
        ax.set_title(f"{symbol} [{interval}]")
        buf = io.BytesIO()
        fig.savefig(buf, format='png', bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        logger.exception("build_chart_bytes error: %s", e)
        return None

# ---------- FIND ZONES FOR SYMBOL/TF ----------
def find_zones(symbol, tf):
    try:
        df = fetch_klines(symbol, tf, limit=KLIMIT)
        if df is None or len(df) < 60:
            return []
        ob = detect_order_blocks(df)
        fvg = detect_fvg(df)
        zones = ob + fvg
        # expand and attach metadata
        results = []
        for z in zones:
            expanded = expand_zone(z['zone'])
            results.append({
                'id': f"{symbol}|{tf}|{z['type']}|{z['index']}",
                'symbol': symbol,
                'tf': tf,
                'type': z['type'],
                'side': z['side'],
                'zone': expanded,
                'detected_at': datetime.now(timezone.utc).isoformat(),
                'hit_at': None
            })
        return results
    except Exception as e:
        logger.exception("find_zones error %s %s: %s", symbol, tf, e)
        return []

# ---------- SCAN (multi-threaded) ----------
def scan_symbols(symbols):
    logger.info("Starting scan for %d symbols", len(symbols))
    new_count = 0
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = []
        for s in symbols:
            for tf in TF_LIST:
                futures.append(ex.submit(find_zones, s, tf))
        for f in futures:
            try:
                zs = f.result(timeout=60)
                if not zs:
                    continue
                for z in zs:
                    symzones = state.setdefault('zones', {}).setdefault(z['symbol'], [])
                    if any(existing['id'] == z['id'] for existing in symzones):
                        continue
                    symzones.append(z)
                    new_count += 1
                    # notify detection
                    text = (f"ð Zone detected\nSymbol: {z['symbol']}\nTF: {z['tf']}\nType: {z['type']}\n"
                            f"Zone: {z['zone'][0]:.6f} - {z['zone'][1]:.6f}\nDetected: {z['detected_at']}")
                    try:
                        df_chart = fetch_klines(z['symbol'], z['tf'], limit=200)
                        img = build_chart_bytes(df_chart, z['symbol'], z['tf'], zone=z['zone'])
                    except Exception:
                        img = None
                    send_telegram(text, photo_bytes=img)
            except Exception as e:
                logger.exception("scan_symbols worker error: %s", e)
    state['last_scan'] = datetime.now(timezone.utc).isoformat()
    save_state(STATE_FILE, state)
    logger.info("Scan finished; new zones=%d", new_count)

# ---------- ZONE HIT MONITOR ----------
def monitor_hits():
    cooldown_s = COOLDOWN_MINUTES * 60
    while True:
        try:
            now = datetime.now(timezone.utc)
            for symbol, zones in list(state.get('zones', {}).items()):
                # fast fetch latest price via 1m kline
                df_latest = fetch_klines(symbol, "1m", limit=2)
                if df_latest is None or df_latest.empty:
                    continue
                last_price = float(df_latest['close'].iloc[-1])
                for z in zones:
                    if z.get('hit_at'):
                        continue
                    low, high = z['zone']
                    if low <= last_price <= high:
                        detected = pd.to_datetime(z['detected_at'])
                        elapsed = now - detected.to_pydatetime() if hasattr(detected, 'to_pydatetime') else now - pd.to_datetime(z['detected_at'])
                        txt = (f"â Zone HIT\nSymbol: {symbol}\nTF: {z['tf']}\nType: {z['type']}\n"
                               f"Price: {last_price:.6f}\nZone: {low:.6f} - {high:.6f}\nDetected: {z['detected_at']}\nTime to hit: {elapsed}")
                        df_chart = fetch_klines(symbol, z['tf'], limit=200)
                        img = build_chart_bytes(df_chart, symbol, z['tf'], zone=z['zone']) if df_chart is not None else None
                        send_telegram(txt, photo_bytes=img)
                        z['hit_at'] = now.isoformat()
                        state.setdefault('events', []).append({'symbol':symbol,'zone_id':z['id'],'hit_at':now.isoformat(),'price':last_price})
                        save_state(STATE_FILE, state)
        except Exception as e:
            logger.exception("monitor_hits error: %s", e)
        time.sleep(15)

# ---------- FLASK ----------
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({"status":"ok","last_scan":state.get('last_scan'), "zones_total": sum(len(v) for v in state.get('zones', {}).values())})

@app.route("/scan", methods=["GET","POST"])
def http_scan():
    Thread(target=lambda: scan_symbols(get_spot_list()), daemon=True).start()
    return jsonify({"ok":True,"message":"scan started"})

@app.route("/status")
def http_status():
    return jsonify({"zones": state.get('zones', {}), "events": state.get('events', [])})

def get_spot_list():
    try:
        syms = get_spot_symbols()
        syms = [s for s in syms if s.endswith("USDT")]
        return syms[:200]
    except Exception:
        return ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]

def get_spot_symbols():
    try:
        r = requests.get(MEXC_REST_BASE + "/api/v3/ticker/24hr", timeout=10)
        data = r.json()
        syms = [d['symbol'] for d in data if d.get('symbol','').endswith("USDT")]
        return syms
    except Exception as e:
        logger.exception("get_spot_symbols error: %s", e)
        return []

# ---------- START ----------
if __name__ == "__main__":
    logger.info("Starting pretop SMC-like bot (heuristic)")
    Thread(target=monitor_hits, daemon=True).start()
    Thread(target=lambda: scan_symbols(get_spot_list()), daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)