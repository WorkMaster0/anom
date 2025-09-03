import os
from dotenv import load_dotenv

load_dotenv()

# Налаштування бота
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

# Налаштування бірж
EXCHANGES = {
    'binance': {
        'apiKey': os.getenv('BINANCE_API_KEY'),
        'secret': os.getenv('BINANCE_SECRET'),
        'enableRateLimit': True,
        'options': {
            'defaultType': 'spot',
            'createOrder': False,  # Тільки читання!
            'withdraw': False      # Тільки читання!
        }
    },
    'bybit': {
        'apiKey': os.getenv('BYBIT_API_KEY'),
        'secret': os.getenv('BYBIT_SECRET'),
        'enableRateLimit': True,
        'options': {
            'defaultType': 'spot',
            'createOrder': False,
            'withdraw': False
        }
    }
}

# Налаштування арбітражу
ARBITRAGE_SETTINGS = {
    'min_spread': 0.3,      # Мінімальний спред 0.3%
    'max_spread': 5.0,      # Максимальний спред 5.0%
    'timeframe': '1m',      # Таймфрейм аналізу
    'symbols': ['BTC/USDT', 'ETH/USDT', 'BNB/USDT', 'SOL/USDT', 'XRP/USDT'],
    'refresh_interval': 10  # Секунд між перевірками
}

# Налаштування ризиків
RISK_SETTINGS = {
    'daily_loss_limit': -5.0,    # -5% в день
    'max_trade_size': 0.2,       # 20% від балансу
    'cooldown_period': 30,       # 30 секунд між угодами
    'max_positions': 5           # Макс. кількість одночасних угод
}

# Налаштування безпеки
SECURITY_SETTINGS = {
    'ip_whitelist': ['192.168.1.1'],  # IP вашого сервера
    'encryption_key': os.getenv('ENCRYPTION_KEY'),
    'require_confirmation': True      # Потрібне підтвердження угод
}