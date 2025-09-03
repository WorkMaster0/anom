import asyncio
from typing import Dict, List
from config import ARBITRAGE_SETTINGS
from exchanges.binance_client import binance_client
from exchanges.bybit_client import bybit_client

class ArbitrageDetector:
    def __init__(self):
        self.symbols = ARBITRAGE_SETTINGS['symbols']
        self.min_spread = ARBITRAGE_SETTINGS['min_spread']
        self.max_spread = ARBITRAGE_SETTINGS['max_spread']
    
    async def get_prices(self, symbol: str) -> Dict[str, float]:
        """Отримання цін з усіх бірж"""
        prices = {}
        
        # Отримуємо ціни паралельно
        tasks = [
            binance_client.get_price(symbol),
            bybit_client.get_price(symbol)
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        prices['binance'] = results[0] if not isinstance(results[0], Exception) else 0
        prices['bybit'] = results[1] if not isinstance(results[1], Exception) else 0
        
        return prices
    
    async def find_arbitrage_opportunities(self) -> List[Dict]:
        """Пошук арбітражних можливостей"""
        opportunities = []
        
        for symbol in self.symbols:
            prices = await self.get_prices(symbol)
            
            # Фільтруємо нульові ціни
            valid_prices = {k: v for k, v in prices.items() if v > 0}
            if len(valid_prices) < 2:
                continue
            
            # Знаходимо мін та макс ціни
            min_exchange = min(valid_prices, key=valid_prices.get)
            max_exchange = max(valid_prices, key=valid_prices.get)
            min_price = valid_prices[min_exchange]
            max_price = valid_prices[max_exchange]
            
            # Розраховуємо спред
            spread = ((max_price - min_price) / min_price) * 100
            
            if self.min_spread <= spread <= self.max_spread:
                opportunities.append({
                    'symbol': symbol,
                    'buy_exchange': min_exchange,
                    'sell_exchange': max_exchange,
                    'buy_price': min_price,
                    'sell_price': max_price,
                    'spread': spread,
                    'timestamp': asyncio.get_event_loop().time()
                })
        
        return opportunities

# Глобальний екземпляр
arbitrage_detector = ArbitrageDetector()