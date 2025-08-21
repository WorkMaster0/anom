# main.py
import os
import logging
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from html import escape
from typing import List, Dict, Tuple, Any

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from cachetools import TTLCache
from dotenv import load_dotenv
import aiohttp

# ------------------------------------------------------------------------------
# ЛОГИ
# ------------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ultimaster-bot")

# ------------------------------------------------------------------------------
# ENV
# ------------------------------------------------------------------------------
load_dotenv()

# ЗАЛИШАЮ ТОЧНО ТАКІ Ж НАЗВИ, ЯК У ВАС БУЛИ
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://anom-1.onrender.com").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", f"{APP_BASE_URL}/webhook").strip()

# КРИТЕРІЇ (глобальні змінні)
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "1000"))           # $1K
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.000001"))      # Мінімальна ціна
MIN_PRICE_CHANGE = float(os.getenv("MIN_PRICE_CHANGE", "5"))  # 5%
MAX_VOLUME = int(os.getenv("MAX_VOLUME", "100000000"))     # $100M
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))   # 10 хвилин
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "20"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задано")

# ------------------------------------------------------------------------------
# Jupiter Lite API (без ключа)
# ------------------------------------------------------------------------------
JUP_BASE = "https://lite-api.jup.ag"
TOKEN_V2_BASE = f"{JUP_BASE}/tokens/v2"
PRICE_V3_BASE = f"{JUP_BASE}/price/v3"

# Внутрішній ліміт
MAX_REQUESTS_PER_MINUTE = 18
PER_REQUEST_DELAY = 0.35

# ------------------------------------------------------------------------------
# Ініціалізація
# ------------------------------------------------------------------------------
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

# ------------------------------------------------------------------------------
# СТАН
# ------------------------------------------------------------------------------
alert_cache = TTLCache(maxsize=2000, ttl=86400)
active_monitoring: Dict[int, bool] = {}
latest_anomalies: List[Dict[str, Any]] = []

# Рейт-ліміт
request_lock = asyncio.Lock()
request_count = 0
request_count_reset_time = 0.0
rate_limit_exceeded = False
last_rate_limit_time = 0.0

# Кеші
recent_cache = TTLCache(maxsize=2, ttl=300)
category_cache = TTLCache(maxsize=10, ttl=180)
price_cache = TTLCache(maxsize=5000, ttl=60)

# ------------------------------------------------------------------------------
# УТИЛІТИ HTTP
# ------------------------------------------------------------------------------
async def _sleep_delay():
    await asyncio.sleep(PER_REQUEST_DELAY)

def _now_ts() -> float:
    return datetime.now().timestamp()

async def _rate_guard() -> Tuple[bool, int]:
    global request_count, request_count_reset_time, rate_limit_exceeded, last_rate_limit_time

    now = _now_ts()

    if now - request_count_reset_time > 60:
        request_count = 0
        request_count_reset_time = now
        logger.info("Reset request count")

    if rate_limit_exceeded and (now - last_rate_limit_time) < 30:
        remaining = int(30 - (now - last_rate_limit_time))
        return False, remaining

    if request_count >= MAX_REQUESTS_PER_MINUTE:
        remaining = int(60 - (now - request_count_reset_time))
        return False, max(remaining, 1)

    return True, 0

async def _http_get_json(session: aiohttp.ClientSession, url: str, params: Dict[str, Any] | None = None, timeout: int = 15) -> Dict[str, Any] | List[Any]:
    global request_count, rate_limit_exceeded, last_rate_limit_time
    ok, wait_s = await _rate_guard()
    if not ok:
        logger.info(f"Rate guard: waiting {wait_s}s before {url}")
        await asyncio.sleep(wait_s)

    await _sleep_delay()
    try:
        async with session.get(url, params=params, timeout=timeout) as resp:
            status = resp.status
            text = await resp.text()
            if status == 200:
                request_count += 1
                if rate_limit_exceeded:
                    rate_limit_exceeded = False
                try:
                    return await resp.json()
                except Exception:
                    logger.error(f"JSON decode error for {url}: {text[:200]}")
                    return {}
            elif status == 429:
                logger.warning(f"429 from {url}: {text[:200]}")
                rate_limit_exceeded = True
                last_rate_limit_time = _now_ts()
                return {}
            else:
                logger.error(f"HTTP {status} from {url}: {text[:200]}")
                return {}
    except Exception as e:
        logger.error(f"HTTP error for {url}: {e}")
        return {}

# ------------------------------------------------------------------------------
# Jupiter: ЗАПИТИ
# ------------------------------------------------------------------------------
async def jup_get_recent(limit: int = 80) -> List[Dict[str, Any]]:
    cache_key = f"recent:{limit}"
    if cache_key in recent_cache:
        return recent_cache[cache_key]

    url = f"{TOKEN_V2_BASE}/recent"
    params = {"limit": limit}
    async with aiohttp.ClientSession() as session:
        data = await _http_get_json(session, url, params)
    if not isinstance(data, list):
        data = []
    recent_cache[cache_key] = data
    logger.info(f"Jupiter recent: {len(data)} tokens")
    return data

async def jup_get_category(category: str, interval: str = "24h", limit: int = 100) -> List[Dict[str, Any]]:
    cache_key = f"cat:{category}:{interval}:{limit}"
    if cache_key in category_cache:
        return category_cache[cache_key]

    url = f"{TOKEN_V2_BASE}/{category}/{interval}"
    params = {"limit": limit}
    async with aiohttp.ClientSession() as session:
        data = await _http_get_json(session, url, params)
    if not isinstance(data, list):
        data = []
    category_cache[cache_key] = data
    logger.info(f"Jupiter category {category}/{interval}: {len(data)} tokens")
    return data

async def jup_get_prices(mints: List[str]) -> Dict[str, float]:
    result: Dict[str, float] = {}
    to_fetch = []
    for m in mints:
        if m in price_cache:
            result[m] = price_cache[m]
        else:
            to_fetch.append(m)

    if not to_fetch:
        return result

    batch_size = 90
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(to_fetch), batch_size):
            batch = to_fetch[i:i + batch_size]
            params = {"ids": ",".join(batch)}
            data = await _http_get_json(session, PRICE_V3_BASE, params)
            data_map = {}
            if isinstance(data, dict):
                data_map = data.get("data", {}) or {}
            for mint, val in data_map.items():
                price = 0.0
                if isinstance(val, dict):
                    price = float(val.get("price") or val.get("priceUsd") or 0.0)
                result[mint] = price
                price_cache[mint] = price

    return result

async def jup_get_real_time_data() -> List[Dict[str, Any]]:
    """Отримує актуальні дані з різних джерел Jupiter"""
    try:
        recent_tokens = await jup_get_recent(limit=100)
        trending_tokens = await jup_get_category("toptrending", "1h", limit=50)
        
        all_tokens = recent_tokens + trending_tokens
        unique_tokens = {}
        
        for token in all_tokens:
            if isinstance(token, dict):
                mint = token.get("mint") or token.get("address") or token.get("id")
                if mint:
                    unique_tokens[mint] = token
        
        return list(unique_tokens.values())
        
    except Exception as e:
        logger.error(f"Error getting real-time data: {e}")
        return []

async def jup_get_detailed_prices(mints: List[str]) -> Dict[str, Dict[str, Any]]:
    """Отримує детальну інформацію про ціни для списку токенів"""
    if not mints:
        return {}
    
    try:
        async with aiohttp.ClientSession() as session:
            batch_size = 50
            results = {}
            
            for i in range(0, len(mints), batch_size):
                batch = mints[i:i + batch_size]
                params = {"ids": ",".join(batch)}
                
                data = await _http_get_json(session, PRICE_V3_BASE, params)
                if isinstance(data, dict) and "data" in data:
                    for mint, price_data in data["data"].items():
                        if isinstance(price_data, dict):
                            results[mint] = {
                                "price": float(price_data.get("price", 0) or 0),
                                "price_change_24h": float(price_data.get("priceChange24hPct", 0) or 0),
                                "volume_24h": float(price_data.get("volume24h", 0) or 0)
                            }
                
                await asyncio.sleep(0.2)
            
            return results
            
    except Exception as e:
        logger.error(f"Error getting detailed prices: {e}")
        return {}

# ------------------------------------------------------------------------------
# НОРМАЛІЗАЦІЯ ДАНИХ
# ------------------------------------------------------------------------------
def normalize_token(obj: Dict[str, Any]) -> Dict[str, Any]:
    mint = obj.get("mint") or obj.get("address") or obj.get("id") or ""
    name = obj.get("name") or obj.get("tokenName") or "Unknown"
    symbol = (obj.get("symbol") or obj.get("tokenSymbol") or "UNK").upper()
    logo = obj.get("logoURI") or obj.get("logo") or ""
    
    vol = (
        obj.get("volumeUSD") or obj.get("volume_usd") or
        obj.get("traded24hUSD") or obj.get("traded_usd") or
        obj.get("metrics", {}).get("volumeUSD") or 0
    )
    change = (
        obj.get("priceChange24hPct") or obj.get("price_change_24h") or
        obj.get("change24h") or obj.get("pctChange24h") or 0
    )
    try:
        vol = float(vol or 0)
    except Exception:
        vol = 0.0
    try:
        change = float(change or 0)
    except Exception:
        change = 0.0

    return {
        "id": mint,
        "name": name,
        "symbol": symbol,
        "logo": logo,
        "current_price": 0.0,
        "market_cap": 0,
        "total_volume": vol,
        "price_change_percentage_24h": change,
        "chainId": "solana",
        "pairAddress": "",
    }

def merge_price(tokens: List[Dict[str, Any]], price_map: Dict[str, float]) -> None:
    for t in tokens:
        p = price_map.get(t["id"], 0.0)
        t["current_price"] = float(p or 0.0)

# ------------------------------------------------------------------------------
# АНАЛІТИКА
# ------------------------------------------------------------------------------
async def analyze_coins(chat_id: int | None = None) -> List[Dict[str, Any]]:
    """Нова логіка пошуку аномальних токенів"""
    anomalies: List[Dict[str, Any]] = []
    
    try:
        logger.info("Step 1: Getting real-time tokens...")
        tokens = await jup_get_real_time_data()
        
        if not tokens:
            logger.warning("No tokens received from API")
            return anomalies
        
        mints = []
        for token in tokens:
            mint = token.get("mint") or token.get("address") or token.get("id")
            if mint:
                mints.append(mint)
        
        logger.info(f"Step 2: Getting prices for {len(mints)} tokens...")
        price_data = await jup_get_detailed_prices(mints)
        
        logger.info("Step 3: Processing tokens...")
        for token in tokens:
            mint = token.get("mint") or token.get("address") or token.get("id")
            if not mint or mint not in price_data:
                continue
            
            p_data = price_data[mint]
            price = p_data.get("price", 0)
            change_24h = p_data.get("price_change_24h", 0)
            volume_24h = p_data.get("volume_24h", 0)
            
            if price <= 0:
                continue
            
            normalized = {
                "id": mint,
                "name": token.get("name", "Unknown"),
                "symbol": token.get("symbol", "UNK"),
                "logo": token.get("logoURI", ""),
                "current_price": price,
                "price_change_percentage_24h": change_24h,
                "total_volume": volume_24h,
                "market_cap": 0,
                "chainId": "solana",
                "trending": True
            }
            
            # Використовуємо глобальні змінні без оголошення global
            volume_ok = volume_24h > MIN_VOLUME
            price_ok = price > MIN_PRICE
            change_ok = abs(change_24h) > MIN_PRICE_CHANGE
            
            if volume_ok and price_ok and change_ok:
                logger.info(f"ANOMALY FOUND: {normalized['name']} "
                           f"(Price: ${price:.6f}, Change: {change_24h:.1f}%, Volume: ${volume_24h:,.0f})")
                anomalies.append(normalized)
        
        logger.info(f"Found {len(anomalies)} anomalies")
        return anomalies
        
    except Exception as e:
        logger.error(f"Error in analyze_coins: {e}")
        return anomalies

# ------------------------------------------------------------------------------
# ВІДПРАВКА АЛЕРТІВ
# ------------------------------------------------------------------------------
async def send_alert(chat_id: int, coin: Dict[str, Any]):
    coin_id = coin.get("id", "")
    if not coin_id or coin_id in alert_cache:
        return

    name = escape(coin.get("name", "Unknown"))
    sym = escape(coin.get("symbol", "UNK").upper())
    price = float(coin.get("current_price", 0) or 0)
    mcap = int(coin.get("market_cap", 0) or 0)
    change = float(coin.get("price_change_percentage_24h", 0) or 0)
    vol = float(coin.get("total_volume", 0) or 0)
    chain = escape(coin.get("chainId", ""))

    msg_lines = [
        f"🚨 <b>{name} ({sym})</b>",
        f"💰 Ціна: <code>${price:.8f}</code>",
        f"📊 Капіталізація: <code>{'Unknown' if mcap == 0 else f'${mcap:,}'}</code>",
        f"📈 Зміна 24h: <b>{change:.1f}%</b>",
        f"💹 Обсяг: <code>${vol:,.0f}</code>",
        f"🔥 Тренд: {'так' if coin.get('trending') else 'ні'}",
        f"🔗 Ланцюг: {chain}",
        f"🔗 Дані: Jupiter Lite API",
    ]
    await bot.send_message(chat_id, "\n".join(msg_lines), parse_mode="HTML", disable_web_page_preview=True)

    alert_cache[coin_id] = 1
    latest_anomalies.insert(0, coin)
    del latest_anomalies[200:]
    logger.info(f"Sent alert for {name} to chat_id: {chat_id}")

# ------------------------------------------------------------------------------
# ЗАДАЧІ
# ------------------------------------------------------------------------------
async def monitoring_task(chat_id: int):
    logger.info(f"Starting monitoring task for chat_id: {chat_id}")
    
    await bot.send_message(chat_id, "🔍 Моніторинг запущено! Шукаємо аномальні токени...")
    
    cycle_count = 0
    while active_monitoring.get(chat_id):
        try:
            cycle_count += 1
            logger.info(f"Monitoring cycle #{cycle_count} for chat_id: {chat_id}")
            
            anomalies = await analyze_coins(chat_id)
            
            if not anomalies:
                logger.info("No anomalies found in this cycle")
                if cycle_count % 3 == 0:
                    await bot.send_message(
                        chat_id, 
                        "ℹ️ Аномальних токенів не знайдено. "
                        "Моніторинг продовжується...\n\n"
                        "Перевіряються токени з:\n"
                        f"• Зміна ціни > ±{MIN_PRICE_CHANGE}%\n"
                        f"• Обсяг торгів > ${MIN_VOLUME:,}\n"
                        f"• Ціна > ${MIN_PRICE:.6f}"
                    )
            else:
                sent = 0
                for coin in anomalies:
                    if coin["id"] not in alert_cache and sent < MAX_ALERTS_PER_CYCLE:
                        await send_alert(chat_id, coin)
                        sent += 1
                        await asyncio.sleep(1)
                
                if sent > 0:
                    await bot.send_message(
                        chat_id, 
                        f"✅ Знайдено {sent} аномальних токенів!"
                    )
            
            await asyncio.sleep(CHECK_INTERVAL)
            
        except Exception as e:
            logger.error(f"monitoring_task error: {e}")
            await bot.send_message(
                chat_id, 
                f"⚠️ Помилка моніторингу: {str(e)[:200]}...\n"
                "Спробую знову через 10 хвилин."
            )
            await asyncio.sleep(CHECK_INTERVAL)

async def clear_cache_task():
    while True:
        logger.info("Clearing alert cache")
        alert_cache.clear()
        await asyncio.sleep(86400)

# ------------------------------------------------------------------------------
# КОМАНДИ
# ------------------------------------------------------------------------------
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    logger.info(f"Received /start from chat_id: {message.chat.id}")
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
        "• /topgainers — топ ростучі\n"
        "• /setcriteria — налаштувати критерії\n"
        "• /testscan — тестове сканування\n"
        "• /help — ця підказка"
    )

@dp.message(Command("stop"))
async def stop_cmd(message: types.Message):
    logger.info(f"Received /stop from chat_id: {message.chat.id}")
    chat_id = message.chat.id
    if active_monitoring.pop(chat_id, None):
        await message.answer("⏹️ Моніторинг зупинено!")
    else:
        await message.answer("ℹ Моніторинг не активний")

@dp.message(Command("status"))
async def status_cmd(message: types.Message):
    logger.info(f"Received /status from chat_id: {message.chat.id}")
    chat_id = message.chat.id
    stat = "активний" if active_monitoring.get(chat_id) else "неактивний"
    await message.answer(
        f"📊 Статус: {stat}\n"
        f"🔍 У кеші сповіщень: {len(alert_cache)}\n"
        f"📈 Останні аномалії: {len(latest_anomalies)}\n\n"
        f"Поточні критерії:\n"
        f"• Мін. обсяг: ${MIN_VOLUME:,}\n"
        f"• Мін. ціна: ${MIN_PRICE:.6f}\n"
        f"• Мін. зміна: {MIN_PRICE_CHANGE}%"
    )

@dp.message(Command("latest"))
async def latest_cmd(message: types.Message):
    logger.info(f"Received /latest from chat_id: {message.chat.id}")
    
    if not latest_anomalies:
        await message.answer("ℹ Аномалій ще немає")
        return
    
    lines = ["🔍 <b>Останні аномальні токени:</b>\n"]
    
    for i, coin in enumerate(latest_anomalies[:10], 1):
        name = escape(coin.get('name', 'Unknown')[:15])
        symbol = escape(coin.get('symbol', 'UNK')[:8])
        price = float(coin.get('current_price', 0) or 0)
        change = float(coin.get('price_change_percentage_24h', 0) or 0)
        
        change_icon = "📈" if change >= 0 else "📉"
        change_text = f"{change_icon} {abs(change):.1f}%"
        
        price_text = f"${price:,.8f}".rstrip('0').rstrip('.') if price < 1 else f"${price:,.4f}"
        
        lines.append(
            f"{i}. <b>{name} ({symbol})</b>\n"
            f"   💰 <i>Ціна:</i> <code>{price_text}</code>\n"
            f"   📊 <i>Зміна:</i> {change_text}\n"
        )
    
    await message.answer("\n".join(lines), parse_mode="HTML")

@dp.message(Command("topvol"))
async def topvol_cmd(message: types.Message):
    logger.info(f"Received /topvol from chat_id: {message.chat.id}")
    
    loading_msg = await message.answer("🔄 Завантажую топ токенів за обсягом...")
    
    try:
        top_tokens = await jup_get_real_time_data()
        
        if not top_tokens:
            await loading_msg.edit_text("❌ Не вдалося отримати дані")
            return
        
        detailed_tokens = []
        for token in top_tokens:
            mint = token.get("mint") or token.get("address") or token.get("id")
            if mint:
                price_data = await jup_get_detailed_prices([mint])
                if mint in price_data:
                    detailed_data = {
                        "name": token.get("name", "Unknown"),
                        "symbol": token.get("symbol", "UNK"),
                        "price": price_data[mint].get("price", 0),
                        "volume": price_data[mint].get("volume_24h", 0),
                        "change": price_data[mint].get("price_change_24h", 0)
                    }
                    detailed_tokens.append(detailed_data)
        
        if not detailed_tokens:
            await loading_msg.edit_text("❌ Не знайдено токенів з даними")
            return
        
        valid_tokens = [t for t in detailed_tokens if t["price"] > 0]
        valid_tokens.sort(key=lambda x: x.get("volume", 0), reverse=True)
        
        lines = ["💹 <b>Топ токенів за обсягом:</b>\n"]
        
        for i, token in enumerate(valid_tokens[:15], 1):
            name = escape(token.get('name', 'Unknown')[:15])
            symbol = escape(token.get('symbol', 'UNK')[:8])
            price = float(token.get('price', 0) or 0)
            volume = float(token.get('volume', 0) or 0)
            change = float(token.get('change', 0) or 0)
            
            change_icon = "📈" if change >= 0 else "📉"
            change_text = f"{change_icon} {abs(change):.1f}%"
            
            price_text = f"${price:,.8f}".rstrip('0').rstrip('.') if price < 1 else f"${price:,.4f}"
            volume_text = f"${volume:,.0f}" if volume >= 1000 else f"${volume:,.2f}"
            
            lines.append(
                f"{i}. <b>{name} ({symbol})</b>\n"
                f"   💰 <i>Ціна:</i> <code>{price_text}</code>\n"
                f"   💹 <i>Обсяг:</i> <code>{volume_text}</code>\n"
                f"   📊 <i>Зміна:</i> {change_text}\n"
            )
        
        await loading_msg.edit_text("\n".join(lines), parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error in topvol_cmd: {e}")
        await loading_msg.edit_text("⚠️ Помилка при отриманні даних")

@dp.message(Command("topgainers"))
async def topgainers_cmd(message: types.Message):
    logger.info(f"Received /topgainers from chat_id: {message.chat.id}")
    
    loading_msg = await message.answer("🔄 Завантажую топ ростучі токени...")
    
    try:
        top_tokens = await jup_get_real_time_data()
        
        if not top_tokens:
            await loading_msg.edit_text("❌ Не вдалося отримати дані")
            return
        
        detailed_tokens = []
        for token in top_tokens:
            mint = token.get("mint") or token.get("address") or token.get("id")
            if mint:
                price_data = await jup_get_detailed_prices([mint])
                if mint in price_data:
                    detailed_data = {
                        "name": token.get("name", "Unknown"),
                        "symbol": token.get("symbol", "UNK"),
                        "price": price_data[mint].get("price", 0),
                        "volume": price_data[mint].get("volume_24h", 0),
                        "change": price_data[mint].get("price_change_24h", 0)
                    }
                    detailed_tokens.append(detailed_data)
        
        if not detailed_tokens:
            await loading_msg.edit_text("❌ Не знайдено токенів з даними")
            return
        
        gainers = [t for t in detailed_tokens if t["price"] > 0 and t.get("change", 0) > 0]
        if not gainers:
            gainers = [t for t in detailed_tokens if t["price"] > 0]
        
        if not gainers:
            await loading_msg.edit_text("❌ Не знайдено токенів з ціною")
            return
        
        gainers.sort(key=lambda x: x.get("change", 0), reverse=True)
        
        lines = ["🚀 <b>Топ ростучі токени:</b>\n"]
        
        for i, token in enumerate(gainers[:15], 1):
            name = escape(token.get('name', 'Unknown')[:15])
            symbol = escape(token.get('symbol', 'UNK')[:8])
            price = float(token.get('price', 0) or 0)
            volume = float(token.get('volume', 0) or 0)
            change = float(token.get('change', 0) or 0)
            
            change_icon = "📈"
            change_text = f"{change_icon} {change:.1f}%"
            
            price_text = f"${price:,.8f}".rstrip('0').rstrip('.') if price < 1 else f"${price:,.4f}"
            volume_text = f"${volume:,.0f}" if volume >= 1000 else f"${volume:,.2f}"
            
            lines.append(
                f"{i}. <b>{name} ({symbol})</b>\n"
                f"   💰 <i>Ціна:</i> <code>{price_text}</code>\n"
                f"   💹 <i>Обсяг:</i> <code>{volume_text}</code>\n"
                f"   📊 <i>Зміна:</i> {change_text}\n"
            )
        
        await loading_msg.edit_text("\n".join(lines), parse_mode="HTML")
        
    except Exception as e:
        logger.error(f"Error in topgainers_cmd: {e}")
        await loading_msg.edit_text("⚠️ Помилка при отриманні даних")

@dp.message(Command("setcriteria"))
async def set_criteria_cmd(message: types.Message):
    logger.info(f"Received /setcriteria from chat_id: {message.chat.id}")
    try:
        args = message.text.split()[1:]
        if len(args) != 3:
            await message.answer(
                "ℹ️ Використовуйте: /setcriteria MIN_VOLUME MIN_PRICE MIN_CHANGE\n"
                "Приклад: /setcriteria 1000 0.0001 5\n\n"
                "Поточні значення:\n"
                f"• Мін. обсяг: ${MIN_VOLUME:,}\n"
                f"• Мін. ціна: ${MIN_PRICE:.6f}\n"
                f"• Мін. зміна: {MIN_PRICE_CHANGE}%"
            )
            return
        
        # Оголошуємо глобальні змінні
        global MIN_VOLUME, MIN_PRICE, MIN_PRICE_CHANGE
        MIN_VOLUME = int(args[0])
        MIN_PRICE = float(args[1])
        MIN_PRICE_CHANGE = float(args[2])
        
        await message.answer(
            f"✅ Критерії оновлено:\n"
            f"• Мін. обсяг: ${MIN_VOLUME:,}\n"
            f"• Мін. ціна: ${MIN_PRICE:.6f}\n"
            f"• Мін. зміна: {MIN_PRICE_CHANGE}%"
        )
        
    except Exception as e:
        await message.answer(f"⚠️ Помилка: {str(e)}")

@dp.message(Command("testscan"))
async def test_scan_cmd(message: types.Message):
    """Команда для тестового сканування"""
    logger.info(f"Received /testscan from chat_id: {message.chat.id}")
    
    test_msg = await message.answer("🧪 Запускаю тестове сканування...")
    
    try:
        anomalies = await analyze_coins(message.chat.id)
        
        if anomalies:
            result = f"✅ Знайдено {len(anomalies)} аномалій!\n\n"
            for i, coin in enumerate(anomalies[:5], 1):
                result += (f"{i}. {coin['name']} ({coin['symbol']})\n"
                          f"   Ціна: ${coin['current_price']:.6f}\n"
                          f"   Зміна: {coin['price_change_percentage_24h']:.1f}%\n"
                          f"   Обсяг: ${coin['total_volume']:,.0f}\n\n")
            
            await test_msg.edit_text(result)
        else:
            await test_msg.edit_text(
                "❌ Тестове сканування не знайшло аномалій.\n\n"
                "Перевірте:\n"
                "1. Доступність Jupiter API\n"
                "2. Параметри пошуку (зміна >5%, обсяг >$1000)\n"
                "3. Спробуйте змінити критерії /setcriteria"
            )
            
    except Exception as e:
        logger.error(f"Test scan error: {e}")
        await test_msg.edit_text(f"⚠️ Помилка тестового сканування: {str(e)}")

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    return await start_cmd(message)

# ------------------------------------------------------------------------------
# WEBHOOK + LIFESPAN
# ------------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        logger.info(f"Attempting to set webhook to {WEBHOOK_URL}")
        await bot.set_webhook(WEBHOOK_URL, secret_token=WEBHOOK_SECRET)
        logger.info(f"Webhook successfully set to {WEBHOOK_URL}")
        webhook_info = await bot.get_webhook_info()
        logger.info(f"Webhook info: {webhook_info}")
        asyncio.create_task(clear_cache_task())
    except Exception as e:
        logger.error(f"Failed to set webhook: {e}")
        logger.info("Falling back to polling mode")
        asyncio.create_task(dp.start_polling(bot))
    yield
    try:
        await bot.delete_webhook()
        logger.info("Webhook deleted")
    except Exception as e:
        logger.error(f"Failed to delete webhook: {e}")

app.lifespan = lifespan

# ------------------------------------------------------------------------------
# ROUTES
# ------------------------------------------------------------------------------
@app.get("/")
async def root():
    return {"message": "Ultimaster Bot is running!"}

@app.head("/")
async def root_head():
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

# ------------------------------------------------------------------------------
# RUN
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
