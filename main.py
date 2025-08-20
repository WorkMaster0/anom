import os
import asyncio
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from aiogram.webhook.aiohttp_server import setup_application

# =======================
# Налаштування
# =======================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "ВАШ_TELEGRAM_TOKEN")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://your-domain.tld")
WEBHOOK_PATH = f"/webhook/{TELEGRAM_TOKEN}"
WEBHOOK_URL = APP_BASE_URL + WEBHOOK_PATH
PORT = int(os.environ.get("PORT", 8000))

# =======================
# Ініціалізація бота та Dispatcher
# =======================
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# =======================
# Хендлери команд
# =======================
@dp.message(Command("start"))
async def start(message: Message):
    await message.answer("Привіт! Я бот 🚀\nВикористовуй /help для команд.")

@dp.message(Command("help"))
async def help(message: Message):
    await message.answer("/start - запуск бота\n/help - список команд\n/topcoins - топ монет\n/alerts - сповіщення")

@dp.message(Command("topcoins"))
async def topcoins(message: Message):
    # Просто приклад, без реального API
    coins = ["BTC - 30,000$", "ETH - 2,000$", "SOL - 25$"]
    await message.answer("Топ монет:\n" + "\n".join(coins))

@dp.message(Command("alerts"))
async def alerts(message: Message):
    await message.answer("Сповіщення ще не налаштовані 😉")

# =======================
# Вебхук для Render
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
# Запуск через uvicorn
# =======================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
