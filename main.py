import os
import asyncio
import logging
import aiohttp
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

from fastapi import FastAPI, Request
from aiogram.types import Update
import uvicorn

# ======================
# Налаштування
# ======================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Напр. https://your-app.onrender.com/webhook

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не знайдений у змінних оточення")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ultimaster-bot")

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

app = FastAPI()

JUPITER_API = "https://price.jup.ag/v4/tokens"  # актуальний endpoint

# ======================
# Jupiter API
# ======================
async def get_jupiter_data():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(JUPITER_API) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("data", [])
                else:
                    logger.error(f"Jupiter API failed: {resp.status}")
                    return []
    except Exception as e:
        logger.error(f"Jupiter request error: {e}")
        return []

# ======================
# Форматування токенів
# ======================
def format_token(token):
    price = float(token.get("price", 0))
    change = float(token.get("change24h", 0))
    volume = float(token.get("volume24h", 0))
    symbol = token.get("symbol", "")
    name = token.get("name", "")
    contract = token.get("address", "")

    return (
        f"• <b>{name}</b> (<code>{symbol}</code>)\n"
        f"   💲 Ціна: <b>${price:.6f}</b>\n"
        f"   📈 Зміна (24h): <b>{change:+.2f}%</b>\n"
        f"   💹 Обсяг: <b>${volume:,.0f}</b>\n"
        f"   🔗 Контракт: <code>{contract}</code>\n"
    )

def get_top_gainers(tokens, limit=10):
    return sorted(tokens, key=lambda t: float(t.get("change24h", 0)), reverse=True)[:limit]

def get_top_volume(tokens, limit=10):
    return sorted(tokens, key=lambda t: float(t.get("volume24h", 0)), reverse=True)[:limit]

# ======================
# Команди
# ======================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привіт! Я крипто-бот на базі Jupiter.\n\n"
        "📌 Доступні команди:\n"
        "/topgainers — 🚀 топ ростучих токенів (24h)\n"
        "/topvol — 💹 топ токенів за обсягом (24h)\n"
        "/hiddengems — 💎 приховані можливості\n"
        "/help — ℹ️ допомога"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "ℹ️ <b>Команди бота:</b>\n"
        "/topgainers — 🚀 топ ростучих токенів (24h)\n"
        "/topvol — 💹 топ токенів за обсягом (24h)\n"
        "/hiddengems — 💎 приховані можливості\n"
    )

@dp.message(Command("topgainers"))
async def top_gainers(message: Message):
    tokens = await get_jupiter_data()
    if not tokens:
        await message.answer("❌ Не вдалося отримати дані від Jupiter.")
        return
    top = get_top_gainers(tokens)
    msg = "🚀 <b>Топ ростучих токенів (24h):</b>\n\n"
    msg += "\n".join([format_token(t) for t in top])
    await message.answer(msg)

@dp.message(Command("topvol"))
async def top_volume(message: Message):
    tokens = await get_jupiter_data()
    if not tokens:
        await message.answer("❌ Не вдалося отримати дані від Jupiter.")
        return
    top = get_top_volume(tokens)
    msg = "💹 <b>Топ за обсягом (24h):</b>\n\n"
    msg += "\n".join([format_token(t) for t in top])
    await message.answer(msg)

@dp.message(Command("hiddengems"))
async def hidden_gems(message: Message):
    tokens = await get_jupiter_data()
    if not tokens:
        await message.answer("❌ Не вдалося отримати дані від Jupiter.")
        return
    # фільтр: низька капа, але позитивна зміна
    gems = [
        t for t in tokens
        if float(t.get("marketCap", 0)) < 2_000_000
        and float(t.get("change24h", 0)) > 10
    ]
    if not gems:
        await message.answer("😔 Немає прихованих токенів зараз.")
        return
    msg = "💎 <b>Hidden Gems:</b>\n\n"
    msg += "\n".join([format_token(t) for t in gems[:10]])
    await message.answer(msg)

# ======================
# FastAPI Webhook
# ======================
@app.on_event("startup")
async def on_startup():
    await bot.set_webhook(WEBHOOK_URL)
    logger.info("✅ Webhook встановлено")

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()
    logger.info("🛑 Webhook видалено")

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "бот працює 🚀"}

# ======================
# Запуск (локально / Render)
# ======================
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
