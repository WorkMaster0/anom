#!/usr/bin/env python3
# main.py - Live DEX <-> MEXC monitor with Flask + WebSocket + Telegram webhook
import os
import time
import json
import logging
import asyncio
import requests
from datetime import datetime, timezone
from threading import Thread
from typing import Dict, Optional, Set, List
from flask import Flask, request, render_template_string, jsonify
from flask_socketio import SocketIO, emit

# Try import ccxt.pro (optional)
try:
    import ccxt.pro as ccxtpro
except Exception:
    ccxtpro = None

import ccxt  # sync for discovery/fallback

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
# For webhook: set your public URL like https://yourapp.onrender.com
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", "10000"))

STATE_FILE = "state.json"
POLL_INTERVAL_DEX = float(os.getenv("POLL_INTERVAL_DEX", "3.0"))
LIVE_BROADCAST_INTERVAL = float(os.getenv("LIVE_BROADCAST_INTERVAL", "2.0"))
SPREAD_MIN_PCT_ALERT = float(os.getenv("SPREAD_MIN_PCT_ALERT", "2.0"))
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "60"))
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Exchanges to use (ccxt ids). Adjust as needed.
EXCHANGES_FOR_FUTURES = ["mexc", "bybit", "lbank"]  # mexc is primary for CEX websocket

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("live-monitor")

# ---------------- STATE ----------------
state = {
    "symbols": [],          # list of token symbols like ["PEPE","DOGE"]
    "chat_id": None,        # telegram chat id to send messages
    "msg_id": None,         # live panel message id
    "monitoring": True,     # whether background tasks run
}
# runtime caches
dex_prices: Dict[str, float] = {}
cex_prices: Dict[str, float] = {}
last_alert_time: Dict[str, float] = {}

# ---------------- SAVE / LOAD ----------------
def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
                state.update(s)
                logger.info("Loaded state: %d symbols", len(state.get("symbols", [])))
    except Exception as e:
        logger.exception("load_state error: %s", e)

def save_state():
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.exception("save_state error: %s", e)

# ---------------- TELEGRAM HELPERS ----------------
def tg_send(text: str) -> Optional[dict]:
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        logger.debug("Telegram not configured or chat_id missing")
        return None
    try:
        payload = {
            "chat_id": state["chat_id"],
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        r = requests.post(TELEGRAM_API + "/sendMessage", json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.exception("tg_send error: %s", e)
        return None

def tg_edit(message_id: int, text: str):
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        return None
    try:
        payload = {"chat_id": state["chat_id"], "message_id": message_id, "text": text, "parse_mode": "Markdown"}
        r = requests.post(TELEGRAM_API + "/editMessageText", json=payload, timeout=10)
        if r.status_code != 200:
            logger.warning("tg_edit failed: %s %s", r.status_code, r.text)
        return r.json()
    except Exception as e:
        logger.exception("tg_edit error: %s", e)
        return None

# ---------------- DEX SOURCES ----------------
DEXSCREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"
DECTOOLS_PLACEHOLDER = None  # implement if API available
GMGN_PLACEHOLDER = None      # implement if API available

def fetch_price_from_dexscreener(symbol: str) -> Optional[float]:
    """Search Dexscreener for token symbol and return first pair priceUsd"""
    try:
        url = DEXSCREENER_SEARCH.format(q=symbol)
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        pairs = data.get("pairs") or data.get("pairsFound") or []
        if isinstance(pairs, list) and pairs:
            # choose first pair with priceUsd
            for p in pairs:
                price = p.get("priceUsd") or p.get("price")
                if price:
                    try:
                        return float(price)
                    except Exception:
                        continue
        # fallback tokens list
        tokens = data.get("tokens") or []
        for t in tokens:
            for p in t.get("pairs", []):
                try:
                    price = p.get("priceUsd")
                    if price:
                        return float(price)
                except Exception:
                    continue
    except Exception as e:
        logger.debug("dexscreener error for %s: %s", symbol, e)
    return None

# ---------------- Contract mapping utility ----------------
def generate_candidate_pairs(symbol: str) -> List[str]:
    """
    Heuristic mappings for symbol -> exchange pairs.
    Examples: "PEPE" -> ["PEPE/USDT", "PEPEUSDT", "PEPE/USDT:USDT"]
    We'll use common format SYMBOL/USDT.
    """
    s = symbol.upper()
    return [f"{s}/USDT", f"{s}USDT", f"{s}/USDT:USDT", f"{s}/USD"]

# ---------------- CEX (MEXC) watcher using ccxt.pro ----------------
class CEXWatcher:
    def __init__(self, exchange_id="mexc"):
        self.exchange_id = exchange_id
        self.client = None
        self.task = None
        self.running = False

    async def start(self):
        if ccxtpro is None:
            logger.warning("ccxt.pro not installed — CEX watcher disabled")
            return
        if self.running:
            return
        kwargs = {"enableRateLimit": True}
        # allow optional API keys from env
        api_key = os.getenv(f"{self.exchange_id.upper()}_API_KEY")
        api_secret = os.getenv(f"{self.exchange_id.upper()}_API_SECRET")
        if api_key and api_secret:
            kwargs["apiKey"] = api_key
            kwargs["secret"] = api_secret
        try:
            self.client = getattr(ccxtpro, self.exchange_id)(kwargs)
            # prefer futures/swap
            try:
                opts = self.client.options or {}
                if "defaultType" in opts:
                    self.client.options["defaultType"] = "future"
            except Exception:
                pass
        except Exception as e:
            logger.exception("Failed to init %s: %s", self.exchange_id, e)
            self.client = None
            return
        self.running = True
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except Exception:
                pass
        if self.client:
            try:
                await self.client.close()
            except Exception:
                pass
        self.client = None
        self.task = None

    async def _run(self):
        logger.info("%s watcher running", self.exchange_id)
        # loop watching tickers for monitored symbols (limit concurrency)
        while self.running:
            syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
            if not syms:
                await asyncio.sleep(1.0)
                continue
            # create small tasks for each symbol
            tasks = []
            semaphore = asyncio.Semaphore(12)
            async def watch_one(sym):
                pair_candidates = generate_candidate_pairs(sym)
                # try each mapping until success
                for pair in pair_candidates:
                    try:
                        async with semaphore:
                            ticker = await self.client.watch_ticker(pair)
                        last = ticker.get("last") or ticker.get("close") or ticker.get("price")
                        if last is not None:
                            cex_prices[sym] = float(last)
                            return
                    except Exception:
                        # try next mapping
                        await asyncio.sleep(0.0)
                        continue
                # if all candidates fail, set None (or keep old)
                return
            for s in syms:
                tasks.append(asyncio.create_task(watch_one(s)))
            # wait for a short window and then iterate (keeps WS subscriptions alive)
            try:
                await asyncio.wait(tasks, timeout=2.0)
            except Exception:
                pass
            # small sleep
            await asyncio.sleep(0.5)

# ---------------- DEX poller (Dexscreener + optional GMGN) ----------------
class DexPoller:
    def __init__(self):
        self.task = None
        self.running = False

    async def start(self):
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._run())

    async def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except Exception:
                pass
        self.task = None

    async def _run(self):
        logger.info("DEX Poller started (Dexscreener)")
        loop = asyncio.get_event_loop()
        while self.running:
            syms = list(state.get("symbols", []))[:MAX_SYMBOLS]
            if not syms:
                await asyncio.sleep(1.0)
                continue
            # run fetches in threadpool
            coros = [loop.run_in_executor(None, fetch_price_from_dexscreener, s) for s in syms]
            try:
                results = await asyncio.gather(*coros, return_exceptions=True)
                for s, res in zip(syms, results):
                    if isinstance(res, Exception) or res is None:
                        # skip / keep previous
                        continue
                    try:
                        dex_prices[s] = float(res)
                    except Exception:
                        continue
            except Exception as e:
                logger.debug("dex poller gather error: %s", e)
            await asyncio.sleep(POLL_INTERVAL_DEX)

# ---------------- SPREAD CHECK / ALERT ----------------
def check_and_alert(sym: str):
    dex = dex_prices.get(sym)
    cex = cex_prices.get(sym)
    if dex is None or cex is None:
        return
    if dex == 0:
        return
    pct = (cex - dex) / dex * 100.0
    if pct >= SPREAD_MIN_PCT_ALERT:
        now = time.time()
        last = last_alert_time.get(sym, 0)
        if now - last < 300:
            return
        last_alert_time[sym] = now
        msg = (
            "🔔 *Spread Opportunity Detected*\n"
            f"Symbol: `{sym}`\n"
            f"DEX price: `{dex:.8f}`\n"
            f"CEX price: `{cex:.8f}`\n"
            f"Spread: *{pct:.2f}%*\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        logger.info("ALERT %s (%.2f%%)", sym, pct)
        tg_send(msg)

# ---------------- FLASK + SOCKET.IO ----------------
app = Flask(__name__)
# use eventlet for concurrency (recommended with flask-socketio and ccxt.pro)
# ensure eventlet is installed in your env; flask-socketio will auto-detect available async workers.
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

# minimal web UI (Bootstrap + SocketIO)
INDEX_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Live DEX ↔ CEX Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
  </head>
  <body class="bg-light">
    <div class="container py-4">
      <h3>Live DEX ↔ CEX Monitor</h3>
      <div class="mb-2">
        <form id="addForm" class="row g-2">
          <div class="col-auto"><input id="symbol" class="form-control" placeholder="SYMBOL (e.g. PEPE)" autocomplete="off"></div>
          <div class="col-auto"><button class="btn btn-primary">Add</button></div>
          <div class="col-auto"><button id="clearBtn" class="btn btn-danger" type="button">Clear All</button></div>
        </form>
      </div>
      <div id="statusBadge" class="mb-2"></div>
      <div class="table-responsive">
        <table class="table table-sm table-bordered" id="liveTable">
          <thead class="table-light"><tr><th>Symbol</th><th>DEX (USD)</th><th>CEX (USD)</th><th>Δ%</th></tr></thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
      <div class="small text-muted">Connected clients: <span id="clients">0</span></div>
    </div>
    <script>
      const socket = io();
      const tbody = document.getElementById("tbody");
      const clientsEl = document.getElementById("clients");
      const statusBadge = document.getElementById("statusBadge");

      socket.on("connect", () => {
        console.log("connected");
      });
      socket.on("live.update", (data) => {
        // data: {symbols: [...], dex_prices: {...}, cex_prices: {...}, time: ...}
        const symbols = data.symbols || [];
        tbody.innerHTML = "";
        symbols.forEach(s => {
          const dex = data.dex_prices[s];
          const cex = data.cex_prices[s];
          let dexStr = dex == null ? "—" : dex.toFixed(8);
          let cexStr = cex == null ? "—" : cex.toFixed(8);
          let pct = "—";
          if (dex != null && cex != null && dex !== 0) {
            pct = ((cex - dex)/dex*100).toFixed(2) + "%";
          }
          const tr = document.createElement("tr");
          tr.innerHTML = `<td><strong>${s}</strong></td><td>${dexStr}</td><td>${cexStr}</td><td>${pct}</td>`;
          tbody.appendChild(tr);
        });
      });
      socket.on("clients", (n) => { clientsEl.innerText = n; });
      socket.on("status", (txt) => { statusBadge.innerHTML = '<span class="badge bg-info">'+txt+'</span>'; setTimeout(()=>statusBadge.innerHTML="",3000); });

      // add form
      document.getElementById("addForm").addEventListener("submit", (e) => {
        e.preventDefault();
        const sym = document.getElementById("symbol").value.trim().toUpperCase();
        if (!sym) return;
        socket.emit("add_symbol", sym);
        document.getElementById("symbol").value = "";
      });
      document.getElementById("clearBtn").addEventListener("click", () => {
        if (!confirm("Clear all symbols?")) return;
        socket.emit("clear_symbols");
      });
    </script>
  </body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML)

# Telegram webhook endpoint for commands (webhook mode)
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    # Telegram sends updates here; we support message commands only
    if not data:
        return jsonify({"ok": False}), 400
    # get message
    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return jsonify({"ok": True})
    chat = msg.get("chat", {})
    cid = chat.get("id")
    if not state.get("chat_id"):
        state["chat_id"] = cid
        save_state()
    text = (msg.get("text") or "").strip()
    if not text:
        return jsonify({"ok": True})
    logger.info("Webhook cmd from %s: %s", cid, text[:120])
    # simple command parsing
    cmd = text.split()[0].lower()
    if cmd == "/start":
        tg_send("🤖 Live monitor is online. Use /add SYMBOL")
    elif cmd == "/help":
        tg_send("Commands:\n/add SYMBOL\n/remove SYMBOL\n/list\n/start_monitor\n/stop_monitor\n/help")
    elif cmd == "/add":
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            sym = parts[1].strip().upper()
            if sym not in state["symbols"]:
                state["symbols"].append(sym)
                save_state()
                socketio.emit("status", f"Added {sym}")
                tg_send(f"✅ Added {sym}")
            else:
                tg_send(f"⚠️ {sym} already exists")
    elif cmd == "/remove":
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            sym = parts[1].strip().upper()
            if sym in state["symbols"]:
                state["symbols"].remove(sym)
                save_state()
                socketio.emit("status", f"Removed {sym}")
                tg_send(f"🗑 Removed {sym}")
            else:
                tg_send(f"⚠️ {sym} not monitored")
    elif cmd == "/list":
        tg_send("Monitored: " + (", ".join(state["symbols"]) if state["symbols"] else "—"))
    elif cmd == "/start_monitor":
        state["monitoring"] = True
        save_state()
        tg_send("✅ Monitoring resumed")
    elif cmd == "/stop_monitor":
        state["monitoring"] = False
        save_state()
        tg_send("🛑 Monitoring paused")
    else:
        tg_send("❓ Unknown command. /help")
    return jsonify({"ok": True})

# SocketIO events
@socketio.on("connect")
def on_connect():
    emit("clients", len(socketio.server.manager.get_participants('/', '/')) if hasattr(socketio, 'server') else 1)
    # send initial live update
    emit("live.update", {"symbols": state.get("symbols", []), "dex_prices": dex_prices, "cex_prices": cex_prices, "time": time.time()})

@socketio.on("add_symbol")
def on_add_symbol(sym):
    s = sym.strip().upper()
    if not s:
        return
    if s not in state["symbols"]:
        state["symbols"].append(s)
        save_state()
        emit("status", f"Added {s}", broadcast=True)
    else:
        emit("status", f"{s} already monitored")

@socketio.on("clear_symbols")
def on_clear():
    state["symbols"] = []
    save_state()
    emit("status", "Cleared symbols", broadcast=True)

# ---------------- ORCHESTRATION: background async loop ----------------
class Orchestrator:
    def __init__(self):
        self.loop = None
        self.thread = None
        self.cex = CEXWatcher("mexc")
        self.dex = DexPoller()
        self.tasks: List[asyncio.Task] = []
        self.running = False

    def start(self):
        if self.running:
            return
        # start asyncio loop in separate thread
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        self.running = True

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        # load state
        load_state()
        # start components
        logger.info("Starting background components")
        # start cex watcher if available
        if ccxtpro:
            await self.cex.start()
        else:
            logger.warning("ccxt.pro not available; CEX websocket disabled")
        await self.dex.start()
        # live broadcaster task: emits live.update to socketio regularly
        async def broadcaster():
            while True:
                if state.get("monitoring", True):
                    # run spread checks
                    for s in list(state.get("symbols", []))[:MAX_SYMBOLS]:
                        try:
                            check_and_alert(s)
                        except Exception:
                            pass
                # emit live update
                try:
                    socketio.emit("live.update", {"symbols": state.get("symbols", []), "dex_prices": dex_prices, "cex_prices": cex_prices, "time": time.time()})
                except Exception:
                    pass
                await asyncio.sleep(LIVE_BROADCAST_INTERVAL)
        # periodic discovery (optional) - we skip heavy discovery here
        # start broadcaster
        broadcaster_task = asyncio.create_task(broadcaster())
        self.tasks.append(broadcaster_task)
        # also run a lightweight keepalive for cex if ccxt.pro used
        try:
            await asyncio.gather(*self.tasks)
        except asyncio.CancelledError:
            logger.info("Orchestrator tasks cancelled")
        finally:
            await self._shutdown()

    async def _shutdown(self):
        try:
            await self.cex.stop()
        except Exception:
            pass
        try:
            await self.dex.stop()
        except Exception:
            pass

    def stop(self):
        if not self.running:
            return
        # cancel tasks and stop loop
        async def _stop_all():
            for t in list(asyncio.all_tasks(loop=self.loop)):
                try:
                    t.cancel()
                except Exception:
                    pass
        fut = asyncio.run_coroutine_threadsafe(_stop_all(), self.loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2)
        self.running = False

# ---------------- BOOT ----------------
orchestrator = Orchestrator()

if __name__ == "__main__":
    logger.info("🚀 Starting Live DEX<->CEX monitor")
    # load config
    load_state()
    # set Telegram webhook if provided and token exists
    if TELEGRAM_TOKEN and WEBHOOK_URL:
        try:
            url = WEBHOOK_URL.rstrip("/") + "/webhook"
            r = requests.get(f"{TELEGRAM_API}/setWebhook?url={url}", timeout=10)
            logger.info("Set webhook result: %s", r.text[:200])
        except Exception as e:
            logger.warning("Failed to set webhook: %s", e)

    # start background orchestrator
    orchestrator.start()
    # run flask socketio (use eventlet for concurrency)
    # NOTE: flask-socketio will use eventlet/gevent if available. eventlet recommended.
    socketio.run(app, host="0.0.0.0", port=PORT)