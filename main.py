import os
import uvicorn
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from bot.commands import dp, bot
from bot.config import TELEGRAM_BOT_TOKEN, APP_BASE_URL, WEBHOOK_SECRET

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher(storage=None)

app = FastAPI()

@app.get("/")
async def home():
    return {"status": "ok", "name": "UltiMaster Crypto"}

@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if not WEBHOOK_SECRET or secret != WEBHOOK_SECRET:
        return {"status": "forbidden"}
    data = await request.json()
    update = Update(**data)
    await dp.feed_update(bot, update)
    return {"status": "ok"}

@app.on_event("startup")
async def on_startup():
    if APP_BASE_URL and WEBHOOK_SECRET:
        url = f"{APP_BASE_URL}/webhook/{WEBHOOK_SECRET}"
        try:
            await bot.set_webhook(url)
            print("Webhook встановлено:", url)
        except Exception as e:
            print("Помилка встановлення вебхука:", e)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("bot.main:app", host="0.0.0.0", port=port)
