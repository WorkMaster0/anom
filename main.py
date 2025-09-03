import telebot
import asyncio
import threading
from typing import Dict
from config import BOT_TOKEN, ADMIN_ID, ARBITRAGE_SETTINGS
from arbitrage.detector import arbitrage_detector
from risk_management.manager import risk_manager
from exchanges.binance_client import binance_client
from exchanges.bybit_client import bybit_client

# Ініціалізація бота
bot = telebot.TeleBot(BOT_TOKEN)

# Глобальний стан
active = False
opportunities_cache = []

@bot.message_handler(commands=['start'])
def start_command(message):
    """Команда старту"""
    bot.reply_to(message, "🚀 Арбітражний бот активовано!\n\n"
                         "Доступні команди:\n"
                         "/start - Початок роботи\n"
                         "/scan - Сканування арбітражу\n" 
                         "/balance - Перевірка балансу\n"
                         "/settings - Налаштування\n"
                         "/stop - Зупинка бота")

@bot.message_handler(commands=['scan'])
def scan_command(message):
    """Сканування арбітражних можливостей"""
    global active
    active = True
    
    bot.reply_to(message, "🔍 Сканування арбітражу...")
    
    # Запускаємо сканування в окремому потоці
    def scan_thread():
        asyncio.run(continuous_scan())
    
    threading.Thread(target=scan_thread, daemon=True).start()

@bot.message_handler(commands=['balance'])
def balance_command(message):
    """Перевірка балансу"""
    async def get_balances():
        binance_balance = await binance_client.get_balance()
        bybit_balance = await bybit_client.get_balance()
        
        response = (f"💰 Баланси:\n\n"
                   f"• Binance: {binance_balance:.2f} USDT\n"
                   f"• Bybit: {bybit_balance:.2f} USDT\n"
                   f"• Загалом: {binance_balance + bybit_balance:.2f} USDT")
        
        bot.reply_to(message, response)
    
    asyncio.run(get_balances())

@bot.message_handler(commands=['stop'])
def stop_command(message):
    """Зупинка бота"""
    global active
    active = False
    bot.reply_to(message, "⛔ Бот зупинено")

async def continuous_scan():
    """Безперервне сканування арбітражу"""
    global active, opportunities_cache
    
    while active:
        try:
            opportunities = await arbitrage_detector.find_arbitrage_opportunities()
            opportunities_cache = opportunities
            
            if opportunities:
                for opp in opportunities:
                    message = format_opportunity(opp)
                    bot.send_message(ADMIN_ID, message, parse_mode='HTML')
            
            await asyncio.sleep(ARBITRAGE_SETTINGS['refresh_interval'])
            
        except Exception as e:
            print(f"Scan error: {e}")
            await asyncio.sleep(5)

def format_opportunity(opportunity: Dict) -> str:
    """Форматування повідомлення про арбітраж"""
    return f"""🎯 <b>АРБІТРАЖ ЗНАЙДЕНО!</b>

• Токен: {opportunity['symbol']}
• Купити: {opportunity['buy_exchange']} - ${opportunity['buy_price']:.2f}
• Продати: {opportunity['sell_exchange']} - ${opportunity['sell_price']:.2f}
• Спред: <b>{opportunity['spread']:.2f}%</b>

💰 Потенційний прибуток: ~{opportunity['spread'] * 0.8:.2f}%
⏰ Час дії: ~2-5 хвилин

⚡ <i>Швидко дійте!</i>"""

if __name__ == "__main__":
    print("🚀 Арбітражний бот запущено...")
    bot.infinity_polling()