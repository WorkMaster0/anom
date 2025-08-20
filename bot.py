# bot_webhook_full_ready.py
import os
import asyncio
import httpx
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.types import Update
from datetime import datetime, timedelta

# ================== Налаштування ==================
TOKEN = os.getenv("BOT_TOKEN")  # токен твого бота
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "ultimaster123")

bot = Bot(token=TOKEN)
dp = Dispatcher()
app = FastAPI()

API_URL = "https://api.mobula.org/v1/tokens"  # заміни на свій API

# ================== Кеш ==================
cache = {
    "topvol": {"data": [], "expires": datetime.min},
    "topgainers": {"data": [], "expires": datetime.min},
}
CACHE_DURATION = timedelta(minutes=5)  # оновлення кожні 5 хвилин

# ================== Функції отримання даних ==================
async def fetch_tokens(sort_by: str):
    now = datetime.utcnow()
    if cache[sort_by]["data"] and cache[sort_by]["expires"] > now:
        return cache[sort_by]["data"]
    async with httpx.AsyncClient() as client:
        params = {"cap_max": 5000000, "sort": sort_by}
        try:
            r = await client.get(API_URL, params=params, timeout=10)
            r.raise_for_status()
        except Exception as e:
            print(f"Помилка при запиті {sort_by}: {e}")
            return []
        data = r.json()
        tokens = data.get("tokens", [])
        cache[sort_by]["data"] = tokens
        cache[sort_by]["expires"] = now + CACHE_DURATION
        return tokens

async def get_topvol():
    return await fetch_tokens("volume")

async def get_topgainers():
    return await fetch_tokens("change")

# ================== Хендлери команд ==================
@dp.message(commands=["start"])
async def start_command(message: types.Message):
    await message.answer(
        "Привіт! Це Ultimaster бот. Використовуй /topvol або /topgainers."
    )

@dp.message(commands=["topvol"])
async def topvol_command(message: types.Message):
    tokens = await get_topvol()
    if not tokens:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")
        return
    response = "💹 Топ дрібних капів за обсягом:\n"
    for t in tokens[:10]:
        response += f"{t.get('symbol', 'N/A')} - {t.get('volume', 'N/A')}\n"
    await message.answer(response)

@dp.message(commands=["topgainers"])
async def topgainers_command(message: types.Message):
    tokens = await get_topgainers()
    if not tokens:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")
        return
    response = "🚀 Топ дрібних капів за ростом:\n"
    for t in tokens[:10]:
        response += f"{t.get('symbol', 'N/A')} - {t.get('change', 'N/A')}%\n"
    await message.answer(response)

# ================== FastAPI Webhook ==================
@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update(**data)
    await dp.feed_update(update)
    return {"status": "ok"}

# ================== Фонове оновлення кешу ==================
async def background_cache_updater():
    while True:
        await get_topvol()
        await get_topgainers()
        await asyncio.sleep(CACHE_DURATION.total_seconds())

# ================== Запуск ==================
if __name__ == "__main__":
    import uvicorn

    loop = asyncio.get_event_loop()
    loop.create_task(background_cache_updater())  # старт кеш-оновлення

    uvicorn.run("bot_webhook_full_ready:app", host="0.0.0.0", port=8000, reload=True)
