import os
import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
import aiohttp
from dotenv import load_dotenv

# ======================
# Налаштування
# ======================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не знайдений у змінних оточення")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ultimaster-bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

JUPITER_API = "https://price.jup.ag/v1/tokens"

# ======================
# Запит до Jupiter
# ======================
async def get_jupiter_data():
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(JUPITER_API) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["tokens"]
                else:
                    logger.error(f"Jupiter request failed: {resp.status}")
                    return []
    except Exception as e:
        logger.error(f"Jupiter request error: {e}")
        return []

# ======================
# Допоміжні функції
# ======================
def format_token(token):
    price = float(token.get("priceUsd", 0))
    change = float(token.get("change24h", 0))
    symbol = token.get("symbol", "")
    name = token.get("name", "")
    contract = token.get("address", "")
    return f"• {name} ({symbol}) — ${price:.4f} | {change:+.2f}% | {contract}"

def get_top_gainers(tokens, limit=10):
    sorted_tokens = sorted(tokens, key=lambda t: float(t.get("change24h", 0)), reverse=True)
    return sorted_tokens[:limit]

def get_top_volume(tokens, limit=10):
    sorted_tokens = sorted(tokens, key=lambda t: float(t.get("volumeUsd24h", 0)), reverse=True)
    return sorted_tokens[:limit]

# ======================
# Команди бота
# ======================
@dp.message(Command("topgainers"))
async def top_gainers(message: Message):
    tokens = await get_jupiter_data()
    if not tokens:
        await message.answer("❌ Не вдалося отримати дані від Jupiter.")
        return

    top = get_top_gainers(tokens)
    msg = "🚀 Топ «ростучих» (24h):\n"
    for t in top:
        msg += format_token(t) + "\n"
    await message.answer(msg)

@dp.message(Command("topvol"))
async def top_volume(message: Message):
    tokens = await get_jupiter_data()
    if not tokens:
        await message.answer("❌ Не вдалося отримати дані від Jupiter.")
        return

    top = get_top_volume(tokens)
    msg = "💹 Топ за обсягом (24h):\n"
    for t in top:
        msg += format_token(t) + "\n"
    await message.answer(msg)

@dp.message(Command("hiddengems"))
async def hidden_gems(message: Message):
    tokens = await get_jupiter_data()
    if not tokens:
        await message.answer("❌ Не вдалося отримати дані від Jupiter.")
        return

    # Тут можна додати свою логіку, фільтрувати маловідомі токени
    hidden = [t for t in tokens if float(t.get("marketCapUsd", 0)) < 1_000_000]
    msg = "💎 Потенційно прибуткові маловідомі токени:\n"
    for t in hidden[:10]:
        msg += format_token(t) + "\n"
    await message.answer(msg)

# ======================
# Живучість
# ======================
async def periodic_monitor():
    while True:
        try:
            tokens = await get_jupiter_data()
            if tokens:
                logger.info("Jupiter data fetched successfully.")
        except Exception as e:
            logger.error(f"Monitoring error: {e}")
        await asyncio.sleep(300)  # перевірка кожні 5 хв

# ======================
# Запуск
# ======================
async def main():
    asyncio.create_task(periodic_monitor())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
