import ccxt
import asyncio
from config import EXCHANGES
from security.api_manager import api_manager

class BinanceClient:
    def __init__(self):
        api_key, secret = api_manager.load_secure_keys('binance')
        if not api_key or not secret:
            raise ValueError("Binance API keys not found")
        
        self.exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': secret,
            **EXCHANGES['binance']
        })
    
    async def get_price(self, symbol: str) -> float:
        """Отримання поточної ціни"""
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            return float(ticker['last'])
        except Exception as e:
            print(f"Binance price error: {e}")
            return 0.0
    
    async def get_balance(self, asset: str = 'USDT') -> float:
        """Отримання балансу"""
        try:
            balance = await self.exchange.fetch_balance()
            return float(balance['total'].get(asset, 0))
        except Exception as e:
            print(f"Binance balance error: {e}")
            return 0.0
    
    async def get_orderbook(self, symbol: str, depth: int = 5) -> dict:
        """Отримання стакану цін"""
        try:
            orderbook = await self.exchange.fetch_order_book(symbol, depth)
            return {
                'bids': orderbook['bids'][:depth],
                'asks': orderbook['asks'][:depth]
            }
        except Exception as e:
            print(f"Binance orderbook error: {e}")
            return {'bids': [], 'asks': []}

# Глобальний екземпляр
binance_client = BinanceClient()