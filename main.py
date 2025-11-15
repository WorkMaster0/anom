#!/usr/bin/env python3
# app.py
"""
Telegram webhook scanner for three strategies:
  1) Funding-rate harvesting (monitor funding rates)
  2) Triangular arbitrage (single-exchange triangles)
  3) Volatility/mean-reversion signals (cross-exchange spread spikes)

SAFE mode: no trading, only sends Telegram signals.
No API keys required (public data only). Uses ccxt for public tickers/orderbooks when possible.

Usage:
  - Set TELEGRAM_TOKEN and CHAT_ID as environment variables (or edit below).
  - Run: python3 app.py
  - Register Telegram webhook (optional) or use polling (see notes).
    If using webhook, set WEBHOOK_URL env to your public HTTPS URL (https://...) and bot will call /webhook.
  - In Telegram, send commands to the bot:
      /start
      /scan_funding start|stop
      /scan_tri start|stop
      /scan_vol start|stop
      /status
      /help
"""

import os
import time
import threading
import logging
import json
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

import requests
import ccxt
from flask import Flask, request, jsonify

# ----------------------------- CONFIG ----------------------------------------
# Fill these or set environment variables (recommended)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()   # required for sending messages
CHAT_ID = os.getenv("CHAT_ID", "").strip()                 # your chat id (or channel id)
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()         # optional; if set, app will attempt to set webhook

# Exchanges (public access)
EXCHANGE_A_ID = os.getenv("EXCHANGE_A", "kucoin")   # for funding & futures (kucoin preferred)
EXCHANGE_B_ID = os.getenv("EXCHANGE_B", "lbank")    # spot reference exchange (lbank default)

# Runtime params (tweak as needed)
FUNDING_SCAN_INTERVAL = float(os.getenv("FUNDING_SCAN_INTERVAL", "60"))   # seconds
TRI_SCAN_INTERVAL = float(os.getenv("TRI_SCAN_INTERVAL", "5"))            # seconds
VOL_SCAN_INTERVAL = float(os.getenv("VOL_SCAN_INTERVAL", "2"))            # seconds

FUNDING_THRESHOLD_PCT = float(os.getenv("FUNDING_THRESHOLD_PCT", "0.08"))  # percent (0.08% => 0.0008)
TRI_ARB_THRESHOLD_PCT = float(os.getenv("TRI_ARB_THRESHOLD_PCT", "0.2"))   # percent profit threshold
VOL_SPIKE_MULTIPLIER = float(os.getenv("VOL_SPIKE_MULTIPLIER", "4.0"))    # multiplier over rolling std

SYMBOLS = os.getenv("SYMBOLS", "BTC,ETH,SOL").split(",")
SYMBOLS = [s.strip().upper() for s in SYMBOLS if s.strip()]

LOG_LIMIT = 1000

# ----------------------------- LOGGING ---------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("arb-signals")

# in-memory logs (for simple status endpoint)
_logs: List[str] = []
_log_lock = threading.Lock()

def app_log(msg: str):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    logger.info(msg)
    with _log_lock:
        _logs.append(line)
        if len(_logs) > LOG_LIMIT:
            _logs.pop(0)

def get_logs() -> List[str]:
    with _log_lock:
        return list(_logs)

# ----------------------------- TELEGRAM HELPERS -------------------------------
if not TELEGRAM_TOKEN:
    app_log("WARNING: TELEGRAM_TOKEN not set. Bot cannot send messages until set.")

TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def tg_send(text: str):
    """Send message to configured CHAT_ID. Silently fail if token or chat missing."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        app_log("tg_send skipped (missing TELEGRAM_TOKEN or CHAT_ID)")
        return None
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(TELEGRAM_API + "/sendMessage", json=payload, timeout=8)
        if r.status_code != 200:
            app_log(f"tg_send failed: {r.status_code} {r.text}")
            return None
        return r.json()
    except Exception as e:
        app_log(f"tg_send exception: {e}")
        return None

# ----------------------------- EXCHANGE CLIENTS -------------------------------
# We'll create public ccxt clients for the two exchanges.
_ccxt_clients_lock = threading.Lock()
_ccxt_clients: Dict[str, Optional[ccxt.Exchange]] = {}

def get_ccxt_client(exid: str):
    with _ccxt_clients_lock:
        if exid in _ccxt_clients:
            return _ccxt_clients[exid]
        try:
            client = getattr(ccxt, exid)({"enableRateLimit": True})
            # some exchanges require loading markets to use certain methods
            try:
                client.load_markets()
            except Exception:
                pass
            _ccxt_clients[exid] = client
            app_log(f"Initialized {exid} client")
            return client
        except Exception as e:
            app_log(f"Failed to init client {exid}: {e}")
            _ccxt_clients[exid] = None
            return None

# ----------------------------- SHARED STATE ----------------------------------
scanners_state = {
    "funding": {"running": False, "thread": None},
    "tri": {"running": False, "thread": None},
    "vol": {"running": False, "thread": None}
}

# store small history for volatility strategy
price_history: Dict[str, List[float]] = {}
history_lock = threading.Lock()

# utility: safe fetch ticker
def safe_ticker(exid: str, pair: str) -> Optional[Dict[str, Any]]:
    client = get_ccxt_client(exid)
    if not client:
        return None
    try:
        tk = client.fetch_ticker(pair)
        return tk
    except Exception:
        # don't spam logs; return None
        return None

# ----------------------------- STRATEGIES ------------------------------------
# 1) Funding-rate harvesting (signals only)
def funding_scanner_loop():
    """
    Periodically fetch funding (if available) from EXCHANGE_A (e.g., kucoin futures)
    and send signal when funding rate magnitude exceeds threshold.
    Implementation: try ccxt.fetch_funding_rate() for each symbol, else try exchange-specific endpoints.
    """
    exid = EXCHANGE_A_ID
    client = get_ccxt_client(exid)
    app_log("Funding scanner started (exchange=%s)" % exid)

    while scanners_state["funding"]["running"]:
        for sym in SYMBOLS:
            try:
                # Try common ccxt method: fetchFundingRate (symbol) or fetch_funding_rate
                funding = None
                if client:
                    try:
                        # Some ccxt builds implement fetch_funding_rate or fetchFundingRate
                        if hasattr(client, "fetch_funding_rate"):
                            fr = client.fetch_funding_rate(symbol=f"{sym}/USDT")
                            # structure varies: try common keys
                            if isinstance(fr, dict):
                                funding = fr.get("fundingRate") or fr.get("rate") or fr.get("funding_rate") or fr.get("funding")
                        elif hasattr(client, "fetchFundingRate"):
                            fr = client.fetchFundingRate(symbol=f"{sym}/USDT")
                            if isinstance(fr, dict):
                                funding = fr.get("fundingRate") or fr.get("rate")
                    except Exception:
                        funding = None

                # fallback: for kucoin futures, there's public endpoint but ccxt may not wrap; try REST
                if funding is None and exid.lower().startswith("kucoin"):
                    try:
                        # get contract list and funding via public futures API
                        # note: structure may change; best-effort
                        resp = requests.get("https://api-futures.kucoin.com/api/v1/contracts/active", timeout=5)
                        data = resp.json()
                        if isinstance(data, dict) and "data" in data:
                            for item in data["data"]:
                                # item may have "symbol" like "XBTUSDTM" or "BTCUSDTM" etc. Try match
                                # We'll match if item["symbol"].startswith(sym)
                                if "symbol" in item and item["symbol"].upper().startswith(sym.upper()):
                                    # try markPrice or fundingRate fields
                                    funding = item.get("fundingRate") or item.get("fundingRateRate") or item.get("fundingRateRate")
                                    if funding is not None:
                                        funding = float(funding)
                                        break
                    except Exception:
                        funding = None

                # if still None: skip
                if funding is None:
                    app_log(f"[funding] {sym}: funding not available (skipped)")
                    continue

                # funding may be expressed as decimal per funding period (e.g., 0.0002 = 0.02%)
                funding_pct = float(funding) * 100.0
                app_log(f"[funding] {sym}: funding={funding} ({funding_pct:.4f}%)")

                thr = FUNDING_THRESHOLD_PCT
                if abs(funding_pct) >= thr:
                    # positive funding means long pays short; negative means short pays long.
                    side = "longs pay shorts" if funding_pct > 0 else "shorts pay longs"
                    text = (
                        f"🔔 *FUNDING SIGNAL* — {sym}\n"
                        f"Funding rate: *{funding_pct:.4f}%* ({side})\n"
                        f"Exchange: {exid}\n"
                        f"Note: this is a signal to consider delta-neutral funding harvest (monitor/hedge)."
                    )
                    tg_send(text)
            except Exception as e:
                app_log(f"funding loop error for {sym}: {e}")
        time.sleep(FUNDING_SCAN_INTERVAL)
    app_log("Funding scanner stopped")

# 2) Triangular arbitrage scanner (single exchange)
def triangle_scan_once(exid: str, threshold_pct: float) -> List[Dict[str, Any]]:
    """
    Attempt to find simple triangular arbitrage opportunities inside exchange `exid`.
    Approach:
      - Use exchange.fetch_tickers() to get available symbols and prices.
      - Build quick map of quote/base pairs using USDT, BTC, ETH, etc.
      - Check triangles A->B->C->A for arbitrage > threshold_pct.
    This is a heuristic, best-effort scanner for signals only.
    """
    client = get_ccxt_client(exid)
    if not client:
        return []

    try:
        tickers = client.fetch_tickers()
    except Exception as e:
        app_log(f"tri: fetch_tickers failed for {exid}: {e}")
        return []

    # build price map: pair -> last
    prices = {}
    for pair, tk in tickers.items():
        try:
            last = tk.get("last") or tk.get("close")
            if last:
                prices[pair] = float(last)
        except Exception:
            continue

    # Build list of unique currencies from pairs that include USDT or common bases
    # We'll attempt triangles among SYMBOLS × common bridges (USDT,BTC,ETH)
    bridges = ["USDT", "BTC", "ETH"]
    results = []
    # simplify: for each base in SYMBOLS, check triangles base-USDT-BTC-base etc.
    for base in SYMBOLS:
        base = base.upper()
        # consider pairs: base/USDT, base/BTC, base/ETH and reverse
        combos = []
        for b in bridges:
            if b == base:
                continue
            pairs = [
                f"{base}/{b}",
                f"{b}/{base}"
            ]
            combos.extend(pairs)
        # generate triangles: base -> X -> Y -> base, where X,Y from bridges
        for x in bridges:
            for y in bridges:
                if x == y or x == base or y == base:
                    continue
                # try path: base/x, x/y, y/base
                p1 = f"{base}/{x}"
                p2 = f"{x}/{y}"
                p3 = f"{y}/{base}"
                # determine if we have quotes (we might have inverted pairs)
                def get_price_for(pair):
                    if pair in prices:
                        return prices[pair], False  # direct
                    # try inverse
                    if "/" in pair:
                        a,b = pair.split("/")
                        inv = f"{b}/{a}"
                        if inv in prices and prices[inv] != 0:
                            return 1.0/prices[inv], True  # inverted
                    return None, None

                v1, inv1 = get_price_for(p1)
                v2, inv2 = get_price_for(p2)
                v3, inv3 = get_price_for(p3)
                if v1 is None or v2 is None or v3 is None:
                    continue
                # simulate starting with 1 unit of base -> convert through triangles
                try:
                    after = 1.0
                    after = after * v1  # base->x
                    after = after * v2  # x->y
                    after = after * v3  # y->base
                    profit_pct = (after - 1.0) * 100.0
                    if profit_pct >= threshold_pct:
                        results.append({
                            "exchange": exid,
                            "base": base,
                            "path": [p1, p2, p3],
                            "profit_pct": profit_pct,
                            "rates": [v1, v2, v3]
                        })
                except Exception:
                    continue
    return results

def tri_scanner_loop():
    exid = EXCHANGE_A_ID  # choose primary exchange for tri-arb (KuCoin typically)
    app_log("Triangle scanner started (exchange=%s)" % exid)
    while scanners_state["tri"]["running"]:
        try:
            res = triangle_scan_once(exid, TRI_ARB_THRESHOLD_PCT)
            if res:
                for r in res:
                    text = (
                        f"🔺 *TRIANGLE ARB SIGNAL* on {r['exchange']}\n"
                        f"Base: {r['base']}\n"
                        f"Path: {' -> '.join(r['path'])}\n"
                        f"Estimated profit: *{r['profit_pct']:.3f}%*\n"
                        f"_Note: fees/slippage not accounted. Signal only._"
                    )
                    tg_send(text)
                    app_log(f"tri signal: {r['base']} profit={r['profit_pct']:.4f}%")
            else:
                app_log("tri scanner: no opportunities found")
        except Exception as e:
            app_log(f"tri scanner loop error: {e}")
        time.sleep(TRI_SCAN_INTERVAL)
    app_log("Triangle scanner stopped")

# 3) Volatility / mean-reversion spike scanner (cross-exchange)
def vol_scanner_loop():
    app_log("Volatility scanner started (cross-exchange)")
    ex1 = EXCHANGE_A_ID
    ex2 = EXCHANGE_B_ID
    while scanners_state["vol"]["running"]:
        for s in SYMBOLS:
            pair = f"{s}/USDT"
            try:
                tk1 = safe_ticker(ex1, pair)
                tk2 = safe_ticker(ex2, pair)
                if not tk1 or not tk2:
                    continue
                p1 = tk1.get("last") or tk1.get("close")
                p2 = tk2.get("last") or tk2.get("close")
                if not is_price_valid(p1) or not is_price_valid(p2):
                    continue
                p1 = float(p1); p2 = float(p2)
                spread = p1 - p2
                # update history
                with history_lock:
                    hist = price_history.setdefault(s, [])
                    hist.append(spread)
                    if len(hist) > 200:
                        hist.pop(0)
                    # compute rolling std and mean on last N
                    window = min(len(hist), 60)
                    if window < 10:
                        continue
                    recent = hist[-window:]
                mean = sum(recent)/len(recent)
                # sample returns std
                # compute standard deviation of recent spreads
                var = sum((x-mean)**2 for x in recent)/len(recent)
                std = var**0.5
                # detect spike
                dev = abs(spread - mean)
                if std > 0 and dev >= (VOL_SPIKE_MULTIPLIER * std):
                    direction = "KUCOIN > LBank" if (p1 - p2) > 0 else "LBank > KuCoin"
                    text = (
                        f"⚡ *VOL SPIKE SIGNAL* {s}\n"
                        f"{ex1} price: {p1:.6f}\n{ex2} price: {p2:.6f}\n"
                        f"Spread: {spread:.6f} (mean={mean:.6f}, std={std:.6f})\n"
                        f"Deviation: {dev:.6f} = {dev/std:.2f}σ\n"
                        f"Direction: {direction}\n\n"
                        f"Consider opening a hedged mean-reversion trade."
                    )
                    tg_send(text)
                    app_log(f"vol signal {s}: dev={dev:.6f} std={std:.6f}")
                else:
                    app_log(f"vol check {s}: spread={spread:.6f} mean={mean:.6f} std={std:.6f}" if std>0 else f"vol check {s}: insufficient data")
            except Exception as e:
                app_log(f"vol loop error for {s}: {e}")
        time.sleep(VOL_SCAN_INTERVAL)
    app_log("Volatility scanner stopped")

# ----------------------------- START/STOP HELPERS ----------------------------
def start_scanner(name: str) -> str:
    if name not in scanners_state:
        return f"Unknown scanner {name}"
    if scanners_state[name]["running"]:
        return f"{name} scanner already running"
    scanners_state[name]["running"] = True
    if name == "funding":
        t = threading.Thread(target=funding_scanner_loop, daemon=True)
    elif name == "tri":
        t = threading.Thread(target=tri_scanner_loop, daemon=True)
    elif name == "vol":
        t = threading.Thread(target=vol_scanner_loop, daemon=True)
    else:
        return "invalid"
    scanners_state[name]["thread"] = t
    t.start()
    return f"Started {name} scanner"

def stop_scanner(name: str) -> str:
    if name not in scanners_state:
        return f"Unknown scanner {name}"
    if not scanners_state[name]["running"]:
        return f"{name} scanner not running"
    scanners_state[name]["running"] = False
    # thread will exit after loop checks running flag
    return f"Stopping {name} scanner"

def status_report() -> str:
    parts = []
    for k,v in scanners_state.items():
        parts.append(f"{k}: {'running' if v['running'] else 'stopped'}")
    parts.append("Symbols: " + ", ".join(SYMBOLS))
    parts.append(f"Funding thr: {FUNDING_THRESHOLD_PCT}%")
    parts.append(f"Tri thr: {TRI_ARB_THRESHOLD_PCT}%")
    parts.append(f"Vol multiplier: x{VOL_SPIKE_MULTIPLIER}")
    return "\n".join(parts)

# ----------------------------- FLASK / TELEGRAM WEBHOOK -----------------------
app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return jsonify({"ok": True, "status": "arb-signals running", "time": datetime.utcnow().isoformat()})

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Telegram webhook handler.
    Commands:
      /start
      /help
      /scan_funding start|stop
      /scan_tri start|stop
      /scan_vol start|stop
      /status
    """
    data = request.get_json(force=True)
    if not data:
        return jsonify({"ok": False}), 400
    # parse message
    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return jsonify({"ok": True})
    chat = msg.get("chat", {})
    cid = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not text:
        return jsonify({"ok": True})
    app_log(f"Webhook cmd from {cid}: {text[:200]}")
    parts = text.split()
    cmd = parts[0].lower()
    reply = "Unknown command. /help"
    try:
        if cmd == "/start":
            reply = ("Arb signals bot online.\nCommands:\n"
                     "/scan_funding start|stop\n/scan_tri start|stop\n/scan_vol start|stop\n/status\n/help")
        elif cmd == "/help":
            reply = ("Scanner commands:\n"
                     "/scan_funding start|stop — funding-rate harvesting signals\n"
                     "/scan_tri start|stop — triangular arbitrage scanner\n"
                     "/scan_vol start|stop — volatility spike scanner\n"
                     "/status — show status")
        elif cmd == "/scan_funding":
            if len(parts) >= 2 and parts[1].lower() == "start":
                reply = start_scanner("funding")
            else:
                reply = stop_scanner("funding")
        elif cmd == "/scan_tri":
            if len(parts) >= 2 and parts[1].lower() == "start":
                reply = start_scanner("tri")
            else:
                reply = stop_scanner("tri")
        elif cmd == "/scan_vol":
            if len(parts) >= 2 and parts[1].lower() == "start":
                reply = start_scanner("vol")
            else:
                reply = stop_scanner("vol")
        elif cmd == "/status":
            reply = status_report()
        else:
            reply = "Unknown command. /help"
    except Exception as e:
        app_log(f"Error handling cmd {text}: {e}")
        reply = f"Error: {e}"
    # send reply to the user who invoked command (so they know)
    # If TELEGRAM_TOKEN is set, reply via sendMessage to that chat id.
    if TELEGRAM_TOKEN:
        try:
            requests.post(TELEGRAM_API + "/sendMessage", json={"chat_id": cid, "text": reply}, timeout=5)
        except Exception as e:
            app_log(f"Failed to send reply: {e}")
    else:
        app_log("TELEGRAM_TOKEN not set; cannot reply via API.")
    return jsonify({"ok": True})

# Friendly endpoint to start/stop scanners via HTTP (optional)
@app.route("/control/<scanner>/<action>", methods=["POST","GET"])
def control(scanner: str, action: str):
    scanner = scanner.lower()
    action = action.lower()
    if action == "start":
        res = start_scanner(scanner)
    else:
        res = stop_scanner(scanner)
    return jsonify({"result": res})

@app.route("/status", methods=["GET"])
def http_status():
    return jsonify({
        "scanners": {k: ("running" if v["running"] else "stopped") for k,v in scanners_state.items()},
        "symbols": SYMBOLS,
        "logs": get_logs()[-50:]
    })

# ----------------------------- HELPER: set webhook -----------------------------
def set_telegram_webhook():
    if not WEBHOOK_URL:
        app_log("WEBHOOK_URL not set; skipping webhook registration")
        return
    if not TELEGRAM_TOKEN:
        app_log("TELEGRAM_TOKEN not set; cannot set webhook")
        return
    url = WEBHOOK_URL.rstrip("/") + "/webhook"
    try:
        r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=8)
        app_log(f"setWebhook result: {r.status_code} {r.text[:200]}")
    except Exception as e:
        app_log(f"setWebhook failed: {e}")

# ----------------------------- BOOT ------------------------------------------
if __name__ == "__main__":
    app_log("Starting arb-signals service")
    # init clients early
    get_ccxt_client(EXCHANGE_A_ID)
    get_ccxt_client(EXCHANGE_B_ID)
    # try to set webhook if WEBHOOK_URL provided
    set_telegram_webhook()
    # optionally send starting message to configured CHAT_ID
    if TELEGRAM_TOKEN and CHAT_ID:
        tg_send("🟢 Arb Signal Bot started (no trading — signals only). Use /help for commands.")
    # run flask
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), threaded=True)