import requests
import telebot
from flask import Flask, request
from datetime import datetime
import threading
import time

# -------------------------
# Налаштування
# -------------------------
API_KEY_TELEGRAM = "8051222216:AAFORHEn1IjWllQyPp8W_1OY3gVxcBNVvZI"
CHAT_ID = "6053907025"
TIMEFRAMES = ["5m", "15m", "1h", "4h"]
N_CANDLES = 30
FAST_EMA = 10
SLOW_EMA = 30

WEBHOOK_HOST = "https://troovy-detective-bot-1-4on4.onrender.com"
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH

bot = telebot.TeleBot(API_KEY_TELEGRAM)
app = Flask(__name__)

last_signals = {}   # останні сигнали по монетах
last_status = {}    # останній стан по монетах

# -------------------------
# Топ монет по волатильності (% за 24h), мінімальний обсяг 1 млн USDT
# -------------------------
def get_top_symbols(min_volume=1_000_000):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    data = requests.get(url, timeout=10).json()
    usdt_pairs = [x for x in data if x["symbol"].endswith("USDT")]
    filtered_pairs = [x for x in usdt_pairs if float(x["quoteVolume"]) >= min_volume]
    sorted_pairs = sorted(filtered_pairs, key=lambda x: abs(float(x["priceChangePercent"])), reverse=True)
    return [x["symbol"] for x in sorted_pairs]

# -------------------------
# Історичні дані
# -------------------------
def get_historical_data(symbol, interval, limit=100):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    data = response.json()
    ohlc = []
    for d in data:
        timestamp = datetime.fromtimestamp(d[0] / 1000)
        ohlc.append({
            "time": timestamp,
            "open": float(d[1]),
            "high": float(d[2]),
            "low": float(d[3]),
            "close": float(d[4]),
            "volume": float(d[5])
        })
    return ohlc

# -------------------------
# EMA
# -------------------------
def calculate_ema(closes, period):
    ema = closes[0]
    k = 2 / (period + 1)
    for price in closes[1:]:
        ema = price * k + ema * (1 - k)
    return ema

# -------------------------
# Аналіз сигналів (EMA + тренд + волатильність)
# -------------------------
def analyze_phase(ohlc):
    closes = [c["close"] for c in ohlc][-N_CANDLES:]
    highs = [c["high"] for c in ohlc][-N_CANDLES:]
    lows = [c["low"] for c in ohlc][-N_CANDLES:]
    volumes = [c["volume"] for c in ohlc][-N_CANDLES:]

    last_close = closes[-1]
    volatility = max(highs) - min(lows)

    trend_up = closes[-2] < closes[-1]
    trend_down = closes[-2] > closes[-1]

    fast_ema = calculate_ema(closes[-FAST_EMA:], FAST_EMA)
    slow_ema = calculate_ema(closes[-SLOW_EMA:], SLOW_EMA)

    ema_confirm = None
    if fast_ema > slow_ema:
        ema_confirm = "BUY"
    elif fast_ema < slow_ema:
        ema_confirm = "SELL"

    if trend_up and ema_confirm == "BUY":
        return "BUY", volatility, True, ema_confirm, trend_up
    elif trend_down and ema_confirm == "SELL":
        return "SELL", volatility, True, ema_confirm, trend_down
    else:
        return "HOLD", volatility, False, ema_confirm, None

# -------------------------
# Відправка сигналу
# -------------------------
def send_signal(symbol, signal, price, max_volatility, confidence):
    global last_signals
    if signal == "HOLD":
        return

    total_tfs = len(TIMEFRAMES)
    last_signals[symbol] = {
        "signal": signal,
        "price": price,
        "tp": round(price + max_volatility * 0.5 if signal == "BUY" else price - max_volatility * 0.5, 4),
        "sl": round(price - max_volatility * 0.3 if signal == "BUY" else price + max_volatility * 0.3, 4),
        "confidence": confidence,
        "time": datetime.now()
    }

    note = "✅ Підтверджено всіма ТФ" if confidence == total_tfs else f"⚠️ Лише {confidence}/{total_tfs} ТФ співпали"
    msg = (
        f"📢 {symbol}\nСигнал: {signal}\n💰 Ціна: {price}\n"
        f"🎯 TP: {last_signals[symbol]['tp']}\n🛑 SL: {last_signals[symbol]['sl']}\n{note}"
    )
    bot.send_message(CHAT_ID, msg)

    with open("signals.log", "a") as f:
        f.write(
            f"{datetime.now()} | {symbol} | {signal} | {price} "
            f"| TP: {last_signals[symbol]['tp']} | SL: {last_signals[symbol]['sl']} | {note}\n"
        )

# -------------------------
# Перевірка ринку
# -------------------------
def check_market():
    global last_status
    while True:
        try:
            symbols = get_top_symbols()
            for symbol in symbols:
                signals, volatilities, last_prices, ema_confirms, trends = [], [], [], [], []
                for tf in TIMEFRAMES:
                    ohlc = get_historical_data(symbol, tf)
                    signal, volatility, ema_ok, ema_signal, trend = analyze_phase(ohlc)
                    signals.append(signal)
                    volatilities.append(volatility)
                    last_prices.append(ohlc[-1]["close"])
                    ema_confirms.append(ema_ok)
                    trends.append((ema_signal, trend))

                buy_count = signals.count("BUY")
                sell_count = signals.count("SELL")
                total_tfs = len(TIMEFRAMES)

                if len(set(signals)) == 1 and signals[0] != "HOLD":
                    send_signal(symbol, signals[0], last_prices[-1], max(volatilities), total_tfs)
                elif buy_count >= total_tfs - 1:
                    send_signal(symbol, "BUY", last_prices[-1], max(volatilities), buy_count)
                elif sell_count >= total_tfs - 1:
                    send_signal(symbol, "SELL", last_prices[-1], max(volatilities), sell_count)

                last_status[symbol] = {
                    "signals": signals,
                    "ema_confirms": ema_confirms,
                    "trends": trends,
                    "timeframes": TIMEFRAMES,
                    "last_prices": last_prices,
                    "volatilities": volatilities
                }

                time.sleep(0.5)

        except Exception as e:
            print(f"{datetime.now()} - Помилка: {e}")
            with open("errors.log", "a") as f:
                f.write(f"{datetime.now()} - {e}\n")
        time.sleep(10)

# -------------------------
# Вебхук Telegram
# -------------------------
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    global last_status
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])

    message_obj = update.message or update.edited_message
    if not message_obj:
        return "!", 200

    text = message_obj.text.strip()

    if text.startswith("/status"):
        args = text.split()
        if len(args) == 2:
            symbol = args[1].upper()
            if symbol in last_status:
                s = last_status[symbol]
                out = f"📊 {symbol}:\n"
                buy_count = sell_count = 0
                for i, tf in enumerate(s["timeframes"]):
                    sig = s["signals"][i]
                    ema_signal = s["trends"][i][0]
                    trend = s["trends"][i][1]
                    price = s["last_prices"][i]
                    vol = s["volatilities"][i]
                    out += f"{tf}: {sig}, EMA {ema_signal}, Тренд {'UP' if trend else 'DOWN' if trend == False else '—'}, Ціна {price}, Волатильність {vol:.2f}\n"
                    if sig == "BUY": buy_count += 1
                    elif sig == "SELL": sell_count += 1
                total = len(s["timeframes"])
                out += f"\n✅ BUY: {buy_count}/{total}\n❌ SELL: {sell_count}/{total}"
                bot.send_message(message_obj.chat.id, out)
            else:
                bot.send_message(message_obj.chat.id, f"❌ Немає даних для {symbol}")
        else:
            bot.send_message(message_obj.chat.id, "Використання: /status SYMBOL")

    elif text.startswith("/top"):
        symbols = get_top_symbols()[:10]
        msg = "🔥 Топ-10 монет за добовим рухом %:\n" + "\n".join(symbols)
        bot.send_message(message_obj.chat.id, msg)

    elif text.startswith("/last"):
        if not last_signals:
            bot.send_message(message_obj.chat.id, "❌ Немає надісланих сигналів")
        else:
            msg = "📝 Останні сигнали:\n"
            total_tfs = len(TIMEFRAMES)
            for sym, info in last_signals.items():
                note = "✅ Підтверджено всіма ТФ" if info["confidence"] == total_tfs else f"⚠️ Лише {info['confidence']}/{total_tfs} ТФ"
                msg += f"{sym}: {info['signal']} | Ціна {info['price']} | TP {info['tp']} | SL {info['sl']} | {note}\n"
            bot.send_message(message_obj.chat.id, msg)

    return "!", 200

# -------------------------
# Встановлення Webhook
# -------------------------
def setup_webhook():
    url = f"https://api.telegram.org/bot{API_KEY_TELEGRAM}/setWebhook"
    response = requests.post(url, data={"url": WEBHOOK_URL})
    print("Webhook setup:", response.json())

# -------------------------
# Запуск
# -------------------------
if __name__ == "__main__":
    setup_webhook()
    threading.Thread(target=check_market, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)