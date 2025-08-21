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

# КРИТЕРІЇ (можна правити командою /setcriteria)
MIN_CAP = int(os.getenv("MIN_CAP", "100000000"))      # $100M (залишив для сумісності, але не використовуємо)
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "10000"))     # $10K
MAX_VOLUME = int(os.getenv("MAX_VOLUME", "100000000")) # $100M
MIN_PRICE_CHANGE = float(os.getenv("MIN_PRICE_CHANGE", "20"))  # 20% (може бути відсутня в респонсі — враховано)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "600"))       # 10 хвилин
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "20"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задано")

# ------------------------------------------------------------------------------
# Jupiter Lite API (без ключа)
# ------------------------------------------------------------------------------
JUP_BASE = "https://lite-api.jup.ag"
TOKEN_V2_BASE = f"{JUP_BASE}/tokens/v2"
PRICE_V3_BASE = f"{JUP_BASE}/price/v3"  # додаємо ?ids=...

# Внутрішній ліміт (Lite має обмеження; тримаємося консервативно)
MAX_REQUESTS_PER_MINUTE = 18  # дві «категорії» + recent + 1-2 прайс батча з запасом
PER_REQUEST_DELAY = 0.35      # затримка між зверненнями, щоб не впиратись у 429

# ------------------------------------------------------------------------------
# Ініціалізація
# ------------------------------------------------------------------------------
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

# ------------------------------------------------------------------------------
# СТАН
# ------------------------------------------------------------------------------
alert_cache = TTLCache(maxsize=2000, ttl=86400)  # надіслані алерти (уник дублів)
active_monitoring: Dict[int, bool] = {}
latest_anomalies: List[Dict[str, Any]] = []

# Рейт-ліміт
request_lock = asyncio.Lock()
request_count = 0
request_count_reset_time = 0.0
rate_limit_exceeded = False
last_rate_limit_time = 0.0

# Кеші
recent_cache = TTLCache(maxsize=2, ttl=300)     # 5 хв
category_cache = TTLCache(maxsize=10, ttl=180)  # 3 хв
price_cache = TTLCache(maxsize=5000, ttl=60)    # 60 с

# ------------------------------------------------------------------------------
# УТИЛІТИ HTTP
# ------------------------------------------------------------------------------
async def _sleep_delay():
    await asyncio.sleep(PER_REQUEST_DELAY)

def _now_ts() -> float:
    return datetime.now().timestamp()

async def _rate_guard() -> Tuple[bool, int]:
    """
    Повертає (ok, wait_seconds). Якщо False — треба зачекати wait_seconds.
    """
    global request_count, request_count_reset_time, rate_limit_exceeded, last_rate_limit_time

    now = _now_ts()

    # Скидаємо лічильник кожні 60 с
    if now - request_count_reset_time > 60:
        request_count = 0
        request_count_reset_time = now
        logger.info("Reset request count")

    # Якщо ми у періоді «карантину» після 429 — чекаємо
    if rate_limit_exceeded and (now - last_rate_limit_time) < 30:  # 30 с backoff
        remaining = int(30 - (now - last_rate_limit_time))
        return False, remaining

    if request_count >= MAX_REQUESTS_PER_MINUTE:
        # чекаємо до кінця хвилини
        remaining = int(60 - (now - request_count_reset_time))
        return False, max(remaining, 1)

    return True, 0

async def _http_get_json(session: aiohttp.ClientSession, url: str, params: Dict[str, Any] | None = None, timeout: int = 15) -> Dict[str, Any] | List[Any]:
    """
    GET з урахуванням внутрішнього ліміту та backoff на 429.
    """
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
    """
    category: toptraded | toptrending | toporganicscore
    interval: 5m | 1h | 6h | 24h
    """
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
    """
    Price API V3: GET https://lite-api.jup.ag/price/v3?ids=addr1,addr2,...
    Повертаємо dict mint->price (USD).
    """
    result: Dict[str, float] = {}
    # забираємо з кешу частину
    to_fetch = []
    for m in mints:
        if m in price_cache:
            result[m] = price_cache[m]
        else:
            to_fetch.append(m)

    if not to_fetch:
        return result

    # батчами по 80-100
    batch_size = 90
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(to_fetch), batch_size):
            batch = to_fetch[i:i + batch_size]
            params = {"ids": ",".join(batch)}
            data = await _http_get_json(session, PRICE_V3_BASE, params)
            # очікуваний формат: {"data": {"mint": {"price": ...}, ...}, ...}
            data_map = {}
            if isinstance(data, dict):
                data_map = data.get("data", {}) or {}
            for mint, val in data_map.items():
                price = 0.0
                if isinstance(val, dict):
                    # пробуємо різні можливі ключі
                    price = float(val.get("price") or val.get("priceUsd") or 0.0)
                result[mint] = price
                price_cache[mint] = price

    return result

# ------------------------------------------------------------------------------
# НОРМАЛІЗАЦІЯ ДАНИХ
# ------------------------------------------------------------------------------
def normalize_token(obj: Dict[str, Any]) -> Dict[str, Any]:
    """
    Різні ендпоінти можуть вертати різні ключі. Зводимо до спільного вигляду.
    """
    mint = obj.get("mint") or obj.get("address") or obj.get("id") or ""
    name = obj.get("name") or obj.get("tokenName") or "Unknown"
    symbol = (obj.get("symbol") or obj.get("tokenSymbol") or "UNK").upper()
    logo = obj.get("logoURI") or obj.get("logo") or ""
    # спроби витягти обсяг і зміни, якщо вони є
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
        "current_price": 0.0,                # додамо пізніше
        "market_cap": 0,                     # Jupiter V2 це зазвичай не віддає
        "total_volume": vol,                 # якщо нема — буде 0
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
    """
    Логіка:
      1) беремо «нові» токени (recent)
      2) беремо категорії toptraded(24h) і toptrending(1h) для контексту
      3) зливаємо, додаємо ціни
      4) фільтруємо за обсягом + (зміна ціни >= MIN_PRICE_CHANGE або входить у toptrending)
    """
    anomalies: List[Dict[str, Any]] = []

    # 1) recent
    recent_raw = await jup_get_recent(limit=80)
    recent = [normalize_token(x) for x in recent_raw if isinstance(x, dict)]
    if not recent:
        logger.info("No recent tokens or API error")
        return anomalies

    # 2) categories
    toptraded_raw = await jup_get_category("toptraded", interval="24h", limit=200)
    toptrending_raw = await jup_get_category("toptrending", interval="1h", limit=200)
    toptraded_map: Dict[str, Dict[str, Any]] = {}
    for x in toptraded_raw:
        if isinstance(x, dict):
            m = x.get("mint") or x.get("address")
            if m:
                toptraded_map[m] = x
    toptrending_set = set()
    for x in toptrending_raw:
        if isinstance(x, dict):
            m = x.get("mint") or x.get("address")
            if m:
                toptrending_set.add(m)

    # 3) prices for recent
    mints = [t["id"] for t in recent if t["id"]]
    price_map = await jup_get_prices(mints)
    merge_price(recent, price_map)

    # 4) enrich recent with toptraded metrics if exist
    enriched: List[Dict[str, Any]] = []
    for r in recent:
        src = toptraded_map.get(r["id"], {})
        # якщо у категорії є кращі поля для volume / change — підставляємо
        vol = (
            src.get("volumeUSD") or src.get("traded24hUSD") or
            src.get("volume_usd") or r["total_volume"] or 0
        )
        chg = (
            src.get("priceChange24hPct") or src.get("price_change_24h") or
            src.get("pctChange24h") or r["price_change_percentage_24h"] or 0
        )
        try:
            r["total_volume"] = float(vol or 0)
        except Exception:
            r["total_volume"] = 0.0
        try:
            r["price_change_percentage_24h"] = float(chg or 0)
        except Exception:
            r["price_change_percentage_24h"] = 0.0

        r["trending"] = (r["id"] in toptrending_set)
        enriched.append(r)

    # 5) anomalies
    for coin in enriched:
        volume = coin.get("total_volume", 0) or 0
        change = coin.get("price_change_percentage_24h", 0) or 0
        is_trending = bool(coin.get("trending", False))
        # умови:
        if (MIN_VOLUME < volume < MAX_VOLUME) and (change >= MIN_PRICE_CHANGE or is_trending):
            anomalies.append(coin)
            logger.info(f"Found anomaly: {coin['name']} (Volume: {volume:,.0f}, Change: {change:.1f}%, trending={is_trending})")

    logger.info(f"Found {len(anomalies)} anomalies")
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
        f"💹 Обсяг (кат.): <code>${vol:,.0f}</code>",
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
    while active_monitoring.get(chat_id):
        try:
            anomalies = await analyze_coins(chat_id)
            if not anomalies:
                logger.info("No anomalies found or API error occurred")
                if not rate_limit_exceeded:
                    await bot.send_message(chat_id, "ℹ️ Немає аномальних токенів або ще немає даних. Спробую знов за 10 хв.")
            else:
                sent = 0
                for coin in anomalies:
                    if coin["id"] not in alert_cache and sent < MAX_ALERTS_PER_CYCLE:
                        await send_alert(chat_id, coin)
                        sent += 1
        except Exception as e:
            logger.error(f"monitoring_task error: {e}")
            await bot.send_message(chat_id, f"⚠️ Помилка моніторингу: {str(e)}. Повторю спробу.")
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
        "• /topvol — топ за обсягом (категорія)\n"
        "• /topgainers — топ «ростучих» (категорія)\n"
        "• /setcriteria — налаштувати критерії\n"
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
    await message.answer(f"📊 Статус: {stat}\n🔍 У кеші сповіщень: {len(alert_cache)}")

@dp.message(Command("latest"))
async def latest_cmd(message: types.Message):
    logger.info(f"Received /latest from chat_id: {message.chat.id}")
    if latest_anomalies:
        lines = ["🔍 Останні аномальні токени:"]
        for coin in latest_anomalies[:20]:
            lines.append(f"• {escape(coin['name'])} ({escape(coin['symbol'])})")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ Аномалій ще немає")

@dp.message(Command("topvol"))
async def topvol_cmd(message: types.Message):
    logger.info(f"Received /topvol from chat_id: {message.chat.id}")
    raw = await jup_get_category("toptraded", interval="24h", limit=100)
    tokens = [normalize_token(x) for x in raw if isinstance(x, dict)]
    if tokens:
        # сортуємо за total_volume (normalize_token вже постарався витягти)
        tokens.sort(key=lambda t: float(t.get("total_volume", 0) or 0), reverse=True)
        lines = ["💹 Топ за обсягом (категорія 24h):"]
        for t in tokens[:20]:
            lines.append(f"• {escape(t['name'])} ({escape(t['symbol'])}) — ${float(t.get('total_volume',0)):,.0f}")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Порожньо — немає даних категорії.")

@dp.message(Command("topgainers"))
async def topgainers_cmd(message: types.Message):
    logger.info(f"Received /topgainers from chat_id: {message.chat.id}")
    # У V2 є toptrending — вважаємо як ростучі; якщо ціна% є — покажемо
    raw = await jup_get_category("toptrending", interval="24h", limit=100)
    tokens = [normalize_token(x) for x in raw if isinstance(x, dict)]
    if tokens:
        # якщо є відсотки — сортуємо за ними, інакше як є
        tokens.sort(key=lambda t: float(t.get("price_change_percentage_24h", 0) or 0), reverse=True)
        lines = ["🚀 Топ «ростучих» (категорія 24h):"]
        for t in tokens[:20]:
            pct = float(t.get("price_change_percentage_24h", 0) or 0)
            suffix = f" — +{pct:.1f}%" if pct != 0 else ""
            lines.append(f"• {escape(t['name'])} ({escape(t['symbol'])}){suffix}")
        await message.answer("\n".join(lines))
    else:
        await message.answer("ℹ️ Порожньо — немає даних категорії.")

@dp.message(Command("setcriteria"))
async def set_criteria_cmd(message: types.Message):
    logger.info(f"Received /setcriteria from chat_id: {message.chat.id}")
    try:
        args = message.text.split()[1:]
        if len(args) != 3:
            await message.answer("ℹ️ Використовуйте: /setcriteria MIN_CAP MIN_VOLUME MIN_PRICE_CHANGE\nПриклад: /setcriteria 50000000 5000 10")
            return
        global MIN_CAP, MIN_VOLUME, MIN_PRICE_CHANGE
        MIN_CAP = int(args[0])
        MIN_VOLUME = int(args[1])
        MIN_PRICE_CHANGE = float(args[2])
        await message.answer(
            f"✅ Критерії оновлено:\n"
            f"MIN_CAP: ${MIN_CAP:,}\nMIN_VOLUME: ${MIN_VOLUME:,}\nMIN_PRICE_CHANGE: {MIN_PRICE_CHANGE}%"
        )
    except Exception as e:
        await message.answer(f"⚠️ Помилка: {str(e)}")

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
