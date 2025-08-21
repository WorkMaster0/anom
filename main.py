import os
import logging
from fastapi import FastAPI, Request
from contextlib import asynccontextmanager
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from html import escape
import asyncio
from datetime import datetime
from cachetools import TTLCache
import aiohttp

# Налаштування логування
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ultimaster-bot")

# Налаштування з config.py
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://anom-1.onrender.com").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", f"{APP_BASE_URL}/webhook").strip()
MIN_CAP = int(os.getenv("MIN_CAP", "100000000"))  # $100M
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "10000"))  # $10K
MAX_VOLUME = int(os.getenv("MAX_VOLUME", "100000000"))  # $100M
MIN_PRICE_CHANGE = float(os.getenv("MIN_PRICE_CHANGE", "20"))  # 20%
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "20"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задано")

# Ініціалізація
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

# Кеш і глобальні змінні
alert_cache = TTLCache(maxsize=2000, ttl=86400)
active_monitoring = {}
latest_anomalies = []
last_fetch_time = 0
last_coins_data = []
rate_limit_exceeded = False
last_rate_limit_time = 0

# CoinGecko API з пагінацією
async def fetch_coingecko_data():
    global last_fetch_time, last_coins_data, rate_limit_exceeded, last_rate_limit_time
    now = datetime.now().timestamp()
    if rate_limit_exceeded and (now - last_rate_limit_time) < 300:
        logger.info("Rate limit exceeded recently, skipping fetch")
        return last_coins_data
    if now - last_fetch_time < 900 and last_coins_data:  # Збільшено кеш до 15 хвилин
        logger.info("Returning cached CoinGecko data")
        return last_coins_data

    logger.info("Fetching new data from CoinGecko API")
    url = "https://api.coingecko.com/api/v3/coins/markets"
    all_coins = []
    for page in range(1, 3):  # Отримуємо 2 сторінки (до 500 токенів)
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": 250,
            "page": page,
            "sparkline": "false"
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=15) as resp:
                    logger.info(f"CoinGecko API response status (page {page}): {resp.status}")
                    if resp.status == 200:
                        data = await resp.json()
                        logger.info(f"Received {len(data)} coins from CoinGecko (page {page})")
                        for coin in data:
                            all_coins.append({
                                "id": coin.get("id", ""),
                                "name": coin.get("name", ""),
                                "symbol": coin.get("symbol", "").upper(),
                                "market_cap": coin.get("market_cap", 0) or 0,
                                "total_volume": coin.get("total_volume", 0) or 0,
                                "current_price": coin.get("current_price", 0) or 0,
                                "price_change_percentage_24h": coin.get("price_change_percentage_24h", 0) or 0
                            })
                    elif resp.status == 429:
                        logger.error(f"CoinGecko status 429. Response: {await resp.text()}")
                        rate_limit_exceeded = True
                        last_rate_limit_time = now
                        return last_coins_data
                    else:
                        logger.error(f"CoinGecko status {resp.status}. Response: {await resp.text()}")
                await asyncio.sleep(2)  # Затримка 2 секунди між сторінками
        except Exception as e:
            logger.error(f"CoinGecko request error (page {page}): {e}")
    last_coins_data = all_coins
    last_fetch_time = now
    rate_limit_exceeded = False
    logger.info(f"Processed {len(all_coins)} coins")
    return all_coins

async def analyze_coins(chat_id: int = None):
    coins = await fetch_coingecko_data()
    logger.info(f"Analyzing {len(coins)} coins")
    anomalies = []
    if not coins and rate_limit_exceeded:
        if chat_id:
            await bot.send_message(chat_id, "⚠️ Перевищено ліміт запитів до CoinGecko API. Зачекайте 5 хвилин або оновіть до платного плану: https://www.coingecko.com/en/api/pricing")
        return anomalies
    for coin in coins:
        market_cap = coin.get('market_cap', 0) or 0
        volume = coin.get('total_volume', 0) or 0
        price_change = coin.get('price_change_percentage_24h', 0) or 0
        volume_to_cap_ratio = volume / market_cap if market_cap > 0 else 0

        if (market_cap > 0 and market_cap < MIN_CAP and
            volume > MIN_VOLUME and volume < MAX_VOLUME and
            price_change > MIN_PRICE_CHANGE and
            volume_to_cap_ratio > 0.1):
            anomalies.append(coin)
            logger.info(f"Found anomaly: {coin['name']} (Market Cap: {market_cap:,}, Volume: {volume:,}, Price Change: {price_change:.1f}%, Volume/Cap: {volume_to_cap_ratio:.2f})")
    logger.info(f"Found {len(anomalies)} anomalies")
    return anomalies

async def send_alert(chat_id: int, coin: dict):
    coin_id = coin.get("id", "")
    if coin_id in alert_cache:
        return

    name = escape(coin.get("name", ""))
    sym = escape(coin.get("symbol", "").upper())
    price = coin.get("current_price", 0) or 0
    mcap = coin.get("market_cap", 0) or 0
    change = coin.get("price_change_percentage_24h", 0) or 0
    vol = coin.get("total_volume", 0) or 0

    msg = (
        f"🚨 <b>{name} ({sym})</b>\n"
        f"💰 Ціна: <code>${price:.8f}</code>\n"
        f"📊 Капіталізація: <code>${mcap:,}</code>\n"
        f"📈 Зміна 24h: <b>{change:.1f}%</b>\n"
        f"💹 Обсяг: <code>${vol:,}</code>\n"
        f"🔗 <a href='https://www.coingecko.com/en/coins/{coin_id}'>CoinGecko</a>"
    )

    await bot.send_message(chat_id, msg, parse_mode="HTML", disable_web_page_preview=True)
    alert_cache[coin_id] = 1
    latest_anomalies.insert(0, coin)
    del latest_anomalies[200:]
    logger.info(f"Sent alert for {name} to chat_id: {chat_id}")

async def monitoring_task(chat_id: int):
    logger.info(f"Starting monitoring task for chat_id: {chat_id}")
    while active_monitoring.get(chat_id):
        try:
            anomalies = await analyze_coins(chat_id)
            if not anomalies:
                logger.info("No anomalies found or API error occurred")
                if not rate_limit_exceeded:
                    await bot.send_message(chat_id, "ℹ️ Немає аномальних токенів або помилка API. Спробую ще раз через 2 хвилини.")
            else:
                sent = 0
                for coin in anomalies:
                    if coin["id"] not in alert_cache and sent < MAX_ALERTS_PER_CYCLE:
                        await send_alert(chat_id, coin)
                        sent += 1
        except Exception as e:
            logger.error(f"monitoring_task error: {e}")
            await bot.send_message(chat_id, f"⚠️ Помилка моніторингу: {str(e)}. Спробую ще раз.")
        await asyncio.sleep(CHECK_INTERVAL)

async def clear_cache_task():
    while True:
        logger.info("Clearing alert cache")
        alert_cache.clear()
        await asyncio.sleep(86400)  # Раз на 24 години

# Команди
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    logger.info(f"Received /start command from chat_id: {message.chat.id}")
    chat_id = message.chat.id
    if active_monitoring.get(chat_id):
        await message.answer("🔍 Моніторинг вже запущений!")
        return
    active_monitoring[chat_id] = True
    asyncio.create_task(monitoring_task(chat_id))
    await message.answer(
        "🚀 Моніторинг запущено!\n"
        "Команди:\n"
        "• /stop — зупинити\n"
        "• /status — статус\n"
        "• /latest — останні аномалії\n"
        "• /topvol — топ дрібних капів за обсягом\n"
        "• /topgainers — топ дрібних капів за ростом\n"
        "• /setcriteria — налаштувати критерії\n"
        "• /help — ця підказка"
    )

@dp.message(Command("stop"))
async def stop_cmd(message: types.Message):
    logger.info(f"Received /stop command from chat_id: {message.chat.id}")
    chat_id = message.chat.id
    if active_monitoring.pop(chat_id, None):
        await message.answer("⏹️ Моніторинг зупинено!")
    else:
        await message.answer("ℹ Моніторинг не активний")

@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    logger.info(f"Received /status command from chat_id: {message.chat.id}")
    chat_id = message.chat.id
    stat = "активний" if active_monitoring.get(chat_id) else "неактивний"
    await message.answer(f"📊 Статус: {stat}\n🔍 У кеші сповіщень: {len(alert_cache)}")

@dp.message(Command("latest"))
async def latest_cmd(message: types.Message):
    logger.info(f"Received /latest command from chat_id: {message.chat.id}")
    if latest_anomalies:
        lines = ["🔍 Останні аномальні токени:"]
        for coin in latest_anomalies[:20]:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'])})")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ Аномалій ще немає")

@dp.message(Command("topvol"))
async def topvol_cmd(message: types.Message):
    logger.info(f"Received /topvol command from chat_id: {message.chat.id}")
    coins = await fetch_coingecko_data()
    if not coins and rate_limit_exceeded:
        await message.answer("⚠️ Перевищено ліміт запитів до CoinGecko API. Зачекайте 5 хвилин або оновіть до платного плану: https://www.coingecko.com/en/api/pricing")
        return
    filtered = [c for c in coins if c.get("market_cap", 0) > 0 and c.get("market_cap", 0) < MIN_CAP]
    top_coins = sorted(filtered, key=lambda x: x.get("total_volume", 0), reverse=True)[:20]
    if top_coins:
        lines = ["💹 Топ дрібних капів за обсягом:"]
        for coin in top_coins:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'])}) — ${coin.get('total_volume', 0):,.0f}")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")

@dp.message(Command("topgainers"))
async def topgainers_cmd(message: types.Message):
    logger.info(f"Received /topgainers command from chat_id: {message.chat.id}")
    coins = await fetch_coingecko_data()
    if not coins and rate_limit_exceeded:
        await message.answer("⚠️ Перевищено ліміт запитів до CoinGecko API. Зачекайте 5 хвилин або оновіть до платного плану: https://www.coingecko.com/en/api/pricing")
        return
    filtered = [c for c in coins if c.get("market_cap", 0) > 0 and c.get("market_cap", 0) < MIN_CAP]
    top_coins = sorted(filtered, key=lambda x: x.get("price_change_percentage_24h", 0), reverse=True)[:20]
    if top_coins:
        lines = ["🚀 Топ дрібних капів за ростом:"]
        for coin in top_coins:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'])}) — +{coin.get('price_change_percentage_24h', 0):.1f}%")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")

@dp.message(Command("setcriteria"))
async def set_criteria_cmd(message: types.Message):
    logger.info(f"Received /setcriteria command from chat_id: {message.chat.id}")
    try:
        args = message.text.split()[1:]
        if len(args) != 3:
            await message.answer("ℹ️ Використовуйте: /setcriteria MIN_CAP MIN_VOLUME MIN_PRICE_CHANGE\nПриклад: /setcriteria 50000000 5000 10")
            return
        global MIN_CAP, MIN_VOLUME, MIN_PRICE_CHANGE
        MIN_CAP = int(args[0])
        MIN_VOLUME = int(args[1])
        MIN_PRICE_CHANGE = float(args[2])
        await message.answer(f"✅ Критерії оновлено:\nMIN_CAP: ${MIN_CAP:,}\nMIN_VOLUME: ${MIN_VOLUME:,}\nMIN_PRICE_CHANGE: {MIN_PRICE_CHANGE}%")
    except Exception as e:
        await message.answer(f"⚠️ Помилка: {str(e)}")

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    logger.info(f"Received /help command from chat_id: {message.chat.id}")
    return await start_cmd(message)

# Webhook і Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        logger.info(f"Attempting to set webhook to {WEBHOOK_URL}")
        await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        logger.info(f"Webhook successfully set to {WEBHOOK_URL}")
        webhook_info = await bot.get_webhook_info()
        logger.info(f"Webhook info: {webhook_info}")
        asyncio.create_task(clear_cache_task())  # Запускаємо очищення кешу
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        logger.info("Falling back to polling mode")
        asyncio.create_task(dp.start_polling(bot))
    
    yield
    
    try:
        await bot.delete_webhook()
        logger.info("Webhook deleted")
    except Exception as e:
        logger.error(f"Failed to delete webhook: {e}")

app.lifespan = lifespan

@app.get("/")
async def root():
    return {"message": "Ultimaster Bot is running!"}

@app.head("/")
async def root_head():
    return {"message": "Ultimaster Bot is running!"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
        logger.info(f"Received webhook update: {update}")
        telegram_update = types.Update(**update)
        await dp.feed_update(bot, telegram_update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"ok": False}

# Запуск
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)