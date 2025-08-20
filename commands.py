from aiogram import types, Bot, Dispatcher
from html import escape
import asyncio
from .mobula import fetch_mobula_cached, analyze_coins, alert_cache
from .config import MIN_CAP, MAX_ALERTS_PER_CYCLE, CHECK_INTERVAL

bot: Bot = None
dp: Dispatcher = None
active_monitoring = {}
latest_anomalies = []

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

async def monitoring_task(chat_id: int):
    while active_monitoring.get(chat_id):
        try:
            anomalies = await analyze_coins()
            sent = 0
            for coin in anomalies:
                if coin["id"] not in alert_cache and sent < MAX_ALERTS_PER_CYCLE:
                    await send_alert(chat_id, coin)
                    sent += 1
        except Exception as e:
            print("monitoring_task error:", e)
        await asyncio.sleep(CHECK_INTERVAL)

# Команди
from aiogram.filters import Command

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
        "• /topvol — топ дрібних капів за обсягом\n"
        "• /topgainers — топ дрібних капів за ростом\n"
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
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'].upper())})")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ Аномалій ще немає")

@dp.message(Command("topvol"))
async def topvol_cmd(message: types.Message):
    coins = await fetch_mobula_cached()
    filtered = [c for c in coins if c.get("market_cap",0) < MIN_CAP]
    top_coins = sorted(filtered, key=lambda x: x.get("total_volume",0), reverse=True)[:20]
    if top_coins:
        lines = ["💹 Топ дрібних капів за обсягом:"]
        for coin in top_coins:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'].upper())}) — ${coin.get('total_volume',0):,.0f}")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")

@dp.message(Command("topgainers"))
async def topgainers_cmd(message: types.Message):
    coins = await fetch_mobula_cached()
    filtered = [c for c in coins if c.get("market_cap",0) < MIN_CAP]
    top_coins = sorted(filtered, key=lambda x: x.get("price_change_percentage_24h",0), reverse=True)[:20]
    if top_coins:
        lines = ["🚀 Топ дрібних капів за ростом:"]
        for coin in top_coins:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'].upper())}) — +{coin.get('price_change_percentage_24h',0):.1f}%")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Немає токенів, що відповідають критеріям.")

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    return await start_cmd(message)