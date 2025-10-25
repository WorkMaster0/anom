import os
import ccxt.pro as ccxtpro
import pandas as pd
import pandas_ta as ta
import asyncio
import aiohttp
import matplotlib.pyplot as plt
from io import BytesIO
import numpy as np
import telegram

# ==============================
# 🔧 Налаштування
# ==============================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TIMEFRAME = "2h"  # Можна 15m / 1h / 4h / 1d
LIMIT = 200  # Кількість свічок для аналізу

# ==============================
# 🔧 Ініціалізація клієнтів
# ==============================
mexc = ccxtpro.mexc()
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ==============================
# 📊 Логіка SMC (спрощений LuxAlgo)
# ==============================
def analyze_smc(df):
    """
    Аналізує структуру ринку і повертає потенційну зону перевороту
    """
    if len(df) < 100:
        return None

    df['ema'] = ta.ema(df['close'], 50)
    df['swing_high'] = df['high'][(df['high'] > df['high'].shift(1)) & (df['high'] > df['high'].shift(-1))]
    df['swing_low'] = df['low'][(df['low'] < df['low'].shift(1)) & (df['low'] < df['low'].shift(-1))]

    last_price = df['close'].iloc[-1]
    last_ema = df['ema'].iloc[-1]

    # Визначаємо структуру
    structure = "bullish" if last_price > last_ema else "bearish"

    # Визначаємо фібо-зони
    swing_high = df['swing_high'].dropna().iloc[-1]
    swing_low = df['swing_low'].dropna().iloc[-1]
    fib_618 = swing_low + 0.618 * (swing_high - swing_low)
    fib_786 = swing_low + 0.786 * (swing_high - swing_low)
    fib_236 = swing_low + 0.236 * (swing_high - swing_low)
    fib_382 = swing_low + 0.382 * (swing_high - swing_low)

    signal = None
    if structure == "bullish" and fib_618 <= last_price <= fib_786:
        signal = f"🟢 BUY zone reached ({round(last_price, 4)})"
    elif structure == "bearish" and fib_236 <= last_price <= fib_382:
        signal = f"🔴 SELL zone reached ({round(last_price, 4)})"

    return {
        "structure": structure,
        "price": last_price,
        "signal": signal,
        "fib": (fib_236, fib_382, fib_618, fib_786)
    }

# ==============================
# 📈 Побудова графіка
# ==============================
def plot_chart(df, sym, fib):
    plt.figure(figsize=(10, 5))
    plt.plot(df['close'], label="Price", linewidth=1)
    plt.axhline(fib[0], linestyle="--")
    plt.axhline(fib[1], linestyle="--")
    plt.axhline(fib[2], linestyle="--")
    plt.axhline(fib[3], linestyle="--")
    plt.title(f"{sym} - Smart Money Concepts Zones")
    plt.legend()
    buf = BytesIO()
    plt.savefig(buf, format="png")
    buf.seek(0)
    plt.close()
    return buf

# ==============================
# 🧠 Основна логіка моніторингу
# ==============================
async def monitor():
    markets = await mexc.load_markets()
    symbols = [s for s in markets if "/USDT" in s]

    print(f"🔍 Перевіряємо {len(symbols)} пар...")

    for sym in symbols:
        try:
            ohlcv = await mexc.fetch_ohlcv(sym, timeframe=TIMEFRAME, limit=LIMIT)
            df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "vol"])
            analysis = analyze_smc(df)
            if analysis and analysis["signal"]:
                img = plot_chart(df, sym, analysis["fib"])
                msg = (
                    f"📈 <b>{sym}</b>\n"
                    f"💰 Price: {analysis['price']}\n"
                    f"📊 Structure: {analysis['structure']}\n"
                    f"⚠️ Signal: {analysis['signal']}"
                )
                await bot.send_photo(chat_id=CHAT_ID, photo=img, caption=msg, parse_mode="HTML")
                print(f"✅ {sym}: {analysis['signal']}")
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"❌ {sym}: {e}")
            continue

# ==============================
# 🚀 Головний цикл
# ==============================
async def main():
    while True:
        await monitor()
        print("⏳ Очікуємо 1 годину...")
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())