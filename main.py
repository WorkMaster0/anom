import os
import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import setup_application
from aiohttp import web
import aiohttp

# =======================
# Налаштування
# =======================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ВАШ_TELEGRAM_TOKEN")
MOBULA_API_KEY = os.environ.get("MOBULA_API_KEY", "ВАШ_MOBULA_API_KEY")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://your-domain.tld")
WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
WEBHOOK_URL = APP_BASE_URL + WEBHOOK_PATH
PORT = int(os.environ.get("PORT", 8000))

# Опційно: мінімальні параметри для відбору монет
MIN_CAP = int(os.environ.get("MIN_CAP", 100_000_000))
MIN_VOLUME = int(os.environ.get("MIN_VOLUME", 10_000))
MIN_PRICE_CHANGE = float(os.environ.get("MIN_PRICE_CHANGE", 20))

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", 120))  # секунд

# =======================
# Ініціалізація бота
# =======================
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# =======================
# Хендлери команд
# =======================
@dp.message(Command("start"))
async def start_handler(message: Message):
    await message.answer("Привіт! Я CryptoBot 🚀\nВикористовуй /help для команд.")

@dp.message(Command("help"))
async def help_handler(message: Message):
    await message.answer(
        "Список команд:\n"
        "/start - запуск бота\n"
        "/help - список команд\n"
        "/topcoins - показати топ монет за капіталізацією\n"
        "/alerts - підписка на сигнали"
    )

@dp.message(Command("topcoins"))
async def topcoins_handler(message: Message):
    coins = await fetch_top_coins()
    if coins:
        text = "Топ монет:\n" + "\n".join(coins[:10])
    else:
        text = "Не вдалося отримати дані."
    await message.answer(text)

@dp.message(Command("alerts"))
async def alerts_handler(message: Message):
    await message.answer("Поки що підписка не активна. Робимо апдейт скоро 😉")

# =======================
# Функції для MOBULA API
# =======================
async def fetch_top_coins():
    url = "https://api.mobula.pro/v1/coins"
    headers = {"Authorization": f"Bearer {MOBULA_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                # фільтруємо по капіталізації і обсягу
                top = [
                    f"{c['symbol']} - Cap: {c['market_cap']:,} USD"
                    for c in data
                    if c['market_cap'] >= MIN_CAP and c['volume_24h'] >= MIN_VOLUME
                ]
                return top
    except Exception as e:
        print("Помилка fetch_top_coins:", e)
        return None

# =======================
# Вебхук
# =======================
async def on_startup(app: web.Application):
    await bot.set_webhook(WEBHOOK_URL)
    print("Webhook встановлено:", WEBHOOK_URL)

async def on_shutdown(app: web.Application):
    await bot.delete_webhook()
    await bot.session.close()

app = web.Application()
setup_application(app, dp, path=WEBHOOK_PATH)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_shutdown)

# =======================
# Запуск локально або на Render
# =======================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
