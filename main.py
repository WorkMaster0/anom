#!/usr/bin/env python3
"""
smc_webhook_render_bot_v2.py

Render-ready Telegram webhook bot (SMC heuristics) - v2

Features:
 - On /scan analyzes ALL USDT spot pairs on MEXC for SMC-like zones (order blocks & FVG) on 2h TF
 - Caches symbol list (1h), polite rate-limited requests
 - Stores state in ./data/smc_state.json (Render-friendly)
 - Auto-registers Telegram webhook using RENDER_EXTERNAL_URL (if provided)
 - Commands: /scan, /pair SYMBOL, /status, /help, /watch SYMBOL, /unwatch SYMBOL, /listzones, /prebreak ON|OFF|PCT
 - Sends chart image when zone detected and when price enters zone
 - Optionally notifies "pre-break" when price approaches zone (configurable)
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
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

MEXC_REST_BASE = "https://api.mexc.com"
TF = "2h"
KLIMIT = 500
SCAN_WORKERS = int(os.getenv("SCAN_WORKERS", "8"))

# heuristic params
ORDER_BLOCK_LOOKBACK = int(os.getenv("ORDER_BLOCK_LOOKBACK", "24"))
MIN_BODY_THRESHOLD = float(os.getenv("MIN_BODY_THRESHOLD", "0.2"))
FVG_MIN_GAP_PCT = float(os.getenv("FVG_MIN_GAP_PCT", "0.1"))
ZONE_PADDING_PCT = float(os.getenv("ZONE_PADDING_PCT", "0.2"))

# prebreak/approach logic
PREBREAK_ENABLED = True
PREBREAK_PCT = float(os.getenv("PREBREAK_PCT", "0.01"))  # 1% by default

# timing / caching
SYMBOL_CACHE_TTL = 3600
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "smc_state.json")
SYMBOL_CACHE_FILE = os.path.join(DATA_DIR, "symbols_cache.json")

# rate limiting
MIN_REQUEST_INTERVAL = float(os.getenv("MIN_REQUEST_INTERVAL", "0.06"))
_last_request_time = 0.0
_rate_lock = Lock()

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("smc-webhook-v2")

# ---------------- STATE ----------------
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.exception("load_state error: %s", e)
    # default
    return {"zones": {}, "last_scan": None, "events": [], "watchlist": [], "prebreak": {"enabled": PREBREAK_ENABLED, "pct": PREBREAK_PCT}}

def save_state(st):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(st, f, indent=2, default=str)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.exception("save_state error: %s", e)

state = load_state()

# ---------------- HTTP session + rate-limited GET ----------------
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
        except Exception as e:
            _last_request_time = time.time()
            raise
        _last_request_time = time.time()
    return r

# ---------------- Telegram helpers ----------------
def escape_md_v2(text: str) -> str:
    esc = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        esc = esc.replace(ch, "\\"+ch)
    return esc

def send_telegram(text: str, photo_bytes: bytes = None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.debug("telegram disabled, would send: %s", text[:200])
        return
    try:
        if photo_bytes is not None:
            files = {'photo': ('chart.png', photo_bytes, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            r = session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            r = session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
        if r.status_code != 200:
            logger.warning("Telegram API responded %s: %s", r.status_code, r.text)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

def set_webhook_if_render():
    if not TELEGRAM_TOKEN:
        logger.info("No TELEGRAM_TOKEN, skipping webhook setup")
        return
    if not RENDER_EXTERNAL_URL:
        logger.info("No RENDER_EXTERNAL_URL, skipping webhook setup")
        return
    webhook_url = f"{RENDER_EXTERNAL_URL}/telegram_webhook/{TELEGRAM_TOKEN}"
    try:
        logger.info("Registering webhook: %s", webhook_url)
        r = session.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook", params={"url": webhook_url}, timeout=15)
        j = r.json() if r is not None else {}
        logger.info("setWebhook response: %s", j)
        send_telegram(f"Webhook set to {webhook_url}")
    except Exception as e:
        logger.exception("set_webhook_if_render error: %s", e)

# ---------------- MEXC helpers and symbol cache ----------------
def get_spot_symbols_cached(limit=1000):
    # load cache if recent
    try:
        if os.path.exists(SYMBOL_CACHE_FILE):
            with open(SYMBOL_CACHE_FILE, "r") as f:
                cache = json.load(f)
            if time.time() - cache.get("fetched_at", 0) < SYMBOL_CACHE_TTL:
                syms = cache.get("symbols", [])
                logger.info("Loaded %d symbols from cache", len(syms))
                return syms[:limit]
    except Exception:
        logger.exception("symbol cache read error")

    try:
        r = rate_limited_get(MEXC_REST_BASE + "/api/v3/ticker/24hr", timeout=15)
        data = r.json()
        syms = [d['symbol'] for d in data if d.get('symbol','').endswith("USDT")]
        # save cache
        try:
            with open(SYMBOL_CACHE_FILE, "w") as f:
                json.dump({"fetched_at": time.time(), "symbols": syms}, f)
        except Exception:
            logger.exception("symbol cache write error")
        logger.info("Fetched %d symbols from MEXC", len(syms))
        return syms[:limit]
    except Exception as e:
        logger.exception("get_spot_symbols_cached error: %s", e)
        # fallback to existing cache if present
        try:
            if os.path.exists(SYMBOL_CACHE_FILE):
                with open(SYMBOL_CACHE_FILE, "r") as f:
                    cache = json.load(f)
                    return cache.get("symbols", [])[:limit]
        except Exception:
            logger.exception("symbol cache fallback error")
        # fallback minimal
        return ["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT"]

def fetch_klines(symbol: str, interval: str, limit: int = 500):
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

# ---------------- SMC heuristics ----------------
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

# ---------------- CHARTS ----------------
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
    logger.info("Starting full scan for all USDT pairs TF=%s", TF)
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
                    txt = (f"🔎 Zone detected\nSymbol: {z['symbol']}\nTF: {z['tf']}\nType: {z['type']}\n"
                           f"Zone: {z['zone'][0]:.6f} - {z['zone'][1]:.6f}\nDetected: {z['detected_at']}")
                    try:
                        df_chart = fetch_klines(z['symbol'], z['tf'], limit=200)
                        img = build_chart_bytes(df_chart, z['symbol'], z['tf'], zone=z['zone']) if df_chart is not None else None
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
            send_telegram(f"🔍 No zones found for {symbol} on {TF}.")
            return
        for z in zs:
            txt = (f"🔎 Zone detected\nSymbol: {z['symbol']}\nTF: {z['tf']}\nType: {z['type']}\n"
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

# ---------------- WATCHLIST ----------------
def add_watch(symbol):
    sym = symbol.upper()
    if sym not in state.get("watchlist", []):
        state.setdefault("watchlist", []).append(sym)
        save_state(state)
        return True
    return False

def remove_watch(symbol):
    sym = symbol.upper()
    if sym in state.get("watchlist", []):
        state["watchlist"].remove(sym)
        save_state(state)
        return True
    return False

# ---------------- MONITOR ZONE HITS & PREBREAK ----------------
def monitor_zone_hits():
    while True:
        try:
            now = datetime.now(timezone.utc)
            prebreak_cfg = state.get("prebreak", {"enabled": PREBREAK_ENABLED, "pct": PREBREAK_PCT})
            for symbol, zones in list(state.get("zones", {}).items()):
                df_latest = fetch_klines(symbol, "1m", limit=2)
                if df_latest is None or df_latest.empty:
                    continue
                last_price = float(df_latest['close'].iloc[-1])
                for z in zones:
                    if z.get("hit_at"):
                        continue
                    low, high = z['zone']
                    # HIT
                    if low <= last_price <= high:
                        detected = pd.to_datetime(z['detected_at'])
                        elapsed = now - (detected.to_pydatetime() if hasattr(detected, 'to_pydatetime') else pd.to_datetime(z['detected_at']))
                        txt = (f"✅ Zone HIT\nSymbol: {symbol}\nTF: {z['tf']}\nType: {z['type']}\n"
                               f"Price: {last_price:.6f}\nZone: {low:.6f} - {high:.6f}\nDetected: {z['detected_at']}\nTime to hit: {elapsed}")
                        df_chart = fetch_klines(symbol, z['tf'], limit=200)
                        img = build_chart_bytes(df_chart, symbol, z['tf'], zone=z['zone']) if df_chart is not None else None
                        send_telegram(txt, photo_bytes=img)
                        z['hit_at'] = now.isoformat()
                        state.setdefault("events", []).append({"symbol":symbol, "zone_id":z['id'], "hit_at":now.isoformat(), "price": last_price})
                        save_state(state)
                    # PREBREAK (approach) - only if enabled
                    elif prebreak_cfg.get("enabled", False):
                        pct = prebreak_cfg.get("pct", PREBREAK_PCT)
                        if last_price > high and (last_price - high)/high <= pct:
                            key = f"prebreak:{z['id']}"
                            if not state.get("events_marker", {}).get(key):
                                txt = (f"⚠️ Approaching zone (from above)\nSymbol: {symbol}\nTF: {z['tf']}\nType: {z['type']}\n"
                                       f"Price: {last_price:.6f}\nZone top: {high:.6f}\nDistance: {(last_price-high)/high:.4%}")
                                df_chart = fetch_klines(symbol, z['tf'], limit=200)
                                img = build_chart_bytes(df_chart, symbol, z['tf'], zone=z['zone']) if df_chart is not None else None
                                send_telegram(txt, photo_bytes=img)
                                state.setdefault("events_marker", {})[key] = now.isoformat()
                                save_state(state)
                        elif last_price < low and (low - last_price)/low <= pct:
                            key = f"prebreak:{z['id']}"
                            if not state.get("events_marker", {}).get(key):
                                txt = (f"⚠️ Approaching zone (from below)\nSymbol: {symbol}\nTF: {z['tf']}\nType: {z['type']}\n"
                                       f"Price: {last_price:.6f}\nZone bottom: {low:.6f}\nDistance: {(low-last_price)/low:.4%}")
                                df_chart = fetch_klines(symbol, z['tf'], limit=200)
                                img = build_chart_bytes(df_chart, symbol, z['tf'], zone=z['zone']) if df_chart is not None else None
                                send_telegram(txt, photo_bytes=img)
                                state.setdefault("events_marker", {})[key] = now.isoformat()
                                save_state(state)
            # optionally report watchlist updates
            for sym in state.get("watchlist", []):
                try:
                    df_latest = fetch_klines(sym, "1m", limit=2)
                    if df_latest is None or df_latest.empty:
                        continue
                    lp = float(df_latest['close'].iloc[-1])
                    send_telegram(f"🔎 Watchlist update: {sym} price {lp:.6f}")
                except Exception:
                    pass
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
            send_telegram("⚡ Full scan started (this may take several minutes).")
            return jsonify({"ok": True})
        elif cmd == "/pair":
            if len(parts) < 2:
                send_telegram("Usage: /pair SYMBOL  (e.g. /pair BTCUSDT)")
                return jsonify({"ok": True})
            sym = parts[1].upper()
            Thread(target=analyze_pair_and_send, args=(sym,), daemon=True).start()
            send_telegram(f"🔍 Analyzing {sym} on {TF}...")
            return jsonify({"ok": True})
        elif cmd == "/status":
            zones_total = sum(len(v) for v in state.get("zones", {}).values())
            last = state.get("last_scan")
            send_telegram(f"Status: zones={zones_total}, last_scan={last}")
            return jsonify({"ok": True})
        elif cmd == "/help":
            help_text = ("/scan - analyze all pairs\n/pair SYMBOL - analyze single pair\n"
                         "/watch SYMBOL - add to watchlist\n/unwatch SYMBOL - remove\n/listzones SYMBOL - list stored zones\n"
                         "/prebreak ON|OFF|PCT - toggle or set prebreak pct (e.g. /prebreak 0.005)\n/status - bot status")
            send_telegram(help_text)
            return jsonify({"ok": True})
        elif cmd == "/watch":
            if len(parts) < 2:
                send_telegram("Usage: /watch SYMBOL")
                return jsonify({"ok": True})
            sym = parts[1].upper()
            added = add_watch(sym)
            send_telegram(f"{sym} added to watchlist." if added else f"{sym} already in watchlist.")
            return jsonify({"ok": True})
        elif cmd == "/unwatch":
            if len(parts) < 2:
                send_telegram("Usage: /unwatch SYMBOL")
                return jsonify({"ok": True})
            sym = parts[1].upper()
            removed = remove_watch(sym)
            send_telegram(f"{sym} removed from watchlist." if removed else f"{sym} not in watchlist.")
            return jsonify({"ok": True})
        elif cmd == "/listzones":
            if len(parts) < 2:
                send_telegram("Usage: /listzones SYMBOL")
                return jsonify({"ok": True})
            sym = parts[1].upper()
            z = state.get("zones", {}).get(sym, [])
            if not z:
                send_telegram(f"No zones stored for {sym}")
            else:
                lines = [f"{item['id']} {item['type']} {item['zone'][0]:.6f}-{item['zone'][1]:.6f} detected {item['detected_at']}" for item in z]
                for i in range(0, len(lines), 10):
                    send_telegram("Zones:\n" + "\n".join(lines[i:i+10]))
            return jsonify({"ok": True})
        elif cmd == "/prebreak":
            if len(parts) < 2:
                cfg = state.get("prebreak", {"enabled": PREBREAK_ENABLED, "pct": PREBREAK_PCT})
                send_telegram(f"Prebreak config: enabled={cfg['enabled']} pct={cfg['pct']}")
                return jsonify({"ok": True})
            arg = parts[1].upper()
            if arg in ("ON","OFF"):
                state.setdefault("prebreak", {})["enabled"] = (arg=="ON")
                save_state(state)
                send_telegram(f"Prebreak notifications {'enabled' if arg=='ON' else 'disabled'}.")
                return jsonify({"ok": True})
            try:
                pct = float(parts[1])
                state.setdefault("prebreak", {})["pct"] = pct
                save_state(state)
                send_telegram(f"Prebreak pct set to {pct}")
                return jsonify({"ok": True})
            except Exception:
                send_telegram("Invalid prebreak argument. Use ON, OFF, or a decimal like 0.01")
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
    return jsonify({"zones": state.get("zones", {}), "events": state.get("events", []), "watchlist": state.get("watchlist", [])})

# ---------------- STARTUP ----------------
if __name__ == "__main__":
    logger.info("Starting SMC webhook Render-ready bot v2 (TF=%s)", TF)
    try:
        set_webhook_if_render()
    except Exception:
        logger.exception("set_webhook_if_render failed")
    Thread(target=monitor_zone_hits, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)