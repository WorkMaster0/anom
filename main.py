import os
import time
import threading
import numpy as np
import pandas as pd
import requests
from binance.client import Client
from ta.trend import EMAIndicator, MACD, ADXIndicator
from ta.momentum import RSIIndicator
from dotenv import load_dotenv
from flask import Flask, jsonify

# ==========================
#   ENV
# ==========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

# ==========================
#   Flask app
# ==========================
app = Flask(__name__)

# ==========================
#   Telegram
# ==========================

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print("❌ Помилка Telegram:", e)

# ==========================
#   Binance дані
# ==========================

def fetch_klines(symbol="BTCUSDT", interval="1h", limit=200):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume", "c1", "c2", "c3", "c4", "c5", "c6"
    ])
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df

def get_top_symbols(limit=10):
    tickers = client.get_ticker()
    df = pd.DataFrame(tickers)
    df["quoteVolume"] = df["quoteVolume"].astype(float)
    df = df[df["symbol"].str.endswith("USDT")]
    df = df.sort_values("quoteVolume", ascending=False).head(limit)
    return df["symbol"].tolist()

# ==========================
#   Індикатори
# ==========================

def add_indicators(df):
    df["ema_fast"] = EMAIndicator(df["close"], window=12).ema_indicator()
    df["ema_slow"] = EMAIndicator(df["close"], window=26).ema_indicator()
    df["ema_cross_up"] = (df["ema_fast"] > df["ema_slow"]) & (df["ema_fast"].shift() <= df["ema_slow"].shift())
    df["ema_cross_down"] = (df["ema_fast"] < df["ema_slow"]) & (df["ema_fast"].shift() >= df["ema_slow"].shift())

    rsi = RSIIndicator(df["close"], window=14).rsi()
    df["rsi_long"] = rsi < 30
    df["rsi_short"] = rsi > 70

    macd = MACD(df["close"])
    df["macd_long"] = macd.macd_diff() > 0
    df["macd_short"] = macd.macd_diff() < 0

    adx = ADXIndicator(df["high"], df["low"], df["close"], window=14).adx()
    df["adx"] = adx

    # Risk/Reward заглушка
    df["risk_reward"] = np.random.uniform(2, 5, len(df))

    # Orderflow заглушки
    df["fake_breakout_long"] = False
    df["fake_breakout_short"] = False
    df["funding_long"] = True
    df["funding_short"] = False
    df["oi_long"] = True
    df["oi_short"] = False

    return df

# ==========================
#   Команди
# ==========================

class StrategyTeam:
    def analyze(self, df) -> dict:
        raise NotImplementedError

class TrendTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        if last["ema_cross_up"] and last["adx"] > 25:
            return {"signal": "LONG", "confidence": 0.7, "patterns": ["ema_cross_up", "adx_strong"]}
        elif last["ema_cross_down"] and last["adx"] > 25:
            return {"signal": "SHORT", "confidence": 0.7, "patterns": ["ema_cross_down", "adx_strong"]}
        return {"signal": "WATCH", "confidence": 0.3, "patterns": []}

class MomentumTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        if last["rsi_long"] and last["macd_long"]:
            return {"signal": "LONG", "confidence": 0.6, "patterns": ["rsi_oversold", "macd_long"]}
        elif last["rsi_short"] and last["macd_short"]:
            return {"signal": "SHORT", "confidence": 0.6, "patterns": ["rsi_overbought", "macd_short"]}
        return {"signal": "WATCH", "confidence": 0.3, "patterns": []}

class PatternTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        if last["fake_breakout_long"]:
            return {"signal": "LONG", "confidence": 0.65, "patterns": ["fake_breakout_long"]}
        elif last["fake_breakout_short"]:
            return {"signal": "SHORT", "confidence": 0.65, "patterns": ["fake_breakout_short"]}
        return {"signal": "WATCH", "confidence": 0.3, "patterns": []}

class OrderflowTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        if last["funding_long"] and last["oi_long"]:
            return {"signal": "LONG", "confidence": 0.55, "patterns": ["funding_long", "oi_long"]}
        elif last["funding_short"] and last["oi_short"]:
            return {"signal": "SHORT", "confidence": 0.55, "patterns": ["funding_short", "oi_short"]}
        return {"signal": "WATCH", "confidence": 0.3, "patterns": []}

class RiskTeam(StrategyTeam):
    def analyze(self, df):
        last = df.iloc[-1]
        if last["risk_reward"] < 3:
            return {"signal": "WATCH", "confidence": 0.1, "patterns": ["bad_rr"]}
        return {"signal": "PASS", "confidence": 1.0, "patterns": ["ok_rr"]}

# ==========================
#   Координатор
# ==========================

class Coordinator:
    def __init__(self, teams):
        self.teams = teams

    def decide(self, df):
        results = [t.analyze(df) for t in self.teams]
        signals = [r for r in results if r["signal"] not in ["WATCH", "PASS"]]

        if not signals:
            return {"final_signal": "WATCH", "confidence": 0.0, "votes": results}

        long_votes = [r for r in signals if r["signal"] == "LONG"]
        short_votes = [r for r in signals if r["signal"] == "SHORT"]

        if len(long_votes) >= 2 and len(long_votes) > len(short_votes):
            conf = np.mean([r["confidence"] for r in long_votes])
            return {"final_signal": "LONG", "confidence": conf, "votes": results}
        elif len(short_votes) >= 2 and len(short_votes) > len(long_votes):
            conf = np.mean([r["confidence"] for r in short_votes])
            return {"final_signal": "SHORT", "confidence": conf, "votes": results}

        return {"final_signal": "WATCH", "confidence": 0.0, "votes": results}

# ==========================
#   Аналіз + Telegram
# ==========================

def analyze_and_alert(symbol="BTCUSDT", interval="1h"):
    df = fetch_klines(symbol, interval)
    df = add_indicators(df)

    teams = [TrendTeam(), MomentumTeam(), PatternTeam(), OrderflowTeam(), RiskTeam()]
    coord = Coordinator(teams)
    decision = coord.decide(df)

    text = f"📊 <b>Аналіз {symbol} ({interval})</b>\n\n"
    for r in decision["votes"]:
        text += f"- {r['signal']} | conf={r['confidence']:.2f} | {', '.join(r['patterns'])}\n"

    text += f"\n✅ <b>Фінальне рішення:</b> {decision['final_signal']} (довіра {decision['confidence']:.2f})"

    print(text)
    send_telegram_message(text)

    return decision

def run_periodic_analysis():
    while True:
        symbols = get_top_symbols(10)
        for sym in symbols:
            analyze_and_alert(sym, "1h")
            time.sleep(2)  # щоб не навантажувати API
        time.sleep(300)  # повтор через 5 хвилин

# ==========================
#   Flask endpoints
# ==========================

@app.route("/")
def home():
    return "✅ Crypto Analyzer Bot працює!"

@app.route("/analyze")
def manual_analyze():
    symbols = get_top_symbols(5)
    results = {}
    for sym in symbols:
        decision = analyze_and_alert(sym, "1h")
        results[sym] = decision
    return jsonify(results)

# ==========================
#   Запуск
# ==========================

if __name__ == "__main__":
    # Фоновий потік для періодичного аналізу
    t = threading.Thread(target=run_periodic_analysis, daemon=True)
    t.start()

    # Flask сервер
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)