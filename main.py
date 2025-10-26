#!/usr/bin/env python3
"""
smc_webhook_render_bot.py

Render-ready Telegram webhook bot that:
 - On /scan analyzes ALL USDT spot pairs on MEXC for SMC-like zones (order blocks & FVG) on 2h TF
 - Saves detected zones to state in /data/state.json and monitors them in background
 - Notifies Telegram when price reaches a detected reversal zone (with elapsed time + chart)
 - Installs Telegram webhook automatically using RENDER_EXTERNAL_URL (if present)
 - Supports /pair <SYMBOL>, /status, /help
 - Optimizations: symbol list cached 1h, polite rate-limiting, configurable workers

Notes:
 - This is heuristic SMC detection (not LuxAlgo).
 - Set TELEGRAM_TOKEN and CHAT_ID env vars in Render.
 - Render provides RENDER_EXTERNAL_URL env var for webhook URL.
"""

import os
import time
import json
import logging
import traceback
import requests
import io
from datetime import datetime, timezone, timedelta
from threading import Thread, Lock
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import numpy as np
import mplfinance as mpf
import matplotlib.pyplot as plt

from flask import Flask, request, jsonify

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "5000"))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")  # Render-provided public URL

# MEXC API base
MEXC_REST_BASE = "https://api.mexc.com"
TF = "2h"
KLIMIT = 500
SCAN_WORKERS = int(os.getenv("SCAN_WORKERS", "8"))

# Heuristic params
ORDER_BLOCK_LOOKBACK = int(os.getenv("ORDER_BLOCK_LOOKBACK", "24"))
MIN_BODY_THRESHOLD = float(os.getenv("MIN_BODY_THRESHOLD", "0.002"))
FVG_MIN_GAP_PCT = float(os.getenv("FVG_MIN_GAP_PCT", "0.001"))
ZONE_PADDING_PCT = float(os.getenv("ZONE_PADDING_PCT", "0.002"))
HIT_COOLDOWN_MINUTES = int(os.getenv("HIT_COOLDOWN_MINUTES", "10"))

# State file path in /data for Render persistence
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "smc_webhook_state.json")

# Symbol cache file
SYMBOL_CACHE_FILE = os.path.join(DATA_DIR, "symbol_cache.json")
SYMBOL_CACHE_TTL = 3600  # seconds

# Rate limiting (polite)
MIN_REQUEST_INTERVAL = float(os.getenv("MIN_REQUEST_INTERVAL", "0.06"))  # ~16 rps
_last_request_time = 0.0
_rate_lock = Lock()

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("smc-webhook-render")

# ---------------- STATE HELPERS ----------------
def load_state(default):
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.exception("load_state error: %s", e)
    return default

def save_state(state):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.exception("save_state error: %s", e)

state = load_state({"zones": {}, "last_scan": None, "events": []})

# ---------------- HTTP SESSION + RATE LIMIT ----------------
session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_maxsize=50, max_retries=2)
session.mount("https://", adapter)
session.mount("http://", adapter)

def rate_limited_get(url, params=None, timeout=12):
    global _last_request_time
    with _rate_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        try:
            r = session.get(url, params=params, timeout=timeout)
        finally:
            _last_request_time = time.time()
    return r

# ---------------- TELEGRAM HELPERS ----------------
def escape_md_v2(text: str) -> str:
    esc = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        esc = esc.replace(ch, "\\"+ch)
    return esc

def send_telegram(text: str, photo_bytes: bytes = None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("Telegram not configured, skipping message: %s", text[:140])
        return
    try:
        if photo_bytes:
            files = {'photo': ('chart.png', photo_bytes, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            r = session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            r = session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
        if r.status_code != 200:
            logger.warning("Telegram API returned %s: %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

def set_telegram_webhook():
    """Attempt to set webhook automatically using RENDER_EXTERNAL_URL."""
    if not TELEGRAM_TOKEN:
        logger.info("TELEGRAM_TOKEN not set; skipping webhook registration.")
        return
    if not RENDER_EXTERNAL_URL:
        logger.info("RENDER_EXTERNAL_URL not set; skipping webhook registration.")
        return
    webhook_url = f"{RENDER_EXTERNAL_URL}/telegram_webhook/{TELEGRAM_TOKEN}"
    try:
        logger.info("Setting Telegram webhook to %s", webhook_url)
        r = session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook", params={"url": webhook_url}, timeout=15)
        data = r.json() if r is not None else {}
        logger.info("setWebhook response: %s", data)
        send_telegram(f"Webhook set to {webhook_url}")
    except Exception as e:
        logger.exception("Failed to set webhook: %s", e)

# ---------------- MEXC HELPERS & SYMBOL CACHE ----------------
def get_spot_symbols_cached(limit=1000):
    """Return list of USDT pairs. Cache result to SYMBOL_CACHE_FILE for SYMBOL_CACHE_TTL seconds."""
    try:
        if os.path.exists(SYMBOL_CACHE_FILE):
            with open(SYMBOL_CACHE_FILE, "r") as f:
                cache = json.load(f)
            ts = cache.get("fetched_at")
            if ts and time.time() - ts < SYMBOL_CACHE_TTL:
                syms = cache.get("symbols", [])
                logger.info("Loaded %d symbols from cache", len(syms))
                return syms[:limit]
    except Exception:
        logger.exception("symbol cache read error")

    try:
        r = rate_limited_get(MEXC_REST_BASE + "/api/v3/ticker/24hr", timeout=15)
        data = r.json()
        syms = [d['symbol'] for d in data if d.get('symbol','').endswith("USDT")]
        # persist
        try:
            with open(SYMBOL_CACHE_FILE, "w") as f:
                json.dump({"fetched_at": time.time(), "symbols": syms}, f)
        except Exception:
            logger.exception("symbol cache write error")
        logger.info("Fetched %d symbols from MEXC", len(syms))
        return syms[:limit]
    except Exception as e:
        logger.exception("get_spot_symbols_cached error: %s", e)
        # fallback to a small list
        return ["BTCUSDT","ETHUSDT","USDTUSDT"][:limit]

def fetch_klines(symbol: str, interval: str, limit: int = 500):
    """Fetch klines via rate_limited_get and return pandas DataFrame or None"""
    try:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = rate_limited_get(MEXC_REST_BASE + "/api/v3/klines", params=params, timeout=15)
        arr = r.json()
        if not isinstance(arr, list):
            return None
        df = pd.DataFrame(arr, columns=["open_time","open","high","low","close","volume","close_time","qav","trades","taker_base","taker_quote","ignore"])
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df.set_index("open_time", inplace=True)
        return df
    except Exception as e:
        logger.debug("fetch_klines error %s %s: %s", symbol, interval, e)
        return None

# ---------------- SMC HEURISTICS ----------------
def detect_order_blocks(df: pd.DataFrame, lookback=ORDER_BLOCK_LOOKBACK):
    zones = []
    n = len(df)
    start = max(2, n - lookback - 3)
    for i in range(start, n-1):
        c = df.iloc[i]; p = df.iloc[i-1]
        body = abs(c['close'] - c['open'])
        body_pct = (body / c['close']) if c['close'] else 0
        if body_pct < MIN_BODY_THRESHOLD:
            continue
        if c['close'] > c['open'] and p['close'] < p['open'] and c['open'] < p['close']:
            low = min(c['open'], c['close'], p['low']); high = max(c['open'], c['close'], p['high'])
            zones.append({'type':'orderblock','side':'bull','zone':[low, high],'index':i})
        if c['close'] < c['open'] and p['close'] > p['open'] and c['open'] > p['close']:
            low = min(c['open'], c['close'], p['low']); high = max(c['open'], c['close'], p['high'])
            zones.append({'type':'orderblock','side':'bear','zone':[low, high],'index':i})
    return zones

def detect_fvg(df: pd.DataFrame, lookback=ORDER_BLOCK_LOOKBACK):
    zones = []
    n = len(df)
    start = max(0, n - lookback - 4)
    for i in range(start, n-2):
        A = df.iloc[i]; B = df.iloc[i+1]; C = df.iloc[i+2]
        if B['close'] < B['open'] and A['high'] < C['low']:
            gap_pct = (C['low'] - A['high']) / (A['high'] if A['high'] else 1)
            if gap_pct >= FVG_MIN_GAP_PCT:
                zones.append({'type':'fvg','side':'bull','zone':[A['high'], C['low']],'index':i+1})
        if B['close'] > B['open'] and A['low'] > C['high']:
            gap_pct = (A['low'] - C['high']) / (C['high'] if C['high'] else 1)
            if gap_pct >= FVG_MIN_GAP_PCT:
                zones.append({'type':'fvg','side':'bear','zone':[C['high'], A['low']],'index':i+1})
    return zones

def expand_zone(zone):
    low, high = zone
    pad = (high - low) * ZONE_PADDING_PCT
    return [low - pad, high + pad]

# ---------------- CHARTING ----------------
def build_chart_bytes(df: pd.DataFrame, symbol: str, interval: str, zone=None):
    try:
        fig, axes = mpf.plot(df.tail(200), type='candle', style='yahoo', returnfig=True)
        ax = axes[0] if isinstance(axes, list) else axes
        if zone:
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

# ---------------- FIND ZONES FOR SYMBOL ----------------
def find_zones_for_symbol(symbol: str):
    try:
        df = fetch_klines(symbol, TF, limit=KLIMIT)
        if df is None or len(df) < 60:
            return []
        ob = detect_order_blocks(df)
        fvg = detect_fvg(df)
        zones = ob + fvg
        results = []
        for z in zones:
            ez = expand_zone(z['zone'])
            zid = f"{symbol}|{TF}|{z['type']}|{z['index']}"
            results.append({
                "id": zid, "symbol": symbol, "tf": TF, "type": z['type'], "side": z.get('side'),
                "zone": ez, "detected_at": datetime.now(timezone.utc).isoformat(), "hit_at": None
            })
        return results
    except Exception as e:
        logger.exception("find_zones_for_symbol error %s: %s", symbol, e)
        return []

# ---------------- SCAN (on-demand) ----------------
def scan_all_pairs_and_store():
    logger.info("Starting full scan (on-demand) for all USDT pairs on TF=%s", TF)
    symbols = get_spot_symbols_cached(limit=1000)
    new = 0
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(find_zones_for_symbol, s): s for s in symbols}
        for fut, s in futures.items():
            try:
                zs = fut.result(timeout=90)
                if not zs:
                    continue
                for z in zs:
                    symzones = state.setdefault("zones", {}).setdefault(z['symbol'], [])
                    if any(x['id'] == z['id'] for x in symzones):
                        continue
                    symzones.append(z)
                    new += 1
                    # notify detection
                    txt = (f"ð Zone detected\nSymbol: {z['symbol']}\nTF: {z['tf']}\nType: {z['type']}\n"
                           f"Zone: {z['zone'][0]:.6f} - {z['zone'][1]:.6f}\nDetected: {z['detected_at']}")
                    try:
                        df_chart = fetch_klines(z['symbol'], z['tf'], limit=200)
                        img = build_chart_bytes(df_chart, z['symbol'], z['tf'], zone=z['zone'])
                    except Exception:
                        img = None
                    send_telegram(txt, photo_bytes=img)
            except Exception as e:
                logger.exception("scan worker error for %s: %s", s, e)
    state['last_scan'] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    logger.info("Scan finished, new zones=%d", new)
    return new

# ---------------- PAIR ANALYSIS ----------------
def analyze_pair_and_send(symbol: str):
    try:
        zs = find_zones_for_symbol(symbol)
        if not zs:
            send_telegram(f"ð No zones found for {symbol} on {TF}.")
            return
        for z in zs:
            txt = (f"ð Zone detected\nSymbol: {z['symbol']}\nTF: {z['tf']}\nType: {z['type']}\n"
                   f"Zone: {z['zone'][0]:.6f} - {z['zone'][1]:.6f}\nDetected: {z['detected_at']}")
            df_chart = fetch_klines(z['symbol'], z['tf'], limit=200)
            img = build_chart_bytes(df_chart, z['symbol'], z['tf'], zone=z['zone']) if df_chart is not None else None
            send_telegram(txt, photo_bytes=img)
            symzones = state.setdefault("zones", {}).setdefault(z['symbol'], [])
            if not any(x['id']==z['id'] for x in symzones):
                symzones.append(z)
        save_state(state)
    except Exception as e:
        logger.exception("analyze_pair_and_send error: %s", e)
        send_telegram(f"Error analyzing {symbol}: {e}")

# ---------------- MONITOR HITS ----------------
def monitor_zone_hits():
    cooldown_s = HIT_COOLDOWN_MINUTES * 60
    while True:
        try:
            now = datetime.now(timezone.utc)
            for symbol, zones in list(state.get("zones", {}).items()):
                df_latest = fetch_klines(symbol, "1m", limit=2)
                if df_latest is None or df_latest.empty:
                    continue
                last_price = float(df_latest['close'].iloc[-1])
                for z in zones:
                    if z.get("hit_at"):
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
                        state.setdefault("events", []).append({"symbol":symbol, "zone_id":z['id'], "hit_at":now.isoformat(), "price": last_price})
                        save_state(state)
        except Exception as e:
            logger.exception("monitor_zone_hits error: %s", e)
        time.sleep(20)

# ---------------- FLASK + WEBHOOK ----------------
app = Flask(__name__)

@app.route("/telegram_webhook/<token>", methods=["POST"])
def telegram_webhook(token):
    if token != TELEGRAM_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    update = request.get_json(force=True)
    try:
        msg = update.get("message") or update.get("edited_message") or {}
        text = msg.get("text", "").strip()
        if not text:
            return jsonify({"ok": True})
        parts = text.split()
        cmd = parts[0].lower()
        if cmd == "/scan":
            Thread(target=scan_all_pairs_and_store, daemon=True).start()
            send_telegram("â¡ Full scan started. I will post detections as I find them.")
            return jsonify({"ok": True})
        elif cmd == "/pair":
            if len(parts) < 2:
                send_telegram("Usage: /pair SYMBOL (e.g. /pair BTCUSDT)")
                return jsonify({"ok": True})
            sym = parts[1].upper()
            Thread(target=analyze_pair_and_send, args=(sym,), daemon=True).start()
            send_telegram(f"ð Analyzing {sym} on {TF}...")
            return jsonify({"ok": True})
        elif cmd == "/status":
            zones_total = sum(len(v) for v in state.get("zones", {}).values())
            last = state.get("last_scan")
            send_telegram(f"Status: zones={zones_total}, last_scan={last}")
            return jsonify({"ok": True})
        elif cmd == "/help":
            send_telegram("/scan - analyze all pairs\n/pair SYMBOL - analyze single pair\n/status - bot status")
            return jsonify({"ok": True})
        else:
            return jsonify({"ok": True})
    except Exception as e:
        logger.exception("telegram_webhook handler error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/scan", methods=["GET","POST"])
def http_scan():
    Thread(target=scan_all_pairs_and_store, daemon=True).start()
    return jsonify({"ok": True, "message": "scan started"})

@app.route("/status", methods=["GET"])
def http_status():
    return jsonify({"zones": state.get("zones", {}), "events": state.get("events", [])})

# ---------------- STARTUP ----------------
def ensure_symbol_cache():
    try:
        get_spot_symbols_cached(limit=200)
    except Exception:
        pass

def get_spot_symbols_cached(limit=1000):
    # wrapper to call the module-level function defined earlier (avoid forward ref)
    return globals()['get_spot_symbols_cached'](limit=limit)

if __name__ == "__main__":
    logger.info("Starting SMC webhook Render-ready bot (TF=%s)", TF)
    # Ensure symbol cache exists
    try:
        # If webhook can be set, attempt it (Render provides RENDER_EXTERNAL_URL)
        set_telegram_webhook()
    except Exception:
        logger.exception("set_telegram_webhook failed at startup")
    # start monitor
    Thread(target=monitor_zone_hits, daemon=True).start()
    # for local dev, run flask directly; on Render use gunicorn with `app` as entrypoint
    app.run(host="0.0.0.0", port=PORT)