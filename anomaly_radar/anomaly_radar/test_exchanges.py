import ccxt

EXCHANGES = [
    "binance",
    "mexc",
    "bitget",
    "bybit",
    "phemex",
    "xt",
    "lbank",
    "kucoin",
]

for exchange_id in EXCHANGES:
    try:
        exchange_class = getattr(ccxt, exchange_id)
        exchange = exchange_class({
            "enableRateLimit": True,
        })

        exchange.load_markets()

        print(f"✅ {exchange_id}: {len(exchange.markets)} markets")

    except Exception as e:
        print(f"❌ {exchange_id}: {type(e).__name__}: {e}")
