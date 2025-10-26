import os
import io
import json
import time
import logging
import requests
import pandas as pd
import mplfinance as mpf
from flask import Flask, request, jsonify
from datetime import datetime
from threading import Thread
import matplotlib.pyplot as plt

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("smc-webhook-bot")

# ---------------- CONFIG ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
PORT = int(os.getenv("PORT", "10000"))
TF = "2h"
ZONE_MARGIN = 0.01  # ±1% навколо зони розвороту
DATA_DIR = "/tmp/data"

os.makedirs(DATA_DIR, exist_ok=True)

# ---------------- FLASK ----------------
app = Flask(__name__)

# ---------------- GLOBAL STATE ----------------
potential_tokens = []

# ---------------- TELEGRAM ----------------
def send_telegram(text: str, photo=None):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        if photo:
            files = {'photo': ('signal.png', photo, 'image/png')}
            data = {'chat_id': CHAT_ID, 'caption': text, 'parse_mode': 'MarkdownV2'}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto", data=data, files=files, timeout=10)
        else:
            payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "MarkdownV2"}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        logger.exception("send_telegram error: %s", e)

# ---------------- FETCH MEXC ----------------
def get_usdt_pairs():
    try:
        r = requests.get("https://www.mexc.com/open/api/v2/market/symbols")
        data = r.json().get("data", [])
        symbols = [d["symbol"] for d in data if d["quoteCurrency"] == "USDT"]
        return symbols
    except Exception as e:
        logger.exception("Failed to fetch symbols: %s", e)
        return ["BTCUSDT", "ETHUSDT", "BNBUSDT"]

# ---------------- MOCK KLINES (імітатор SMC) ----------------
def fetch_klines(symbol, tf=TF, limit=200):
    dates = pd.date_range(end=datetime.now(), periods=limit, freq='2H')
    df = pd.DataFrame(index=dates)
    df["open"] = 100 + pd.np.random.randn(limit).cumsum()
    df["high"] = df["open"] + pd.np.random.rand(limit)*2
    df["low"] = df["open"] - pd.np.random.rand(limit)*2
    df["close"] = df["open"] + pd.np.random.randn(limit)
    df["volume"] = pd.np.random.rand(limit)*100
    return df

# ---------------- REVERSAL ZONE ----------------
def get_reversal_zone(df):
    zone_low = df["low"].iloc[-20:].min()
    zone_high = df["high"].iloc[-20:].max()
    return zone_low, zone_high

# ---------------- PLOT ----------------
def plot_zone(symbol, df, zone_low, zone_high):
    addplots = [
        mpf.make_addplot([zone_low]*len(df), color='red', linestyle='--'),
        mpf.make_addplot([zone_high]*len(df), color='green', linestyle='--')
    ]
    fig, ax = mpf.plot(df.tail(100), type='candle', style='yahoo', addplot=addplots, returnfig=True)
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    plt.close(fig)
    return buf

# ---------------- SCAN ----------------
def scan_all_pairs_and_store():
    global potential_tokens
    potential_tokens = []

    symbols = get_usdt_pairs()
    logger.info(f"Starting full scan for {len(symbols)} USDT pairs TF={TF}")

    for symbol in symbols:
        df = fetch_klines(symbol, TF)
        zone_low, zone_high = get_reversal_zone(df)
        price = df["close"].iloc[-1]

        logger.info(f"[DEBUG] {symbol}: price={price:.4f}, zone_low={zone_low:.4f}, zone_high={zone_high:.4f}")

        if zone_low*(1-ZONE_MARGIN) <= price <= zone_high*(1+ZONE_MARGIN):
            potential_tokens.append({
                "symbol": symbol,
                "price": price,
                "zone": (zone_low, zone_high)
            })
            msg = f"⚡ {symbol} is in reversal zone!\nPrice: {price:.4f}\nZone: {zone_low:.4f}-{zone_high:.4f}"
            photo = plot_zone(symbol, df, zone_low, zone_high)
            send_telegram(msg, photo=photo)

# ---------------- TELEGRAM WEBHOOK ----------------
@app.route("/telegram_webhook/<token>", methods=["POST"])
def telegram_webhook(token):
    if token != TELEGRAM_TOKEN:
        return jsonify({"ok": False, "error": "invalid token"}), 403
    update = request.get_json(force=True) or {}
    text = update.get("message", {}).get("text", "").lower().strip()

    if text.startswith("/scan"):
        Thread(target=scan_all_pairs_and_store, daemon=True).start()
        send_telegram("⚡ Full scan started (2h TF).")

    if text.startswith("/debug"):
        msg = "Top tokens near reversal zone:\n"
        for t in potential_tokens[-10:]:
            msg += f"{t['symbol']} @ {t['price']:.4f} (zone {t['zone'][0]:.4f}-{t['zone'][1]:.4f})\n"
        send_telegram(msg)

    if text.startswith("/prebreak"):
        msg = "Tokens approaching reversal zone:\n"
        for t in potential_tokens[-10:]:
            msg += f"{t['symbol']} @ {t['price']:.4f} (zone {t['zone'][0]:.4f}-{t['zone'][1]:.4f})\n"
        send_telegram(msg)

    return jsonify({"ok": True})

# ---------------- RUN ----------------
if __name__ == "__main__":
    logger.info(f"Starting SMC webhook Render-ready bot (TF={TF})")

    # встановлюємо вебхук автоматично
    try:
        RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
        if RENDER_URL and TELEGRAM_TOKEN:
            webhook_url = f"{RENDER_URL}/telegram_webhook/{TELEGRAM_TOKEN}"
            r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}")
            logger.info(f"Webhook set to {webhook_url}, response: {r.json()}")
    except Exception as e:
        logger.exception("Failed to set webhook: %s", e)

    app.run(host="0.0.0.0", port=PORT)