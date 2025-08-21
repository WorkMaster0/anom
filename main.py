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
MOBULA_API_KEY = os.getenv("MOBULA_API_KEY", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://anom-1.onrender.com").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", f"{APP_BASE_URL}/webhook").strip()
MIN_CAP = int(os.getenv("MIN_CAP", "100000000"))
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "10000"))
MAX_VOLUME = int(os.getenv("MAX_VOLUME", "100000000"))
MIN_PRICE_CHANGE = float(os.getenv("MIN_PRICE_CHANGE", "20"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "20"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задано")
if not MOBULA_API_KEY:
    raise RuntimeError("MOBULA_API_KEY не задано")

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

# Mobula API
async def fetch_mobula_cached():
    global last_fetch_time, last_coins_data
    now = datetime.now().timestamp()
    if now - last_fetch_time < 600 and last_coins_data:
        logger.info("Returning cached Mobula data")
        return last_coins_data

    logger.info("Fetching new data from Mobula API")
    url = "https://api.mobula.io/v1/market/multi-data"
    headers = {"Authorization": f"Bearer {MOBULA_API_KEY}"}
    params = {"assets": "all", "limit": 500, "offset": 0}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=10) as resp:
                logger.info(f"Mobula API response status: {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"Received {len(data.get('data', []))} coins from Mobula")
                    raw_assets = data.get("data", [])
                    formatted_data = []
                    for asset in raw_assets:
                        coin = {
                            "id": asset.get("id", ""),
                            "name": asset.get("name", ""),
                            "symbol": asset.get("symbol", ""),
                            "market_cap": asset.get("market_cap", 0),
                            "total_volume": asset.get("volume_24h", 0),
                            "current_price": asset.get("price", 0),
                            "price_change_percentage_24h": asset.get("price_change_24h", 0) * 100
                        }
                        formatted_data.append(coin)
                    last_coins_data = formatted_data
                    last_fetch_time = now
                    return formatted_data
                elif resp.status == 429:
                    logger.warning("Mobula rate limit 429, retry in 60s")
                    await asyncio.sleep(60)
                    return await fetch_mobula_cached()
                else:
                    logger.error(f"Mobula status {resp.status}")
    except Exception as e:
        logger.error(f"Помилка запиту Mobula: {e}")

    return last_coins_data

async def analyze_coins():
    coins = await fetch_mobula_cached()
    anomalies = []
    for coin in coins:
        market_cap = coin.get('market_cap', 0)
        volume = coin.get('total_volume', 0)
        price_change = coin.get('price_change_percentage_24h', 0)

        if market_cap < MIN_CAP and volume > MIN_VOLUME and price_change > MIN_PRICE_CHANGE:
            anomalies.append(coin)
    logger.info(f"Found {len(anomalies)} anomalies")
    return anomalies

async def send_alert(chat_id: int, coin: dict):
    coin_id = coin.get("id", "")
    if coin_id in alert_cache:
        return

    name = escape(coin.get("name", ""))
    sym = escape((coin.get("symbol") or "").upper())
    price = coin.get("current_price") or 0
    mcap = coin.get("market_cap") or 0
    change = coin.get("price_change_percentage_24h") or 0
    vol = coin.get("total_volume") or 0

    msg = (
        f"🚨 <b>{name} ({sym})</b>\n"
        f"💰 Ціна: <code>${price:.8f}</code>\n"
        f"📊 Капіталізація: <code>${mcap:,}</code>\n"
        f"📈 Зміна 24h: <b>{change:.1f}%</b>\n"
        f"💹 Обсяг: <code>${vol:,}</code>\n"
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
            anomalies = await analyze_coins()
            sent = 0
            for coin in anomalies:
                if coin["id"] not in alert_cache and sent < MAX_ALERTS_PER_CYCLE:
                    await send_alert(chat_id, coin)
                    sent += 1
        except Exception as e:
            logger.error(f"monitoring_task error: {e}")
        await asyncio.sleep(CHECK_INTERVAL)

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
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'].upper())})")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ Аномалій ще немає")

@dp.message(Command("topvol"))
async def topvol_cmd(message: types.Message):
    logger.info(f"Received /topvol command from chat_id: {message.chat.id}")
    coins = await fetch_mobula_cached()
    filtered = [c for c in coins if c.get("market_cap", 0) < MIN_CAP]
    top_coins = sorted(filtered, key=lambda x: x.get("total_volume", 0), reverse=True)[:20]
    if top_coins:
        lines = ["💹 Топ дрібних капів за обсягом:"]
        for coin in top_coins:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'].upper())}) — ${coin.get('total_volume', 0):,.0f}")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")

@dp.message(Command("topgainers"))
async def topgainers_cmd(message: types.Message):
    logger.info(f"Received /topgainers command from chat_id: {message.chat.id}")
    coins = await fetch_mobula_cached()
    filtered = [c for c in coins if c.get("market_cap", 0) < MIN_CAP]
    top_coins = sorted(filtered, key=lambda x: x.get("price_change_percentage_24h", 0), reverse=True)[:20]
    if top_coins:
        lines = ["🚀 Топ дрібних капів за ростом:"]
        for coin in top_coins:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'].upper())}) — +{coin.get('price_change_percentage_24h', 0):.1f}%")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    logger.info(f"Received /help command from chat_id: {message.chat.id}")
    return await start_cmd(message)

# Webhook і Lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        logger.info(f"Webhook set to {WEBHOOK_URL}")
        # Перевірка статусу вебхука
        webhook_info = await bot.get_webhook_info()
        logger.info(f"Webhook info: {webhook_info}")
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
    
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