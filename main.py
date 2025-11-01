# main.py
import os
import time
import json
import logging
import io
from datetime import datetime, timezone
from threading import Thread
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import numpy as np
import requests
import mplfinance as mpf
from flask import Flask, jsonify, request

from binance.client import Client
from binance.exceptions import BinanceAPIException

# ---------------- CONFIG / ENV ----------------
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
PORT = int(os.getenv("PORT", "5000"))
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "6"))
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", str(5 * 60)))  # 5 minutes default
TOP_N = int(os.getenv("TOP_N", "50"))  # scan top N by 24h change

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("smc_bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("smc-bot")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    logger.warning("TELEGRAM token/chat not set - alerts will not be sent.")

# ---------------- BINANCE CLIENT ----------------
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET, {"timeout": 10})

# ---------------- STATE ----------------
STATE_FILE = "smc_state.json"
def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.exception("load_state error: %s", e)
    return {"last_scan": None, "signals": {}}

def save_state(state):
    try:
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as e:
        logger.exception("save_state error: %s", e)

state = load_state()

# ---------------- TELEGRAM ----------------
MD_ESC = r"_*[]()~`>#+-=|{}.!"
import re
def escape_md_v2(text: str) -> str:
    return re.sub(f"([{re.escape(MD_ESC)}])", r"\\\1", str(text))

def send_telegram(text: str, photo_buf: io.BytesIO = None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("Telegram not configured, skipping send.")
        return False
    try:
        if photo_buf:
            files = {'photo': ('signal.png', photo_buf.getvalue(), 'image/png')}
            data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': escape_md_v2(text), 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=15)
        else:
            payload = {"chat_id": TELEGRAM_CHAT_ID, "text": escape_md_v2(text), "parse_mode": "MarkdownV2"}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
        return True
    except Exception as e:
        logger.exception("send_telegram error: %s", e)
        return False

# ---------------- FETCH HELPERS ----------------
def fetch_top_symbols_by_change(limit=TOP_N):
    """Беремо топ N за абсолютною зміною % на ф'ючерсах (USDT-контракти)."""
    try:
        tickers = client.futures_ticker()  # list of dicts
        usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
        sorted_pairs = sorted(usdt, key=lambda x: abs(float(x.get("priceChangePercent", 0))), reverse=True)
        return [d["symbol"] for d in sorted_pairs[:limit]]
    except Exception as e:
        logger.exception("fetch_top_symbols_by_change error: %s", e)
        # fallback common
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]


def fetch_klines_df(symbol: str, interval: str = "5m", limit: int = 500) -> pd.DataFrame | None:
    try:
        kl = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(kl, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"
        ])
        for c in ["open","high","low","close","volume"]:
            df[c] = df[c].astype(float)
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
        df.set_index("open_time", inplace=True)
        return df
    except BinanceAPIException as e:
        logger.error("Binance error fetch klines %s: %s", symbol, e)
    except Exception as e:
        logger.exception("fetch_klines_df error: %s", e)
    return None

def get_funding(symbol: str):
    try:
        fr = client.futures_funding_rate(symbol=symbol, limit=1)
        return float(fr[0]["fundingRate"])
    except Exception:
        return 0.0

def get_open_interest(symbol: str):
    try:
        oi = client.futures_open_interest(symbol=symbol)
        return float(oi.get("openInterest", 0))
    except Exception:
        return 0.0

def get_recent_liqs(symbol: str, limit=50):
    try:
        liqs = client.futures_liquidation_orders(symbol=symbol, limit=limit)
        # return sums by side
        long = sum(float(x["price"]) * float(x["origQty"]) for x in liqs if x.get("side") == "SELL")
        short = sum(float(x["price"]) * float(x["origQty"]) for x in liqs if x.get("side") == "BUY")
        return {"long": long, "short": short}
    except Exception:
        return {"long": 0.0, "short": 0.0}

# ---------------- SMC FEATURE ENGINEERING ----------------
def apply_smc_features(df: pd.DataFrame) -> pd.DataFrame:
    """Compute SMC-ish features used by detectors (non-indicator heavy)."""
    df = df.copy()
    # basic structure
    df["body"] = df["close"] - df["open"]
    df["range"] = df["high"] - df["low"]
    df["upper_wick"] = df["high"] - df[["close","open"]].max(axis=1)
    df["lower_wick"] = df[["close","open"]].min(axis=1) - df["low"]
    # dynamic levels: last 20 highs/lows
    df["s_support"] = df["low"].rolling(20, min_periods=1).min()
    df["s_resistance"] = df["high"].rolling(20, min_periods=1).max()
    # volume context
    df["vol_ma20"] = df["volume"].rolling(20, min_periods=1).mean()
    df["vol_spike"] = df["volume"] > 2.0 * df["vol_ma20"]
    # wick exhaustion
    df["long_lower_wick"] = df["lower_wick"] > 1.5 * df["range"]  # strong lower wick
    df["long_upper_wick"] = df["upper_wick"] > 1.5 * df["range"]  # strong upper wick
    # detect simple fair value gaps (FVG) - three candle heuristic:
    df["fvg"] = False
    # we'll mark FVG at index i if candle i-2 high < candle i low (up gap) or i-2 low > i high (down gap)
    for i in range(2, len(df)):
        if df["high"].iat[i-2] < df["low"].iat[i]:
            # up FVG at i (gap between high[i-2] and low[i])
            df["fvg"].iat[i] = True
        elif df["low"].iat[i-2] > df["high"].iat[i]:
            df["fvg"].iat[i] = True
    # simple imbalance (single-candle strong body)
    df["imbalance_up"] = (df["body"] > 0) & (df["body"] > 0.6 * df["range"])
    df["imbalance_down"] = (df["body"] < 0) & (abs(df["body"]) > 0.6 * df["range"])
    # order-block heuristic: big opposite candle preceding a reversal (simplified)
    df["order_block_bull"] = False
    df["order_block_bear"] = False
    for i in range(1, len(df)):
        # bull order block: prior candle was big bearish and followed by bullish reaction
        if df["body"].iat[i-1] < -0.005 * df["close"].iat[i-1] and df["body"].iat[i] > 0:
            df["order_block_bull"].iat[i] = True
        if df["body"].iat[i-1] > 0.005 * df["close"].iat[i-1] and df["body"].iat[i] < 0:
            df["order_block_bear"].iat[i] = True
    # swings for BOS/CHOCH: compute local swing highs/lows
    df["swing_high"] = df["high"][(df["high"].shift(1) < df["high"]) & (df["high"].shift(-1) < df["high"])]
    df["swing_low"] = df["low"][(df["low"].shift(1) > df["low"]) & (df["low"].shift(-1) > df["low"])]
    # forward fill NaNs for convenience, we'll use last valid
    return df

# ---------------- STRUCTURE DETECTION (BOS / CHOCH) ----------------
def detect_structure(df: pd.DataFrame):
    """
    Very simple BOS/CHOCH detector:
      - find last significant swing high & low (by looking back N candles)
      - if price breaks previous swing high after being below -> BOS (bull)
      - if price breaks previous swing low after being above -> BOS (bear)
      - CHOCH is detected when direction flips (price breaks opposite side).
    This is heuristic — intended to provide structural context.
    """
    N = 60  # lookback
    sub = df.tail(N)
    swings_high = sub["swing_high"].dropna()
    swings_low = sub["swing_low"].dropna()
    out = {"last_swing_high": None, "last_swing_low": None, "structure": "unknown", "choch": False}
    if not swings_high.empty:
        sh_idx = swings_high.index[-1]
        out["last_swing_high"] = (sh_idx, float(swings_high.iloc[-1]))
    if not swings_low.empty:
        sl_idx = swings_low.index[-1]
        out["last_swing_low"] = (sl_idx, float(swings_low.iloc[-1]))
    last_close = float(df["close"].iat[-1])
    # determine structure
    if out["last_swing_high"] and last_close > out["last_swing_high"][1]:
        out["structure"] = "bull"
    elif out["last_swing_low"] and last_close < out["last_swing_low"][1]:
        out["structure"] = "bear"
    # CHOCH: crude detection — previous structure opposite recently
    # (if within lookback price previously was opposite)
    prev_close = float(df["close"].iat[-5]) if len(df) > 5 else last_close
    if out["structure"] == "bull" and prev_close < out.get("last_swing_low", (None, float("inf")))[1]:
        out["choch"] = True
    if out["structure"] == "bear" and prev_close > out.get("last_swing_high", (None, -1))[1]:
        out["choch"] = True
    return out

# ---------------- SMC DETECTOR ----------------
def detect_smc_setup(df: pd.DataFrame, funding: float, oi: float, liqs: dict):
    """
    Combine features into human-readable SMC-style scenarios.
    Returns action (WATCH/LONG/SHORT), votes (list), confidence [0..1], notes.
    """
    last = df.iloc[-1]
    votes = []
    confidence = 0.35

    # structure
    struct = detect_structure(df)
    if struct["structure"] == "bull":
        votes.append("structure_bull"); confidence += 0.05
    elif struct["structure"] == "bear":
        votes.append("structure_bear"); confidence += 0.05
    if struct["choch"]:
        votes.append("CHOCH"); confidence += 0.08

    # liquidity grabs & orderblocks
    if last.get("liquidity_grab_long", False) or last.get("long_lower_wick", False) or last.get("order_block_bull", False):
        votes.append("smart_liq_buy"); confidence += 0.12
    if last.get("liquidity_grab_short", False) or last.get("long_upper_wick", False) or last.get("order_block_bear", False):
        votes.append("smart_liq_sell"); confidence += 0.12

    # FVG / imbalance
    if last.get("fvg", False):
        votes.append("FVG"); confidence += 0.08
    if last.get("imbalance_up", False): votes.append("imbalance_up"); confidence += 0.06
    if last.get("imbalance_down", False): votes.append("imbalance_down"); confidence += 0.06

    # volume & cluster
    if last.get("vol_spike", False):
        votes.append("volume_spike"); confidence += 0.08

    # funding / oi / liquidations context
    if funding > 0.0005:
        votes.append("funding_long_bias"); confidence += 0.04
    if funding < -0.0005:
        votes.append("funding_short_bias"); confidence += 0.04
    if oi and oi > 0:
        # naively: large OI -> add weight
        if oi > 1e8:
            votes.append("high_OI"); confidence += 0.04

    # recent liquidations
    if liqs.get("long", 0) > 5e6:
        votes.append("liq_long_cluster"); confidence += 0.06
    if liqs.get("short", 0) > 5e6:
        votes.append("liq_short_cluster"); confidence += 0.06

    # pattern combination -> final action
    action = "WATCH"
    # bullish combos
    if ("smart_liq_buy" in votes or "FVG" in votes or "imbalance_up" in votes) and struct["structure"] in ("bull", "unknown"):
        action = "LONG"
    # bearish combos
    if ("smart_liq_sell" in votes or "imbalance_down" in votes or "FVG" in votes) and struct["structure"] in ("bear", "unknown"):
        action = "SHORT"
    # if contradictory signals, downgrade
    if ("smart_liq_buy" in votes and "smart_liq_sell" in votes) or ("liq_long_cluster" in votes and "liq_short_cluster" in votes):
        confidence *= 0.6

    confidence = max(0.0, min(1.0, confidence))
    notes = {
        "structure": struct,
        "funding": funding,
        "open_interest": oi,
        "liquidations": liqs
    }
    return action, votes, confidence, notes

# ---------------- QUALITY SCORING & RR ----------------
def compute_entry_sl_tp(last_close, action, last_support, last_resistance):
    """Entry at current price, SL by structure, 3 TP levels by resistance/support."""
    entry = last_close
    if action == "LONG":
        sl = last_support * 0.995 if last_support and last_support > 0 else last_close * 0.985
        tp1 = entry + (last_resistance - entry) * 0.33 if last_resistance and last_resistance > entry else entry * 1.002
        tp2 = entry + (last_resistance - entry) * 0.66 if last_resistance and last_resistance > entry else entry * 1.005
        tp3 = last_resistance if last_resistance and last_resistance > entry else entry * 1.01
    elif action == "SHORT":
        sl = last_resistance * 1.005 if last_resistance and last_resistance > 0 else last_close * 1.015
        tp1 = entry - (entry - last_support) * 0.33 if last_support and last_support < entry else entry * 0.998
        tp2 = entry - (entry - last_support) * 0.66 if last_support and last_support < entry else entry * 0.995
        tp3 = last_support if last_support and last_support < entry else entry * 0.99
    else:
        sl = tp1 = tp2 = tp3 = entry
    # calculate RR (for tp1; can be invalid if sl==entry)
    try:
        rr1 = (tp1 - entry) / (entry - sl) if action == "LONG" else (entry - tp1) / (sl - entry)
    except Exception:
        rr1 = 0.0
    return entry, sl, tp1, tp2, tp3, rr1

def quality_score(votes, confidence, rr_best):
    # quick mapping: more important votes add weight
    weight_map = {
        "CHOCH": 0.15, "FVG": 0.12, "smart_liq_buy": 0.12, "smart_liq_sell": 0.12,
        "volume_spike": 0.08, "liq_long_cluster": 0.08, "liq_short_cluster": 0.08,
        "imbalance_up": 0.06, "imbalance_down": 0.06, "high_OI": 0.04
    }
    score = confidence
    for v in votes:
        score += weight_map.get(v, 0.02)
    # penalize small RR
    if rr_best < 1.5:
        score *= 0.7
    return min(score, 1.0)

# ---------------- PLOT ----------------
def make_plot(df: pd.DataFrame, symbol: str, action: str, entry, sl, tps):
    """Create candlestick chart with entry/sl/tp lines and return BytesIO buffer."""
    ap = []
    if sl:
        ap.append(mpf.make_addplot([sl] * len(df), linestyle="--"))
    if entry:
        ap.append(mpf.make_addplot([entry] * len(df), linestyle=":"))
    for tp in tps:
        if tp:
            ap.append(mpf.make_addplot([tp] * len(df), linestyle="-."))
    fig, ax = mpf.plot(df.tail(200), type="candle", style="yahoo", addplot=ap,
                       title=f"{symbol}  {action}", returnfig=True)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    try:
        import matplotlib.pyplot as plt
        plt.close(fig)
    except Exception:
        pass
    return buf

# ---------------- MAIN ANALYZE ----------------
def analyze_one(symbol: str):
    try:
        df = fetch_klines_df(symbol, interval="5m", limit=500)
        if df is None or len(df) < 60:
            return None
        df = apply_smc_features(df)
        funding = get_funding(symbol)
        oi = get_open_interest(symbol)
        liqs = get_recent_liqs(symbol)
        action, votes, confidence, notes = detect_smc_setup(df, funding, oi, liqs)
        if action == "WATCH":
            return None

        last_close = float(df["close"].iat[-1])
        last_support = float(df["s_support"].iat[-1])
        last_resistance = float(df["s_resistance"].iat[-1])
        entry, sl, tp1, tp2, tp3, rr1 = compute_entry_sl_tp(last_close, action, last_support, last_resistance)
        rr_best = max(rr1, (tp2 - entry)/(entry - sl) if action=="LONG" and sl!=entry else 0.0, 0.0)
        score = quality_score(votes, confidence, rr_best)

        # Final filter: require both score and rr threshold
        if score < 0.45 or rr_best < 1.8:
            logger.debug("Filtered out %s (score %.2f rr %.2f)", symbol, score, rr_best)
            return None

        # Prepare message
        symbol_info = f"⚡ FUTURES SMC SIGNAL\nSymbol: {symbol}\nAction: {action}\nLast price: {last_close:.6f}\nScore: {int(score*100)}/100  Confidence: {confidence:.2f}\nFunding: {funding:.6f}  OI: {oi:.0f}\nPatterns: {', '.join(votes)}"
        entry_txt = f"\n\nEntry: {entry:.6f}\nStop-Loss: {sl:.6f}\nTP1: {tp1:.6f}  TP2: {tp2:.6f}  TP3: {tp3:.6f}\nNote: idea, not financial advice."
        text = symbol_info + entry_txt

        # plot and send
        buf = make_plot(df, symbol, action, entry, sl, [tp1, tp2, tp3])
        ok = send_telegram(text, photo_buf=buf)
        if ok:
            state["signals"][symbol] = {
                "time": str(datetime.now(timezone.utc)),
                "score": float(score),
                "confidence": float(confidence),
                "action": action,
                "entry": entry,
                "sl": sl,
                "tp1": tp1, "tp2": tp2, "tp3": tp3,
                "votes": votes
            }
            save_state(state)
            logger.info("Signal sent for %s score=%.2f rr=%.2f", symbol, score, rr_best)
        else:
            logger.warning("Failed to send telegram for %s", symbol)
        return {"symbol": symbol, "score": score, "action": action}
    except Exception as e:
        logger.exception("analyze_one error %s: %s", symbol, e)
        return None

# ---------------- MASTER SCAN ----------------
def scan_all():
    symbols = fetch_top_symbols_by_change(limit=TOP_N)
    logger.info("Master scan - symbols: %d", len(symbols))
    results = []
    with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as exe:
        for r in exe.map(analyze_one, symbols):
            if r:
                results.append(r)
    state["last_scan"] = str(datetime.now(timezone.utc))
    save_state(state)
    logger.info("Scan finished. Signals found: %d", len(results))
    return results

# ---------------- FLASK (status + manual trigger + webhook) ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return jsonify({"ok": True, "last_scan": state.get("last_scan"), "signals": len(state.get("signals", {}))})

@app.route("/scan_now", methods=["POST"])
def scan_now():
    Thread(target=scan_all, daemon=True).start()
    return jsonify({"ok": True, "message": "Manual scan started"})

@app.route("/telegram_webhook/<token>", methods=["POST"])
def telegram_webhook(token):
    # minimal verification for testing with Render / webhook
    if token != TELEGRAM_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    # allow manual scan via chat command (optional)
    try:
        payload = request.get_json(force=True)
        text = payload.get("message", {}).get("text", "").strip().lower()
        if text.startswith("/scan"):
            Thread(target=scan_all, daemon=True).start()
            return jsonify({"ok": True, "message": "Scan started"})
    except Exception:
        pass
    return jsonify({"ok": True})

# ---------------- BACKGROUND AUTO LOOP ----------------
def auto_loop():
    while True:
        try:
            scan_all()
        except Exception as e:
            logger.exception("auto_loop scan_all error: %s", e)
        time.sleep(SCAN_INTERVAL_SECONDS)

# ---------------- START ----------------
if __name__ == "__main__":
    # start background thread
    Thread(target=auto_loop, daemon=True).start()
    # start flask
    app.run(host="0.0.0.0", port=PORT)