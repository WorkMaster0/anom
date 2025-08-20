import os
import logging
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
import asyncio

# ---------------- НАЛАШТУВАННЯ ----------------
TOKEN = os.getenv("TELEGRAM_TOKEN", "8063113740:AAGC-9PHzZD65jPad2lxP5mTmlWuQwvKwrU")  
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://WorkMaster0.onrender.com/webhook")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ultimaster-bot")

# ---------------- ІНІЦІАЛІЗАЦІЯ ----------------
bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

# ---------------- КОМАНДИ ----------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("Привіт 👋! Бот успішно працює через Render 🚀")

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer("Доступні команди:\n/start - почати\n/help - допомога")

# ---------------- WEBHOOK ----------------
@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    telegram_update = types.Update(**update)
    await dp.feed_update(bot, telegram_update)
    return {"ok": True}

# ---------------- ЗАПУСК ----------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
