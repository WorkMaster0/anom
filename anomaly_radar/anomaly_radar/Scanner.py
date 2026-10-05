import ccxt
import time

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

MIN_SPREAD = 2.0       # показувати від 2%
ORDERBOOK_LIMIT = 5


def load_exchanges():
    result = {}

    for exchange_id in EXCHANGES:
        try:
            exchange_class = getattr(ccxt, exchange_id)

            exchange = exchange_class({
                "enableRateLimit": True,
            })

            exchange.load_markets()

            result[exchange_id] = exchange

            print(f"✅ {exchange_id}: {len(exchange.markets)} markets")

        except Exception as e:
            print(f"❌ {exchange_id}: {e}")

    return result


def get_spot_symbols(exchange):
    symbols = set()

    for symbol, market in exchange.markets.items():
        if (
            market.get("spot")
            and market.get("active", True)
            and market.get("quote") == "USDT"
        ):
            symbols.add(symbol)

    return symbols


def get_common_symbols(exchanges):
    symbol_sets = []

    for exchange in exchanges.values():
        symbol_sets.append(get_spot_symbols(exchange))

    if not symbol_sets:
        return set()

    common = set.intersection(*symbol_sets)

    return common


def get_best_prices(exchange, symbol):
    try:
        orderbook = exchange.fetch_order_book(
            symbol,
            ORDERBOOK_LIMIT
        )

        if not orderbook["bids"] or not orderbook["asks"]:
            return None

        best_bid = orderbook["bids"][0]
        best_ask = orderbook["asks"][0]

        return {
            "bid": float(best_bid[0]),
            "bid_amount": float(best_bid[1]),
            "ask": float(best_ask[0]),
            "ask_amount": float(best_ask[1]),
        }

    except Exception:
        return None


def scan(exchanges):
    print("\n🔎 SCANNING...\n")

    common_symbols = get_common_symbols(exchanges)

    print(f"Common USDT spot symbols: {len(common_symbols)}")

    opportunities = []

    for symbol in common_symbols:

        prices = {}

        for exchange_id, exchange in exchanges.items():

            data = get_best_prices(exchange, symbol)

            if data:
                prices[exchange_id] = data

        if len(prices) < 2:
            continue

        # Найкраща ціна, де можна КУПИТИ
        cheapest_exchange = min(
            prices,
            key=lambda x: prices[x]["ask"]
        )

        # Найкраща ціна, де можна ПРОДАТИ
        expensive_exchange = max(
            prices,
            key=lambda x: prices[x]["bid"]
        )

        buy_price = prices[cheapest_exchange]["ask"]
        sell_price = prices[expensive_exchange]["bid"]

        if buy_price <= 0:
            continue

        spread = (
            (sell_price - buy_price)
            / buy_price
            * 100
        )

        if spread >= MIN_SPREAD:

            opportunities.append({
                "symbol": symbol,
                "buy_exchange": cheapest_exchange,
                "buy_price": buy_price,
                "buy_amount": prices[cheapest_exchange]["ask_amount"],
                "sell_exchange": expensive_exchange,
                "sell_price": sell_price,
                "sell_amount": prices[expensive_exchange]["bid_amount"],
                "spread": spread,
            })

    opportunities.sort(
        key=lambda x: x["spread"],
        reverse=True
    )

    print("\n🔥 TOP OPPORTUNITIES\n")

    for opportunity in opportunities[:30]:

        print(
            f"{opportunity['symbol']:20} "
            f"BUY {opportunity['buy_exchange']:8} "
            f"{opportunity['buy_price']:.8g} → "
            f"SELL {opportunity['sell_exchange']:8} "
            f"{opportunity['sell_price']:.8g} "
            f"| SPREAD {opportunity['spread']:.2f}% "
            f"| BUY SIZE ${opportunity['buy_price'] * opportunity['buy_amount']:.2f} "
            f"| SELL SIZE ${opportunity['sell_price'] * opportunity['sell_amount']:.2f}"
        )

    return opportunities


if __name__ == "__main__":

    print("🚀 ANOMALY RADAR")
    print("================")

    exchanges = load_exchanges()

    while True:

        try:
            scan(exchanges)

        except KeyboardInterrupt:
            print("\nStopped.")
            break

        except Exception as e:
            print(f"\n⚠️ Scanner error: {e}")

        print("\nWaiting 30 seconds...\n")

        time.sleep(30)