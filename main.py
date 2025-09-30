#!/usr/bin/env python3
# main.py -- Web service + background periodic analyzer (Binance + Telegram)
import os
import time
import threading
import logging
from typing import List, Dict, Any
import numpy as np
import pandas as pd
import requests
from flask import Flask, jsonify
from dotenv import load_dotenv

# optional libs (wrap imports for clearer error messages)
try:
    from binance.client import Client
except Exception as e:
    Client = None

try:
    from ta.trend import EMAIndicator, MACD, ADXIndicator
    from ta.momentum import RSIIndicator
except Exception as e:
    EMAIndicator = MACD = ADXIndicator = RSIIndicator = None

# --------------------------
# Load env
# --------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "").strip()
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()

# behavior
CYCLE_SECONDS = int(os.getenv("CYCLE_SECONDS", "300"))   # 5 minutes default
TOP_N = int(os.getenv("TOP_N", "10"))
KL_INTERVAL = os.getenv("KL_INTERVAL", "1h")
KLINES_LIMIT = int(os.getenv("KLINES_LIMIT", "300"))

# --------------------------
# Logging
# --------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("crypto-coordinator")

# --------------------------
# Binance client init
# --------------------------
if Client is None:
    logger.error("python-binance is not installed or failed to import. Install 'python-binance' in requirements.")
    binance_client = None
else:
    try:
        binance_client = Client(api_key=BINANCE_API_KEY or None, api_secret=BINANCE_API_SECRET or None)
    except Exception as e:
        logger.exception("Failed to initialize Binance Client: %s", e)
        binance_client = None

# --------------------------
# Flask app
# --------------------------
app = Flask(__name__)

# --------------------------
# Helper: Telegram send with robust logging
# --------------------------
def send_telegram_message(text: str) -> Dict[str, Any]:
    """Send message to Telegram. Returns response dict or None."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured (BOT_TOKEN or CHAT_ID missing). Skipping send.")
        return None
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        try:
            j = resp.json()
        except Exception:
            j = {"ok": resp.ok, "status_code": resp.status_code, "text": resp.text}
        if not resp.ok or not j.get("ok", False):
            logger.warning("Telegram API response not OK: status=%s json=%s", resp.status_code, j)
        else:
            logger.info("Telegram message sent (len=%d)", len(text))
        return j
    except Exception as e:
        logger.exception("send_telegram_message error: %s", e)
        return None

# --------------------------
# Binance helpers
# --------------------------
def get_top_symbols(limit: int = TOP_N) -> List[str]:
    """Return top USDT symbols by quoteVolume (fallback to BTCUSDT)."""
    if not binance_client:
        logger.warning("No binance client, returning fallback ['BTCUSDT']")
        return ["BTCUSDT"]
    try:
        tickers = binance_client.get_ticker()  # returns list of dicts
        df = pd.DataFrame(tickers)
        if "quoteVolume" in df.columns:
            df["quoteVolume"] = pd.to_numeric(df["quoteVolume"], errors="coerce").fillna(0.0)
            df = df[df["symbol"].str.endswith("USDT")]
            df = df.sort_values("quoteVolume", ascending=False).head(limit)
            syms = df["symbol"].astype(str).tolist()
            logger.info("Top %d symbols: %s", len(syms), syms[:10])
            return syms
        # fallback: just return first symbols ending with USDT
        syms = [t["symbol"] for t in tickers if t.get("symbol","").endswith("USDT")]
        if not syms:
            raise RuntimeError("No USDT tickers found")
        return syms[:limit]
    except Exception as e:
        logger.exception("get_top_symbols error: %s", e)
        return ["BTCUSDT"]

def fetch_klines(symbol: str, interval: str = KL_INTERVAL, limit: int = KLINES_LIMIT) -> pd.DataFrame:
    """Fetch klines from Binance and return DataFrame with float columns."""
    if not binance_client:
        raise RuntimeError("Binance client not initialized")
    try:
        raw = binance_client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(raw, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades","tb_base","tb_quote","ignore"
        ])
        for c in ["open","high","low","close","volume","quote_asset_volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        logger.debug("Fetched %d klines for %s %s", len(df), symbol, interval)
        return df
    except Exception as e:
        logger.exception("fetch_klines(%s) failed: %s", symbol, e)
        raise

# --------------------------
# Indicators & features
# --------------------------
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add required columns used by strategy teams. Returns new df."""
    df = df.copy()
    if EMAIndicator is None:
        logger.error("ta (technical analysis) lib missing. Install 'ta'.")
        return df

    # EMA12/26 + crosses
    df["ema_fast"] = EMAIndicator(df["close"], window=12).ema_indicator()
    df["ema_slow"] = EMAIndicator(df["close"], window=26).ema_indicator()
    df["ema_cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1))
    df["ema_cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1))

    # RSI
    df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
    df["rsi_long"] = df["rsi"] < 30
    df["rsi_short"] = df["rsi"] > 70

    # MACD diff
    macd = MACD(df["close"])
    df["macd_diff"] = macd.macd_diff()
    df["macd_long"] = df["macd_diff"] > 0
    df["macd_short"] = df["macd_diff"] < 0

    # ADX
    df["adx"] = ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()

    # ATR for RR calc (simple)
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["close"].shift(1)).abs()
    df["tr3"] = (df["low"] - df["close"].shift(1)).abs()
    df["true_range"] = df[["tr1","tr2","tr3"]].max(axis=1)
    df["atr14"] = df["true_range"].rolling(14).mean().fillna(method="bfill")

    # Orderflow/funding placeholders (in future you can fetch real funding+OI)
    df["funding_long"] = False
    df["funding_short"] = False
    df["oi_long"] = False
    df["oi_short"] = False

    # risk_reward placeholder will be computed per-last row when computing entry/SL/TP
    return df

# --------------------------
# Teams (same pattern)
# --------------------------
class StrategyTeam:
    def analyze(self, df: pd.DataFrame) -> Dict[str, Any]:
        raise NotImplementedError

class TrendTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        last = df.iloc[-1]
        if bool(last.get("ema_cross_up")) and float(last.get("adx", 0)) > 25:
            return {"signal": "LONG", "confidence": 0.75, "patterns": ["ema_cross_up", "adx>25"]}
        if bool(last.get("ema_cross_down")) and float(last.get("adx", 0)) > 25:
            return {"signal": "SHORT", "confidence": 0.75, "patterns": ["ema_cross_down", "adx>25"]}
        return {"signal": "WATCH", "confidence": 0.25, "patterns": []}

class MomentumTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        last = df.iloc[-1]
        if bool(last.get("rsi_long")) and bool(last.get("macd_long")):
            return {"signal": "LONG", "confidence": 0.6, "patterns": ["rsi<30","macd_pos"]}
        if bool(last.get("rsi_short")) and bool(last.get("macd_short")):
            return {"signal": "SHORT", "confidence": 0.6, "patterns": ["rsi>70","macd_neg"]}
        return {"signal": "WATCH", "confidence": 0.25, "patterns": []}

class PatternTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        # placeholder: can implement false-break detection etc.
        last = df.iloc[-1]
        # no pattern currently
        return {"signal": "WATCH", "confidence": 0.25, "patterns": []}

class OrderflowTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        # placeholder: funding/OI based decisions (false by default)
        last = df.iloc[-1]
        if bool(last.get("funding_long")) and bool(last.get("oi_long")):
            return {"signal": "LONG", "confidence": 0.5, "patterns": ["funding_long","oi_long"]}
        if bool(last.get("funding_short")) and bool(last.get("oi_short")):
            return {"signal": "SHORT", "confidence": 0.5, "patterns": ["funding_short","oi_short"]}
        return {"signal": "WATCH", "confidence": 0.25, "patterns": []}

class RiskTeam(StrategyTeam):
    def analyze(self, df: pd.DataFrame):
        # compute simple RR using ATR: entry = last close; sl = entry - 1.5*atr ; tp = entry + 3*atr (LONG)
        last = df.iloc[-1]
        atr = float(last.get("atr14", last.get("atr14", np.nan))) if "atr14" in last else float(last.get("atr14", np.nan))
        # If ATR missing, assume not enough info -> PASS
        if np.isnan(atr) or atr <= 0:
            return {"signal": "PASS", "confidence": 1.0, "patterns": ["no_atr"]}
        entry = float(last["close"])
        sl = entry - 1.5 * atr
        tp = entry + 3.0 * atr
        rr = (tp - entry) / (entry - sl) if (entry - sl) != 0 else 0.0
        # attach rr to last (used later)
        return {"signal": "PASS" if rr >= 3.0 else "WATCH", "confidence": float(min(1, max(0, rr/5))), "patterns": ["rr=%.2f" % rr], "rr": rr}

# --------------------------
# Coordinator
# --------------------------
class Coordinator:
    def __init__(self, teams: List[StrategyTeam]):
        self.teams = teams

    def decide(self, df: pd.DataFrame) -> Dict[str, Any]:
        results = []
        for t in self.teams:
            try:
                r = t.analyze(df)
            except Exception as e:
                logger.exception("Team analyze failed: %s", e)
                r = {"signal": "WATCH", "confidence": 0.0, "patterns": ["error"]}
            results.append(r)

        # collect only active votes
        votes = [r for r in results if r["signal"] not in ("WATCH","PASS")]
        if not votes:
            return {"final_signal": "WATCH", "confidence": 0.0, "votes": results}

        long_votes = [v for v in votes if v["signal"] == "LONG"]
        short_votes = [v for v in votes if v["signal"] == "SHORT"]

        # require at least 2 team votes on one side
        if len(long_votes) >= 2 and len(long_votes) > len(short_votes):
            conf = float(np.mean([v.get("confidence", 0.0) for v in long_votes]))
            return {"final_signal": "LONG", "confidence": conf, "votes": results}
        if len(short_votes) >= 2 and len(short_votes) > len(long_votes):
            conf = float(np.mean([v.get("confidence", 0.0) for v in short_votes]))
            return {"final_signal": "SHORT", "confidence": conf, "votes": results}
        return {"final_signal": "WATCH", "confidence": 0.0, "votes": results}

# --------------------------
# Per-symbol analyze
# --------------------------
def analyze_symbol(symbol: str, interval: str = KL_INTERVAL) -> Dict[str, Any]:
    """Fetch data, compute indicators, run coordinator and return decision dict."""
    try:
        df = fetch_klines(symbol, interval=interval, limit=KLINES_LIMIT)
        df = add_indicators(df)
    except Exception as e:
        logger.exception("analyze_symbol: failed to fetch/indicators for %s: %s", symbol, e)
        return {"symbol": symbol, "error": str(e)}

    teams = [TrendTeam(), MomentumTeam(), PatternTeam(), OrderflowTeam(), RiskTeam()]
    coord = Coordinator(teams)
    decision = coord.decide(df)
    # attach some metadata
    decision["symbol"] = symbol
    decision["last_price"] = float(df["close"].iloc[-1]) if "close" in df.columns and len(df)>0 else None
    # try to attach rr if present in one of teams results
    for v in decision.get("votes", []):
        if "rr" in v:
            decision["rr"] = v["rr"]
            break
    return decision

# --------------------------
# Periodic runner
# --------------------------
_last_cycle_ts = None
_lock = threading.Lock()

def run_periodic_analysis():
    global _last_cycle_ts
    logger.info("Background periodic analysis thread started (cycle %ds, top=%d)", CYCLE_SECONDS, TOP_N)
    while True:
        try:
            symbols = get_top_symbols(TOP_N)
            signals = []
            for sym in symbols:
                try:
                    d = analyze_symbol(sym, KL_INTERVAL)
                    # if there's a clear LONG/SHORT
                    if d.get("final_signal") in ("LONG","SHORT"):
                        signals.append(d)
                    logger.info("Analyzed %s -> %s", sym, d.get("final_signal"))
                except Exception:
                    logger.exception("Error analyzing %s", sym)
                time.sleep(1.2)  # small pause to avoid bursts
            # prepare telegram message: one message summarizing positive signals
            if signals:
                txt = f"⚡ <b>Signals summary — {len(signals)} alerts</b>\n"
                for s in signals:
                    txt += f"\n<b>{s['symbol']}</b>: {s['final_signal']} (conf {s['confidence']:.2f}) price {s.get('last_price')}\n"
                    pat_list = []
                    for v in s.get("votes", []):
                        if v.get("patterns"):
                            pat_list.extend(v.get("patterns", []))
                    if pat_list:
                        txt += "patterns: " + ", ".join(map(str, pat_list)) + "\n"
                send_telegram_message(txt)
            else:
                logger.info("No alerts this cycle (checked %d symbols)", len(symbols))
                # optionally send heartbeat: uncomment if you want periodic pings
                # send_telegram_message(f"Heartbeat: no alerts in last cycle ({len(symbols)} symbols).")
        except Exception as e:
            logger.exception("run_periodic_analysis main loop error: %s", e)
        _last_cycle_ts = time.time()
        time.sleep(CYCLE_SECONDS)

# --------------------------
# Start background thread ONCE at import
# --------------------------
def _start_bg_once():
    # ensure we only start once per process
    if getattr(_start_bg_once, "started", False):
        return
    t = threading.Thread(target=run_periodic_analysis, daemon=True, name="periodic-analyzer")
    t.start()
    _start_bg_once.started = True
    logger.info("Background analyzer thread launched (daemon)")

# Start background thread immediately when module imported (works with gunicorn if workers=1)
try:
    _start_bg_once()
except Exception:
    logger.exception("Failed to start background thread at import time")

# --------------------------
# Flask endpoints
# --------------------------
@app.route("/")
def home():
    return "✅ Crypto Coordinator running"

@app.route("/analyze")
def manual_analyze():
    """One-shot analyze top few symbols and return JSON response (also sends Telegram)."""
    try:
        symbols = get_top_symbols(min(5, TOP_N))
        results = {}
        for s in symbols:
            d = analyze_symbol(s, KL_INTERVAL)
            results[s] = d
        # also send aggregated message (optional)
        # Build summary of non-WATCH signals
        positive = [v for v in results.values() if v.get("final_signal") in ("LONG","SHORT")]
        if positive:
            txt = f"🔔 Manual run alerts ({len(positive)})\n"
            for p in positive:
                txt += f"{p['symbol']}: {p['final_signal']} conf={p['confidence']:.2f} price={p.get('last_price')}\n"
            send_telegram_message(txt)
        return jsonify(results)
    except Exception as e:
        logger.exception("manual_analyze error: %s", e)
        return jsonify({"error": str(e)}), 500