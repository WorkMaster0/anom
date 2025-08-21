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

# ================== Логування ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ultimaster-bot")

# ================== ENV ==================
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://anom-1.onrender.com").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", f"{APP_BASE_URL}/webhook").strip()

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задано")

# ================== Ініціалізація ==================
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

# ================== Кеш і глобальні ==================
alert_cache = TTLCache(maxsize=2000, ttl=86400)
active_monitoring = {}
latest_anomalies = []

# ================== Критерії ==================
MIN_CAP = int(os.getenv("MIN_CAP", "100000000"))  # $100M
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "10000"))  # $10K
MAX_VOLUME = int(os.getenv("MAX_VOLUME", "100000000"))  # $100M
MIN_PRICE_CHANGE = float(os.getenv("MIN_PRICE_CHANGE", "20"))  # 20%
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))  # 10 хвилин
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "20"))

# ================== Jupiter API ==================
JUPITER_REFRESH_INTERVAL = 60
last_jup_data = []
last_jup_time = 0
jup_lock = asyncio.Lock()

async def fetch_jupiter_data(force=False):
    """ Отримує дані з Jupiter API не частіше ніж раз/хв """
    global last_jup_data, last_jup_time
    now = datetime.now().timestamp()
    async with jup_lock:
        if not force and now - last_jup_time < JUPITER_REFRESH_INTERVAL and last_jup_data:
            return last_jup_data, 0

        url = "https://price.jup.ag/v4/tokens"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=20) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        tokens = data.get("data", {})
                        coins = []
                        for addr, token in tokens.items():
                            coins.append({
                                "id": addr,
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

# ================== Аналіз токенів ==================
async def analyze_coins(chat_id: int = None):
    coins, wait_time = await fetch_jupiter_data()
    anomalies = []
    if not coins:
        if chat_id:
            await bot.send_message(chat_id, f"⚠️ Немає даних з Jupiter API. Спробую ще раз через {wait_time} сек.")
        return anomalies

    for coin in coins:
        market_cap = coin.get('market_cap', 0) or 0
        volume = coin.get('total_volume', 0) or 0
        price_change = coin.get('price_change_percentage_24h', 0) or 0

        if (volume > MIN_VOLUME and volume < MAX_VOLUME and
            price_change >= MIN_PRICE_CHANGE):
            anomalies.append(coin)

    return anomalies

# ================== Алерти ==================
async def send_alert(chat_id: int, coin: dict):
    coin_id = coin.get("id", "")
    if coin_id in alert_cache:
        return

    name = escape(coin.get("name", "Unknown"))
    sym = escape(coin.get("symbol", "UNK").upper())
    price = coin.get("current_price", 0) or 0
    mcap = coin.get("market_cap", 0) or 0
    change = coin.get("price_change_percentage_24h", 0) or 0
    vol = coin.get("total_volume", 0) or 0
    chain = escape(coin.get("chainId", ""))

    msg = (
        f"🚨 <b>{name} ({sym})</b>\n"
        f"💰 Ціна: <code>${price:.8f}</code>\n"
        f"📊 Капіталізація: <code>{'Unknown' if mcap == 0 else f'${mcap:,}'}</code>\n"
        f"📈 Зміна 24h: <b>{change:.1f}%</b>\n"
        f"💹 Обсяг: <code>${vol:,}</code>\n"
        f"🔗 Ланцюг: {chain}\n"
        f"🔗 Дані з Jupiter API"
    )

    await bot.send_message(chat_id, msg, parse_mode="HTML", disable_web_page_preview=True)
    alert_cache[coin_id] = 1
    latest_anomalies.insert(0, coin)
    del latest_anomalies[200:]

# ================== Моніторинг ==================
async def monitoring_task(chat_id: int):
    logger.info(f"Starting monitoring task for chat_id: {chat_id}")
    while active_monitoring.get(chat_id):
        try:
            anomalies = await analyze_coins(chat_id)
            if not anomalies:
                await bot.send_message(chat_id, "ℹ️ Немає аномальних токенів. Спробую ще раз через 10 хвилин.")
            else:
                sent = 0
                for coin in anomalies:
                    if coin["id"] not in alert_cache and sent < MAX_ALERTS_PER_CYCLE:
                        await send_alert(chat_id, coin)
                        sent += 1
        except Exception as e:
            logger.error(f"monitoring_task error: {e}")
            await bot.send_message(chat_id, f"⚠️ Помилка моніторингу: {str(e)}")
        await asyncio.sleep(CHECK_INTERVAL)

async def clear_cache_task():
    while True:
        alert_cache.clear()
        await asyncio.sleep(86400)

# ================== Команди ==================
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
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
        "• /topvol — топ за обсягом\n"
        "• /topgainers — топ за ростом\n"
        "• /setcriteria — налаштувати критерії\n"
        "• /help — ця підказка"
    )

@dp.message(Command("stop"))
async def stop_cmd(message: types.Message):
    chat_id = message.chat.id
    if active_monitoring.pop(chat_id, None):
        await message.answer("⏹️ Моніторинг зупинено!")
    else:
        await message.answer("ℹ Моніторинг не активний")

@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    chat_id = message.chat.id
    stat = "активний" if active_monitoring.get(chat_id) else "неактивний"
    await message.answer(f"📊 Статус: {stat}\n🔍 У кеші сповіщень: {len(alert_cache)}")

@dp.message(Command("latest"))
async def latest_cmd(message: types.Message):
    if latest_anomalies:
        lines = ["🔍 Останні аномальні токени:"]
        for coin in latest_anomalies[:20]:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'])})")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ Аномалій ще немає")

@dp.message(Command("topvol"))
async def topvol_cmd(message: types.Message):
    coins, wait_time = await fetch_jupiter_data()
    if not coins:
        await message.answer(f"⚠️ Немає даних з Jupiter API. Спробую ще раз через {wait_time} сек.")
        return
    filtered = [c for c in coins if MIN_VOLUME < c.get("total_volume", 0) < MAX_VOLUME]
    top_coins = sorted(filtered, key=lambda x: x.get("total_volume", 0), reverse=True)[:20]
    if top_coins:
        lines = ["💹 Топ токенів за обсягом:"]
        for coin in top_coins:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'])}) — ${coin.get('total_volume', 0):,.0f}")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")

@dp.message(Command("topgainers"))
async def topgainers_cmd(message: types.Message):
    coins, wait_time = await fetch_jupiter_data()
    if not coins:
        await message.answer(f"⚠️ Немає даних з Jupiter API. Спробую ще раз через {wait_time} сек.")
        return
    filtered = [c for c in coins if MIN_VOLUME < c.get("total_volume", 0) < MAX_VOLUME]
    top_coins = sorted(filtered, key=lambda x: x.get("price_change_percentage_24h", 0), reverse=True)[:20]
    if top_coins:
        lines = ["🚀 Топ токенів за ростом:"]
        for coin in top_coins:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'])}) — +{coin.get('price_change_percentage_24h', 0):.1f}%")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")

@dp.message(Command("setcriteria"))
async def set_criteria_cmd(message: types.Message):
    try:
        args = message.text.split()[1:]
        if len(args) != 3:
            await message.answer("ℹ️ Використовуйте: /setcriteria MIN_CAP MIN_VOLUME MIN_PRICE_CHANGE")
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
    return await start_cmd(message)

# ================== Webhook ==================
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        logger.info(f"Attempting to set webhook to {WEBHOOK_URL}")
        await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        asyncio.create_task(clear_cache_task())
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        asyncio.create_task(dp.start_polling(bot))
    yield
    try:
        await bot.delete_webhook()
    except Exception as e:
        logger.error(f"Failed to delete webhook: {e}")

app.lifespan = lifespan

@app.get("/")
async def root():
    return {"message": "Ultimaster Bot (Jupiter API) is running!"}

@app.head("/")
async def root_head():
    return {"message": "Ultimaster Bot (Jupiter API) is running!"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
        telegram_update = types.Update(**update)
        await dp.feed_update(bot, telegram_update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"ok": False}

# ================== Запуск ==================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
