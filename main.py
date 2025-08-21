import os
import logging
import asyncio
from datetime import datetime

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from html import escape
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from fastapi import FastAPI
import uvicorn

# ================== Логування ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ultimaster-bot")

# ================== Конфіг ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не знайдений у змінних оточення")

WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "https://your-app.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 8080))

MIN_VOLUME = 1000
MAX_VOLUME = 5_000_000
MIN_PRICE_CHANGE = 10

# ================== Ініціалізація ==================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
app = FastAPI()

# ================== Jupiter API (token.jup.ag/all) ==================
JUPITER_REFRESH_INTERVAL = 60
last_jup_data = []
last_jup_time = 0
jup_lock = asyncio.Lock()

async def fetch_jupiter_data(force=False):
    """ Отримує дані з Jupiter API (token.jup.ag/all) не частіше ніж раз/хв """
    global last_jup_data, last_jup_time
    now = datetime.now().timestamp()
    async with jup_lock:
        if not force and now - last_jup_time < JUPITER_REFRESH_INTERVAL and last_jup_data:
            return last_jup_data, 0

        url = "https://token.jup.ag/all"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=20) as resp:
                    if resp.status == 200:
                        tokens = await resp.json()
                        coins = []
                        for token in tokens:
                            coins.append({
                                "id": token.get("address", ""),
                                "name": token.get("name", "Unknown"),
                                "symbol": token.get("symbol", "UNK"),
                                "market_cap": token.get("marketCap", 0) or 0,
                                "total_volume": token.get("dailyVolume", 0) or 0,
                                "current_price": token.get("price", 0) or 0,
                                "price_change_percentage_24h": token.get("priceChange24h", 0) or 0,
                                "chainId": "solana",
                                "pairAddress": ""
                            })
                        last_jup_data = coins
                        last_jup_time = now
                        return coins, 0
                    else:
                        logger.error(f"Jupiter API error {resp.status}: {await resp.text()}")
                        return last_jup_data, 60
        except Exception as e:
            logger.error(f"Jupiter request error: {e}")
            return last_jup_data, 60

# ================== Аналіз монет ==================
async def analyze_coins(chat_id: int = None):
    coins, wait_time = await fetch_jupiter_data()
    anomalies = []
    if not coins:
        if chat_id:
            await bot.send_message(chat_id, f"⚠️ Помилка отримання даних з Jupiter API. Спробую ще раз через {wait_time} сек.")
        return anomalies

    for coin in coins:
        market_cap = coin.get("market_cap", 0) or 0
        volume = coin.get("total_volume", 0) or 0
        price_change = coin.get("price_change_percentage_24h", 0) or 0

        if (volume > MIN_VOLUME and volume < MAX_VOLUME and
            price_change >= MIN_PRICE_CHANGE):
            anomalies.append(coin)

    return anomalies

# ================== Моніторинг ==================
async def monitoring_loop(chat_id: int):
    logger.info(f"Starting monitoring task for chat_id: {chat_id}")
    while True:
        try:
            anomalies = await analyze_coins(chat_id)
            if anomalies:
                lines = ["🚨 Знайдено аномальні токени:"]
                for coin in anomalies:
                    lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'])}) — +{coin.get('price_change_percentage_24h', 0):.1f}%")
                await bot.send_message(chat_id, "\n".join(lines))
        except Exception as e:
            logger.error(f"Monitoring error: {e}")
        await asyncio.sleep(60)

# ================== Хендлери ==================
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("👋 Привіт! Я бот для моніторингу токенів з Jupiter.\n\nДоступні команди:\n/topvol\n/topgainers")

@dp.message(Command("monitor"))
async def monitor_cmd(message: types.Message):
    chat_id = message.chat.id
    asyncio.create_task(monitoring_loop(chat_id))
    await message.answer("✅ Моніторинг запущено!")

@dp.message(Command("topvol"))
async def topvol_cmd(message: types.Message):
    coins, wait_time = await fetch_jupiter_data()
    if not coins:
        await message.answer(f"⚠️ Немає даних з Jupiter API. Спробую ще раз через {wait_time} секунд.")
        return
    filtered = [c for c in coins if MIN_VOLUME < c.get("total_volume", 0) < MAX_VOLUME]
    top_coins = sorted(filtered, key=lambda x: x.get("total_volume", 0), reverse=True)[:20]
    if top_coins:
        lines = ["💹 Топ токенів за обсягом (Jupiter):"]
        for coin in top_coins:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'])}) — ${coin.get('total_volume', 0):,.0f}")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")

@dp.message(Command("topgainers"))
async def topgainers_cmd(message: types.Message):
    coins, wait_time = await fetch_jupiter_data()
    if not coins:
        await message.answer(f"⚠️ Немає даних з Jupiter API. Спробую ще раз через {wait_time} секунд.")
        return
    filtered = [c for c in coins if MIN_VOLUME < c.get("total_volume", 0) < MAX_VOLUME]
    top_coins = sorted(filtered, key=lambda x: x.get("price_change_percentage_24h", 0), reverse=True)[:20]
    if top_coins:
        lines = ["🚀 Топ токенів за ростом (Jupiter):"]
        for coin in top_coins:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'])}) — +{coin.get('price_change_percentage_24h', 0):.1f}%")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")

# ================== Webhook ==================
@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

@app.post(WEBHOOK_PATH)
async def webhook_handler(update: dict):
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "ok", "bot": "Ultimaster"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
