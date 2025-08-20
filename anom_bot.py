# anom_bot.py
import sqlite3
import requests
import time
import json
import logging
import random
import re
from datetime import datetime

# ===== НАЛАШТУВАННЯ =====
TOKEN = "8063113740:AAGC-9PHzZD65jPad2lxP5mTmlWuQwvKwrU"
DB_NAME = "anom_bot.db"
COINGECKO_API_URL = "https://api.coingecko.com/api/v3"
REQUEST_LIMIT = 30
REQUEST_DELAY = 60 / REQUEST_LIMIT
last_request_time = 0

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

price_cache = {}
CACHE_TIME = 300
alerts = {}
subscriptions = {}

# ===== ІНІЦІАЛІЗАЦІЯ БД =====
def init_db():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id INTEGER,
            coin_id TEXT,
            coin_name TEXT,
            PRIMARY KEY (chat_id, coin_id))
        """)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            chat_id INTEGER,
            coin_id TEXT,
            operator TEXT,
            target_price REAL,
            triggered BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)
        """)
        conn.commit()
        logging.info("База даних ініціалізована")
        return True
    except sqlite3.Error as e:
        logging.error(f"Помилка БД: {e}")
        return False
    finally:
        if conn: conn.close()

# ===== API =====
def telegram_request(method, params):
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/{method}"
        response = requests.post(url, json=params, timeout=15)
        return response.json()
    except Exception as e:
        logging.error(f"Telegram API error: {e}")
        return None

def send_message(chat_id, text):
    params = {'chat_id': chat_id, 'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
    return telegram_request("sendMessage", params)

def coingecko_request(endpoint, params=None):
    global last_request_time
    try:
        elapsed = time.time() - last_request_time
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        url = f"{COINGECKO_API_URL}/{endpoint}"
        response = requests.get(url, params=params, timeout=15)
        last_request_time = time.time()
        if response.status_code != 200:
            raise Exception(f"API Error {response.status_code}: {response.text}")
        return response.json()
    except Exception as e:
        logging.error(f"CoinGecko API error: {e}")
        return None

# ===== ДОПОМІЖНІ ФУНКЦІЇ =====
def get_price(coin_id):
    now = time.time()
    if coin_id in price_cache and now - price_cache[coin_id]['timestamp'] < CACHE_TIME:
        return price_cache[coin_id]['price']
    
    data = coingecko_request(f"simple/price", {'ids': coin_id, 'vs_currencies': 'usd'})
    if not data or coin_id not in data:
        raise Exception("Монету не знайдено")
    
    price = data[coin_id]['usd']
    price_cache[coin_id] = {'price': price, 'timestamp': now}
    return price

def format_number(num):
    if num >= 1_000_000_000:
        return f"{num/1_000_000_000:.2f}B"
    elif num >= 1_000_000:
        return f"{num/1_000_000:.2f}M"
    elif num >= 1_000:
        return f"{num/1_000:.2f}K"
    else:
        return f"{num:.2f}"

def coin_icon(symbol):
    icons = {
        "BTC": "₿", "ETH": "◊", "BNB": "🔶", "ADA": "🅰️", 
        "XRP": "🌊", "DOGE": "🐶", "SHIB": "🦊", "PEPE": "🐸",
        "SOL": "🔆", "DOT": "🔴", "AVAX": "❄️", "MATIC": "🔷"
    }
    return icons.get(symbol.upper(), "💎")

# ===== КОМАНДИ ДЛЯ АНОМАЛЬНИХ ТОКЕНІВ =====
def handle_start(chat_id):
    msg = (
        "🚀 <b>Anom Token Hunter Bot</b> 🔍\n\n"
        "Спеціалізований бот для пошуку аномальних токенів з малою капіталізацією\n\n"
        "📊 <b>Основні команды:</b>\n"
        "• /scan - Сканування аномальних токенів\n"
        "• /lowcap - Топ малокапіталізаційних токенів\n"
        "• /volume - Токени з аномальним об'ємом торгів\n"
        "• /price [id] - Ціна токена (напр: bitcoin)\n"
        "• /info [id] - Детальна інформація про токен\n"
        "• /subscribe [id] - Підписка на токен\n"
        "• /unsubscribe [id] - Відписка\n"
        "• /mysubs - Мої підписки\n"
        "• /alert [id][> або <][ціна] - Налаштування сповіщення\n"
        "• /help - Допомога"
    )
    send_message(chat_id, msg)

def handle_help(chat_id):
    msg = (
        "🆘 <b>Допомога по командам бота</b>\n\n"
        "🔍 <b>Пошук аномалій:</b>\n"
        "• /scan - Сканує ринок на аномальні токени\n"
        "• /lowcap - Топ 10 малокапіталізаційних токенів з потенціалом\n"
        "• /volume - Токени з незвично високим об'ємом торгів\n\n"
        "📈 <b>Інформація:</b>\n"
        "• /price bitcoin - Ціна BTC\n"
        "• /info ethereum - Деталі про Ethereum\n\n"
        "🔔 <b>Сповіщення:</b>\n"
        "• /subscribe solana - Підписатися на Solana\n"
        "• /alert bitcoin>50000 - Сповіщення при ціні >50000\n"
        "• /mysubs - Перегляд підписок"
    )
    send_message(chat_id, msg)

def handle_scan_anomalies(chat_id):
    try:
        send_message(chat_id, "🔍 Сканування ринку на аномальні токени...")
        
        # Отримуємо дані про топ 200 токенів
        data = coingecko_request("coins/markets", {
            'vs_currency': 'usd',
            'order': 'market_cap_desc',
            'per_page': 200,
            'page': 1,
            'price_change_percentage': '1h,24h,7d'
        })
        
        if not data:
            send_message(chat_id, "❌ Не вдалося отримати дані")
            return
        
        # Фільтруємо малокапіталізаційні токени (1M - 100M)
        low_cap_tokens = [token for token in data if 1_000_000 <= token['market_cap'] <= 100_000_000]
        
        # Шукаємо аномалії: висока зміна ціни + високий об'єм
        anomalies = []
        for token in low_cap_tokens:
            price_change_24h = token.get('price_change_percentage_24h', 0)
            volume_ratio = token.get('total_volume', 0) / token.get('market_cap', 1)
            
            # Критерії аномалії: зміна > 20% і об'єм > 10% від капіталізації
            if abs(price_change_24h) > 20 and volume_ratio > 0.1:
                anomalies.append(token)
        
        if not anomalies:
            send_message(chat_id, "📊 Аномальних токенів не знайдено. Спробуйте пізніше.")
            return
        
        # Сортуємо за об'ємом торгів
        anomalies.sort(key=lambda x: x['total_volume'] / x['market_cap'], reverse=True)
        
        msg = ["🚨 <b>Знайдено аномальні токени:</b>\n"]
        for i, token in enumerate(anomalies[:5], 1):
            change_24h = token.get('price_change_percentage_24h', 0)
            volume_ratio = (token['total_volume'] / token['market_cap']) * 100
            
            msg.append(
                f"{i}. {coin_icon(token['symbol'])} <b>{token['symbol'].upper()}</b>\n"
                f"   💰 Ціна: ${token['current_price']:.6f}\n"
                f"   📈 Зміна 24h: {change_24h:+.2f}%\n"
                f"   💼 Капа: ${format_number(token['market_cap'])}\n"
                f"   📊 Об'єм: {volume_ratio:.1f}% від капи\n"
                f"   🔗 /info_{token['id']}\n"
            )
        
        send_message(chat_id, "\n".join(msg))
        
    except Exception as e:
        logging.error(f"Scan error: {e}")
        send_message(chat_id, "❌ Помилка при скануванні")

def handle_lowcap_tokens(chat_id):
    try:
        data = coingecko_request("coins/markets", {
            'vs_currency': 'usd',
            'order': 'market_cap_asc',  # Сортуємо за зростанням капіталізації
            'per_page': 50,
            'page': 1,
            'price_change_percentage': '24h'
        })
        
        if not data:
            send_message(chat_id, "❌ Не вдалося отримати дані")
            return
        
        # Фільтруємо токени з капою 100K - 10M
        promising_tokens = [
            token for token in data 
            if 100_000 <= token['market_cap'] <= 10_000_000 and token['total_volume'] > 10_000
        ]
        
        if not promising_tokens:
            send_message(chat_id, "📊 Потенційних токенів не знайдено")
            return
        
        # Сортуємо за зростанням ціни
        promising_tokens.sort(key=lambda x: x.get('price_change_percentage_24h', 0), reverse=True)
        
        msg = ["💎 <b>Топ малокапіталізаційних токенів:</b>\n"]
        for i, token in enumerate(promising_tokens[:10], 1):
            change_24h = token.get('price_change_percentage_24h', 0)
            
            msg.append(
                f"{i}. {coin_icon(token['symbol'])} <b>{token['symbol'].upper()}</b>\n"
                f"   💰 Ціна: ${token['current_price']:.8f}\n"
                f"   📈 Зміна: {change_24h:+.2f}%\n"
                f"   💼 Капа: ${format_number(token['market_cap'])}\n"
                f"   🔗 /info_{token['id']}\n"
            )
        
        send_message(chat_id, "\n".join(msg))
        
    except Exception as e:
        logging.error(f"Lowcap error: {e}")
        send_message(chat_id, "❌ Помилка при пошуку токенів")

def handle_volume_anomalies(chat_id):
    try:
        data = coingecko_request("coins/markets", {
            'vs_currency': 'usd',
            'order': 'volume_desc',
            'per_page': 100,
            'page': 1
        })
        
        if not data:
            send_message(chat_id, "❌ Не вдалося отримати дані")
            return
        
        # Шукаємо токени з об'ємом > 50% від капіталізації
        volume_anomalies = []
        for token in data:
            if token['market_cap'] > 0:
                volume_ratio = token['total_volume'] / token['market_cap']
                if volume_ratio > 0.5 and token['market_cap'] < 50_000_000:
                    volume_anomalies.append((token, volume_ratio))
        
        if not volume_anomalies:
            send_message(chat_id, "📊 Аномалій об'єму не знайдено")
            return
        
        # Сортуємо за співвідношенням об'єму до капи
        volume_anomalies.sort(key=lambda x: x[1], reverse=True)
        
        msg = ["📊 <b>Токени з аномальним об'ємом торгів:</b>\n"]
        for i, (token, ratio) in enumerate(volume_anomalies[:8], 1):
            msg.append(
                f"{i}. {coin_icon(token['symbol'])} <b>{token['symbol'].upper()}</b>\n"
                f"   💰 Ціна: ${token['current_price']:.6f}\n"
                f"   💼 Капа: ${format_number(token['market_cap'])}\n"
                f"   📈 Об'єм: {ratio*100:.1f}% від капи\n"
                f"   🔗 /info_{token['id']}\n"
            )
        
        send_message(chat_id, "\n".join(msg))
        
    except Exception as e:
        logging.error(f"Volume anomaly error: {e}")
        send_message(chat_id, "❌ Помилка при пошуку аномалій")

def handle_price(chat_id, args):
    if not args:
        send_message(chat_id, "ℹ️ Введіть ID токена, наприклад: /price bitcoin")
        return
    
    coin_id = args[0].lower()
    try:
        price = get_price(coin_id)
        send_message(chat_id, f"{coin_icon(coin_id)} <b>{coin_id.upper()}</b>: ${price:.8f}")
    except Exception as e:
        send_message(chat_id, f"❌ Помилка: {e}")

def handle_info(chat_id, args):
    if not args:
        send_message(chat_id, "ℹ️ Введіть ID токена, наприклад: /info ethereum")
        return
    
    coin_id = args[0].lower()
    try:
        data = coingecko_request(f"coins/{coin_id}")
        if not data:
            send_message(chat_id, "❌ Токен не знайдено")
            return
        
        market_data = data['market_data']
        msg = (
            f"{coin_icon(data['symbol'])} <b>{data['name']} ({data['symbol'].upper()})</b>\n\n"
            f"💰 <b>Ціна:</b> ${market_data['current_price']['usd']:.8f}\n"
            f"📈 <b>Зміна 24h:</b> {market_data['price_change_percentage_24h']:+.2f}%\n"
            f"💼 <b>Капіталізація:</b> ${format_number(market_data['market_cap']['usd'])}\n"
            f"📊 <b>Об'єм 24h:</b> ${format_number(market_data['total_volume']['usd'])}\n"
            f"🔢 <b>Ранг:</b> #{data['market_cap_rank'] or 'N/A'}\n\n"
            f"🌐 <b>Сайт:</b> {data['links']['homepage'][0] if data['links']['homepage'] else 'N/A'}\n"
            f"📖 <b>Опис:</b> {data['description']['en'][:200] + '...' if data['description']['en'] else 'Немає опису'}"
        )
        send_message(chat_id, msg)
    except Exception as e:
        logging.error(f"Info error: {e}")
        send_message(chat_id, "❌ Помилка при отриманні інформації")

# ===== СИСТЕМА ПІДПИСОК ТА СПОВІЩЕНЬ =====
def handle_subscribe(chat_id, args):
    if not args:
        send_message(chat_id, "ℹ️ Введіть ID токена для підписки")
        return
    
    coin_id = args[0].lower()
    try:
        # Перевіряємо чи токен існує
        price = get_price(coin_id)
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO subscriptions (chat_id, coin_id, coin_name) VALUES (?, ?, ?)",
            (chat_id, coin_id, coin_id)
        )
        conn.commit()
        conn.close()
        
        send_message(chat_id, f"✅ Підписано на {coin_id.upper()}")
    except Exception as e:
        send_message(chat_id, f"❌ Помилка: {e}")

def handle_unsubscribe(chat_id, args):
    if not args:
        send_message(chat_id, "ℹ️ Введіть ID токена для відписки")
        return
    
    coin_id = args[0].lower()
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM subscriptions WHERE chat_id = ? AND coin_id = ?",
            (chat_id, coin_id)
        )
        conn.commit()
        conn.close()
        
        send_message(chat_id, f"❌ Відписано від {coin_id.upper()}")
    except Exception as e:
        send_message(chat_id, f"❌ Помилка: {e}")

def handle_mysubs(chat_id):
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT coin_id FROM subscriptions WHERE chat_id = ?",
            (chat_id,)
        )
        subs = cursor.fetchall()
        conn.close()
        
        if not subs:
            send_message(chat_id, "📭 У вас немає активних підписок")
            return
        
        msg = ["📋 <b>Ваші підписки:</b>\n"]
        for i, (coin_id,) in enumerate(subs, 1):
            try:
                price = get_price(coin_id)
                msg.append(f"{i}. {coin_icon(coin_id)} {coin_id.upper()} - ${price:.8f}")
            except:
                msg.append(f"{i}. {coin_id.upper()} - ❌ Недоступний")
        
        send_message(chat_id, "\n".join(msg))
    except Exception as e:
        send_message(chat_id, f"❌ Помилка: {e}")

def handle_alert(chat_id, args):
    if not args:
        send_message(chat_id, "ℹ️ Приклад: /alert bitcoin>50000")
        return
    
    try:
        match = re.match(r"([a-zA-Z0-9]+)([<>])([0-9.]+)", args[0])
        if not match:
            send_message(chat_id, "❌ Неправильний формат. Приклад: bitcoin>50000")
            return
        
        coin_id, operator, target_price = match.groups()
        coin_id = coin_id.lower()
        target_price = float(target_price)
        
        # Перевіряємо чи токен існує
        get_price(coin_id)
        
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO alerts (chat_id, coin_id, operator, target_price) VALUES (?, ?, ?, ?)",
            (chat_id, coin_id, operator, target_price)
        )
        conn.commit()
        conn.close()
        
        send_message(chat_id, f"🚨 Сповіщення встановлено: {coin_id.upper()} {operator} ${target_price:.2f}")
    except Exception as e:
        send_message(chat_id, f"❌ Помилка: {e}")

def check_alerts():
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM alerts WHERE triggered = FALSE")
        active_alerts = cursor.fetchall()
        
        for alert in active_alerts:
            alert_id, chat_id, coin_id, operator, target_price, triggered, created_at = alert
            
            try:
                current_price = get_price(coin_id)
                should_trigger = False
                
                if operator == ">" and current_price > target_price:
                    should_trigger = True
                elif operator == "<" and current_price < target_price:
                    should_trigger = True
                
                if should_trigger:
                    cursor.execute(
                        "UPDATE alerts SET triggered = TRUE WHERE id = ?",
                        (alert_id,)
                    )
                    conn.commit()
                    
                    send_message(
                        chat_id,
                        f"🚨 <b>СПОВІЩЕННЯ</b>\n"
                        f"{coin_icon(coin_id)} {coin_id.upper()} {operator} ${target_price:.2f}\n"
                        f"Поточна ціна: ${current_price:.8f}\n"
                        f"Час: {datetime.now().strftime('%H:%M:%S')}"
                    )
                    
            except Exception as e:
                logging.error(f"Alert check error for {coin_id}: {e}")
                continue
        
        conn.close()
    except Exception as e:
        logging.error(f"Alert system error: {e}")

# ===== ОБРОБКА ОНОВЛЕНЬ =====
def process_updates(updates, last_update_id):
    new_last_id = last_update_id
    for update in updates.get('result', []):
        try:
            new_last_id = max(new_last_id, update['update_id'])
            message = update.get('message', {})
            chat_id = message.get('chat', {}).get('id')
            text = message.get('text', '').strip()
            
            if not text or not chat_id:
                continue
            
            # Обробка команд з підкресленням (наприклад, /info_bitcoin)
            if text.startswith('/info_'):
                coin_id = text[6:]
                handle_info(chat_id, [coin_id])
                continue
            
            parts = text.split()
            cmd = parts[0].lower()
            args = parts[1:] if len(parts) > 1 else []
            
            if cmd == "/start":
                handle_start(chat_id)
            elif cmd == "/help":
                handle_help(chat_id)
            elif cmd == "/scan":
                handle_scan_anomalies(chat_id)
            elif cmd == "/lowcap":
                handle_lowcap_tokens(chat_id)
            elif cmd == "/volume":
                handle_volume_anomalies(chat_id)
            elif cmd == "/price":
                handle_price(chat_id, args)
            elif cmd == "/info":
                handle_info(chat_id, args)
            elif cmd == "/subscribe":
                handle_subscribe(chat_id, args)
            elif cmd == "/unsubscribe":
                handle_unsubscribe(chat_id, args)
            elif cmd == "/mysubs":
                handle_mysubs(chat_id)
            elif cmd == "/alert":
                handle_alert(chat_id, args)
            else:
                send_message(chat_id, "❌ Невідома команда. /help")
                
        except Exception as e:
            logging.error(f"Processing update error: {e}")
    
    return new_last_id

def main():
    if not init_db():
        print("❌ Не вдалося ініціалізувати бота")
        return
    
    print("🚀 Anom Token Hunter Bot запущено!")
    last_update_id = 0
    
    while True:
        try:
            # Отримуємо оновлення
            updates = telegram_request("getUpdates", {
                'offset': last_update_id + 1,
                'timeout': 30
            })
            
            if updates and updates.get('ok'):
                last_update_id = process_updates(updates, last_update_id)
            
            # Перевіряємо сповіщення кожні 30 секунд
            check_alerts()
            time.sleep(1)
            
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
