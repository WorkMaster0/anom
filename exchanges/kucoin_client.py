# exchanges/kucoin_client.py
import ccxt
import asyncio
from config import EXCHANGES
from security.api_manager import api_manager

class KuCoinClient:
    def __init__(self):
        api_key, secret = api_manager.load_secure_keys('kucoin')
        if not api_key or not secret:
            raise ValueError("KuCoin API keys not found")
        
        self.exchange = ccxt.kucoin({
            'apiKey': api_key,
            'secret': secret,
            'password': os.getenv('KUCOIN_PASSWORD'),
            **EXCHANGES['kucoin']
        })
    
    async def get_price(self, symbol: str) -> float:
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            return float(ticker['last'])
        except Exception as e:
            print(f"KuCoin price error: {e}")
            return 0.0
    
    async def get_balance(self, asset: str = 'USDT') -> float:
        try:
            balance = await self.exchange.fetch_balance()
            return float(balance['total'].get(asset, 0))
        except Exception as e:
            print(f"KuCoin balance error: {e}")
            return 0.0

# Глобальний екземпляр
kucoin_client = KuCoinClient()