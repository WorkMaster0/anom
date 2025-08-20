import aiohttp
import asyncio
from datetime import datetime
import logging
from cachetools import TTLCache
from .config import MOBULA_API_KEY, MIN_CAP, MIN_VOLUME, MIN_PRICE_CHANGE

log = logging.getLogger("ultimaster-bot")

last_fetch_time = 0
last_coins_data = []

async def fetch_mobula_cached():
    global last_fetch_time, last_coins_data
    now = datetime.now().timestamp()
    if now - last_fetch_time < 600 and last_coins_data:
        return last_coins_data

    url = "https://api.mobula.io/v1/market/multi-data"
    headers = {"Authorization": f"Bearer {MOBULA_API_KEY}"}
    params = {"assets": "all", "limit": 500, "offset": 0}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    raw_assets = data.get("data", [])
                    formatted_data = []
                    for asset in raw_assets:
                        coin = {
                            "id": asset.get("id", ""),
                            "name": asset.get("name", ""),
                            "symbol": asset.get("symbol", ""),
                            "market_cap": asset.get("market_cap", 0),
                            "total_volume": asset.get("volume_24h", 0),
                            "current_price": asset.get("price", 0),
                            "price_change_percentage_24h": asset.get("price_change_24h", 0) * 100
                        }
                        formatted_data.append(coin)
                    last_coins_data = formatted_data
                    last_fetch_time = now
                    return formatted_data
                elif resp.status == 429:
                    log.warning("Mobula rate limit 429, retry in 60s")
                    await asyncio.sleep(60)
                    return await fetch_mobula_cached()
                else:
                    log.error(f"Mobula status {resp.status}")
    except Exception as e:
        log.error(f"Помилка запиту Mobula: {e}")

    return last_coins_data

async def analyze_coins():
    coins = await fetch_mobula_cached()
    anomalies = []
    for coin in coins:
        market_cap = coin.get('market_cap', 0)
        volume = coin.get('total_volume', 0)
        price_change = coin.get('price_change_percentage_24h', 0)

        if market_cap < MIN_CAP and volume > MIN_VOLUME and price_change > MIN_PRICE_CHANGE:
            anomalies.append(coin)
    return anomalies

# TTL Cache для сповіщень
alert_cache = TTLCache(maxsize=2000, ttl=86400)
