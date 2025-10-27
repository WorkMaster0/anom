#!/usr/bin/env python3
"""
Live DEX <-> MEXC price monitor with Telegram live-updates.

Features:
 - ccxt.pro watch_ticker for MEXC (websocket)
 - Dexscreener REST polling for DEX prices (public API)
 - single Telegram message updated repeatedly (editMessageText) with live table
 - commands via long-polling: /start, /stop, /add SYMBOL, /remove SYMBOL, /list, /help
 - state persisted in state.json
"""

import os
import asyncio
import time
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Set
import requests
import math

# try to import ccxt.pro; if not available we will fall back to raising at runtime
try:
    import ccxt.pro as ccxtpro
except Exception:
    ccxtpro = None

# CONFIG
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")  # string or int
STATE_FILE = "state.json"
POLL_INTERVAL_DEX = float(os.getenv("POLL_INTERVAL_DEX", "3.0"))  # seconds for DEX polling
LIVE_UPDATE_INTERVAL = float(os.getenv("LIVE_UPDATE_INTERVAL", "3.0"))  # seconds to edit Telegram message
MEXC_RECONNECT_DELAY = 5.0
SPREAD_MIN_PCT_ALERT = float(os.getenv("SPREAD_MIN_PCT_ALERT", "2.0"))  # optional spread alert threshold
MAX_SYMBOLS = int(os.getenv("MAX_SYMBOLS", "40"))  # prevent over-subscribing in demo
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("live-monitor")

# In-memory state
state = {
    "symbols": [],      # list of symbol strings like "PEPE" or "DOGE"
    "msg_id": None,     # Telegram message id for live panel
    "chat_id": CHAT_ID
}

# runtime caches
mexc_prices: Dict[str, float] = {}   # symbol -> price (from MEXC /ccxt.pro)
dex_prices: Dict[str, float] = {}    # symbol -> price (from Dexscreener)
last_alert_time: Dict[str, float] = {}  # symbol -> timestamp of last spread alert

# load/save state helpers
def load_state():
    global state
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                s = json.load(f)
                # basic validation
                state["symbols"] = s.get("symbols", [])
                state["msg_id"] = s.get("msg_id")
                state["chat_id"] = s.get("chat_id", state["chat_id"])
                logger.info("Loaded state: %d symbols", len(state["symbols"]))
    except Exception as e:
        logger.exception("load_state error: %s", e)

def save_state():
    try:
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(state, f, indent=2)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as e:
        logger.exception("save_state error: %s", e)

# Telegram helpers
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

def tg_edit_text(message_id: int, text: str):
    if not TELEGRAM_TOKEN or not state.get("chat_id"):
        return None
    try:
        payload = {
            "chat_id": state["chat_id"],
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        r = requests.post(TELEGRAM_API + "/editMessageText", json=payload, timeout=10)
        # Telegram returns 200 even on edit fails sometimes. Check
        if r.status_code != 200:
            logger.warning("edit failed %s %s", r.status_code, r.text)
        return r.json()
    except Exception as e:
        logger.exception("tg_edit_text error: %s", e)
        return None

# Utility: pretty table generator (Markdown)
def build_live_table() -> str:
    symbols = state.get("symbols", [])
    if not symbols:
        return "🟡 *No symbols monitored.* Use `/add SYMBOL` to add.\n\n`/help` for commands."
    # table header
    lines = []
    lines.append("📡 *Live DEX ↔ MEXC Prices*")
    lines.append(f"_Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_\n")
    lines.append("`SYMBOL    DEX(USD)      MEXC(USD)     Δ%`")
    lines.append("`--------------------------------------------------`")
    for s in symbols:
        dex = dex_prices.get(s)
        mexc = mexc_prices.get(s)
        dex_str = f"{dex:.8f}" if dex is not None else "—"
        mexc_str = f"{mexc:.8f}" if mexc is not None else "—"
        pct_str = "—"
        if dex is not None and mexc is not None and dex != 0:
            pct = (mexc - dex) / dex * 100.0
            pct_str = f"{pct:+6.2f}%"
        lines.append(f"`{s:<7}` {dex_str:>12}  {mexc_str:>12}  {pct_str:>7}")
    lines.append("\n`/add SYMBOL  /remove SYMBOL  /list  /help`")
    return "\n".join(lines)

# DEX price fetch — Dexscreener search & first pair price
DEXS_CREENER_SEARCH = "https://api.dexscreener.com/latest/dex/search/?q={q}"

def fetch_price_from_dexscreener(symbol: str) -> Optional[float]:
    """
    Try to find token by symbol via dexscreener search and return first pair priceUsd if found.
    """
    try:
        url = DEXS_CREENER_SEARCH.format(q=symbol)
        r = requests.get(url, timeout=8)
        r.raise_for_status()
        data = r.json()
        # result structure: {"pairs": [...]} or {"tokens": [...]} depending on endpoint.
        # Dexscreener search returns 'pairs' root.
        pairs = data.get("pairs") or data.get("pairsFound") or []
        if isinstance(pairs, list) and pairs:
            # pick first pair with priceUsd
            for p in pairs:
                price = p.get("priceUsd") or p.get("priceUsd")
                try:
                    if price is not None:
                        return float(price)
                except Exception:
                    continue
        # fallback: sometimes response contains tokens list with 'pairs'
        tokens = data.get("tokens") or []
        for t in tokens:
            for p in t.get("pairs", []):
                try:
                    return float(p.get("priceUsd"))
                except Exception:
                    continue
    except Exception as e:
        logger.debug("dexscreener fetch error for %s: %s", symbol, e)
    return None

# MEXC websocket watcher using ccxt.pro
class MEXCWatcher:
    def __init__(self):
        self.client = None
        self.running = False
        self.task = None

    async def start(self):
        if ccxtpro is None:
            logger.error("ccxt.pro not available — MEXC watcher disabled")
            return
        if self.running:
            return
        # create client with optional API keys
        kwargs = {"enableRateLimit": True}
        api_key = os.getenv("MEXC_API_KEY")
        api_secret = os.getenv("MEXC_API_SECRET")
        if api_key and api_secret:
            kwargs["apiKey"] = api_key
            kwargs["secret"] = api_secret
        try:
            self.client = getattr(ccxtpro, "mexc")(kwargs)
        except Exception as e:
            logger.exception("Failed to initialize mexc client: %s", e)
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
        logger.info("MEXC watcher started")
        # We'll subscribe to tickers one-by-one in a loop (watch_ticker)
        # Gather symbols
        while self.running:
            symbols = list(state.get("symbols", []))[:MAX_SYMBOLS]
            if not symbols:
                await asyncio.sleep(1.0)
                continue
            # Try to watch many tickers: some exchanges allow bulk, but watch_ticker per symbol is fine.
            # We'll run watchers in parallel but limit concurrency.
            sem = asyncio.Semaphore(10)
            async def watch_one(sym):
                # map SYMBOL -> "SYM/USDT" pair (simple heuristic)
                pair = f"{sym.upper()}/USDT"
                try:
                    async with sem:
                        ticker = await self.client.watch_ticker(pair)
                        last = ticker.get("last") or ticker.get("close") or ticker.get("price")
                        if last is not None:
                            try:
                                mexc_prices[sym] = float(last)
                            except Exception:
                                mexc_prices[sym] = None
                except Exception as e:
                    # don't spam logs; debug level
                    logger.debug("mexc watch error for %s: %s", pair, e)
            # launch watchers concurrently for this iteration
            tasks = [asyncio.create_task(watch_one(s)) for s in symbols]
            # wait a short period then continue to next batch so we keep stream alive
            try:
                await asyncio.wait(tasks, timeout=2.0)
            except Exception:
                pass
            # small sleep before next iteration
            await asyncio.sleep(0.5)

# Dex poller
class DexPoller:
    def __init__(self):
        self.running = False
        self.task = None

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
        logger.info("DEX poller started (Dexscreener)")
        while self.running:
            symbols = list(state.get("symbols", []))[:MAX_SYMBOLS]
            if not symbols:
                await asyncio.sleep(1.0)
                continue
            # poll each symbol in executor to avoid blocking
            loop = asyncio.get_event_loop()
            coros = []
            for s in symbols:
                coros.append(loop.run_in_executor(None, fetch_price_from_dexscreener, s))
            try:
                results = await asyncio.gather(*coros, return_exceptions=True)
                for s, res in zip(symbols, results):
                    if isinstance(res, Exception) or res is None:
                        # keep old price if exists; do nothing
                        continue
                    try:
                        dex_prices[s] = float(res)
                    except Exception:
                        continue
            except Exception as e:
                logger.debug("dex poll gather error: %s", e)
            # sleep
            await asyncio.sleep(POLL_INTERVAL_DEX)

# Live updater (edits single Telegram message)
class LiveUpdater:
    def __init__(self):
        self.running = False
        self.task = None

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
        logger.info("Live updater started")
        # If we don't have a message yet, send one
        if not state.get("msg_id"):
            res = tg_send(build_live_table())
            if res and isinstance(res, dict):
                # response format: {"ok": True, "result": { "message_id": ... }}
                try:
                    mid = res.get("result", {}).get("message_id")
                    if mid:
                        state["msg_id"] = int(mid)
                        save_state()
                except Exception:
                    pass
        # loop and edit
        while self.running:
            if state.get("msg_id"):
                try:
                    txt = build_live_table()
                    tg_edit_text(state["msg_id"], txt)
                except Exception as e:
                    logger.debug("edit error: %s", e)
            else:
                # try to send initial message again
                res = tg_send(build_live_table())
                if res and isinstance(res, dict):
                    try:
                        mid = res.get("result", {}).get("message_id")
                        if mid:
                            state["msg_id"] = int(mid)
                            save_state()
                    except Exception:
                        pass
            await asyncio.sleep(LIVE_UPDATE_INTERVAL)

# Simple spread alert: when both prices present and abs diff relative > threshold, send one-time alert
def check_and_alert_spread_for_symbol(sym: str):
    dex = dex_prices.get(sym)
    mexc = mexc_prices.get(sym)
    if dex is None or mexc is None:
        return
    if dex == 0:
        return
    pct = (mexc - dex) / dex * 100.0
    # only alert if above threshold (positive)
    if pct >= SPREAD_MIN_PCT_ALERT:
        now = time.time()
        last = last_alert_time.get(sym, 0)
        if now - last < 300:  # 5 min cooldown per symbol alert
            return
        last_alert_time[sym] = now
        msg = (
            "🔔 *Spread Opportunity Detected*\n"
            f"Symbol: `{sym}`\n"
            f"DEX price: `{dex:.8f}`\n"
            f"MEXC price: `{mexc:.8f}`\n"
            f"Spread: *{pct:.2f}%*\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        tg_send(msg)

# Telegram long-polling for commands
async def telegram_poller(loop):
    logger.info("Starting Telegram poller (long-polling)")
    offset = None
    while True:
        try:
            params = {"timeout": 20}
            if offset:
                params["offset"] = offset
            r = await loop.run_in_executor(None, lambda: requests.get(TELEGRAM_API + "/getUpdates", params=params, timeout=30))
            if r.status_code != 200:
                await asyncio.sleep(1.0)
                continue
            data = r.json()
            if not data.get("ok"):
                await asyncio.sleep(1.0)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                # handle messages
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                # set chat_id into state if not set
                if not state.get("chat_id"):
                    state["chat_id"] = chat.get("id")
                    save_state()
                    logger.info("Chat id set to %s", state["chat_id"])
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                # commands
                if text.startswith("/start"):
                    tg_send("🤖 Live monitor started. Use /add SYMBOL to add tokens.")
                elif text.startswith("/help"):
                    tg_send(
                        "Commands:\n"
                        "/add SYMBOL - add token (e.g. PEPE)\n"
                        "/remove SYMBOL - remove token\n"
                        "/list - list monitored\n"
                        "/start_monitor - start live monitor\n"
                        "/stop_monitor - stop live monitor\n"
                        "/status - status"
                    )
                elif text.startswith("/add "):
                    parts = text.split(maxsplit=1)
                    if len(parts) == 2:
                        sym = parts[1].strip().upper()
                        if sym in state["symbols"]:
                            tg_send(f"⚠️ {sym} already monitored.")
                        else:
                            state["symbols"].append(sym)
                            save_state()
                            tg_send(f"✅ Added {sym}.")
                    else:
                        tg_send("Usage: /add SYMBOL")
                elif text.startswith("/remove "):
                    parts = text.split(maxsplit=1)
                    if len(parts) == 2:
                        sym = parts[1].strip().upper()
                        if sym in state["symbols"]:
                            state["symbols"].remove(sym)
                            save_state()
                            tg_send(f"🗑 Removed {sym}.")
                        else:
                            tg_send(f"⚠️ {sym} not in monitored list.")
                    else:
                        tg_send("Usage: /remove SYMBOL")
                elif text.startswith("/list"):
                    syms = state.get("symbols", [])
                    tg_send("Monitored: " + (", ".join(syms) if syms else "—"))
                elif text.startswith("/start_monitor"):
                    # starting is handled by main loop tasks; just confirm
                    tg_send("✅ Monitor is running (if not, start the program).")
                elif text.startswith("/stop_monitor"):
                    # user can clear msg
                    if state.get("msg_id"):
                        try:
                            # delete message
                            requests.post(TELEGRAM_API + "/deleteMessage", json={"chat_id": state["chat_id"], "message_id": state["msg_id"]}, timeout=5)
                        except Exception:
                            pass
                        state["msg_id"] = None
                        save_state()
                    tg_send("🛑 Live panel cleared.")
                elif text.startswith("/status"):
                    txt = f"Symbols: {', '.join(state.get('symbols', []) )}\nmsg_id: {state.get('msg_id')}"
                    tg_send(txt)
                else:
                    tg_send("❓ Unknown command. /help")
        except Exception as e:
            logger.debug("telegram poller error: %s", e)
            await asyncio.sleep(1.0)

# Orchestration
async def main():
    load_state()
    # start components
    mexc = MEXCWatcher()
    dex = DexPoller()
    live = LiveUpdater()

    # attempt to start MEXC watcher (if ccxt.pro available)
    if ccxtpro:
        await mexc.start()
    else:
        logger.warning("ccxt.pro not available; MEXC watcher disabled. Install ccxt.pro for live CEX prices.")

    await dex.start()
    await live.start()

    # launch telegram poller
    loop = asyncio.get_event_loop()
    tg_task = asyncio.create_task(telegram_poller(loop))

    # small periodic task to check spreads and raise alerts
    async def spread_checker():
        while True:
            for s in list(state.get("symbols", []))[:MAX_SYMBOLS]:
                try:
                    check_and_alert_spread_for_symbol(s)
                except Exception:
                    pass
            await asyncio.sleep(5.0)
    spread_task = asyncio.create_task(spread_checker())

    try:
        # run forever
        await asyncio.gather(tg_task, spread_task)
    finally:
        # cleanup
        await mexc.stop()
        await dex.stop()
        await live.stop()

if __name__ == "__main__":
    logger.info("🚀 Starting Live DEX<->MEXC monitor")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")