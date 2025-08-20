# bott.py
import sqlite3
import requests
import time
import json
import logging
import random
import re

# ===== НАЛАШТУВАННЯ =====
TOKEN = "8255365352:AAHqFjtxNo02_b6bQwj2ieoFyDAkXmOW4oQ"
DB_NAME = "crypto_bot.db"
COINGECKO_API_URL = "https://api.coingecko.com/api/v3"
REQUEST_LIMIT = 30
REQUEST_DELAY = 60 / REQUEST_LIMIT
last_request_time = 0

logging.basicConfig(
    filename='crypto_bot.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

price_cache = {}
CACHE_TIME = 300
alerts = {}
subscriptions = {}

top_coins_cache = {'coins': [], 'timestamp': 0}
CACHE_TOP_TIME = 600  # 10 хв

# ===== ІНІЦІАЛІЗАЦІЯ БД =====
def init_db():
    conn = None
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id INTEGER,
            coin TEXT,
            PRIMARY KEY (chat_id, coin))
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
        response = requests.post(url, data=params, timeout=15)
        return response.json()
    except Exception as e:
        logging.error(f"Telegram API error: {e}")
        return None

def send_message(chat_id, text):
    params = {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True}
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

# ===== ДОПОМІЖНІ =====
def get_price(coin):
    now = time.time()
    coin = coin.lower()
    if coin in price_cache and now - price_cache[coin]['timestamp'] < CACHE_TIME:
        return price_cache[coin]['price']
    data = coingecko_request(f"simple/price", {'ids': coin, 'vs_currencies': 'usd'})
    if not data or coin not in data: raise Exception("Монету не знайдено")
    price_cache[coin] = {'price': data[coin]['usd'], 'timestamp': now}
    return data[coin]['usd']

def coin_icon(symbol):
    icons = {"BTC":"₿","ETH":"◊","DOGE":"🐶","SHIB":"🦊","PEPE":"🐸","ADA":"🅰️"}
    return icons.get(symbol.upper(), "💵")

# ===== КОМАНДИ =====
def handle_start(chat_id):
    msg = ("🪙 Крипто-Бот Ultimate 🔄\n"
           "💰 /price [монета] - Поточна ціна\n"
           "📈 /top - Топ монет за 24h\n"
           "📊 /change [монета] - Зміна за добу\n"
           "ℹ️ /info [монета] - Деталі монети\n"
           "✅ /subscribe [монета] - Підписка\n"
           "❌ /unsubscribe [монета] - Відписка\n"
           "📌 /mysubs - Мої підписки\n"
           "🚨 /alert [монета][> або <][ціна] - Сповіщення\n"
           "💼 /portfolio - Портфель\n"
           "🆘 /help - Допомога")
    send_message(chat_id, msg)

def handle_help(chat_id):
    msg = ("🆘 Допомога по команді бота 🆘\n"
           "💰 /price btc - Поточна ціна BTC\n"
           "📈 /top - Топ 5 зростання/падіння\n"
           "📊 /change eth - Зміна ціни ETH за 24h\n"
           "ℹ️ /info ada - Деталі монети ADA\n"
           "✅ /subscribe btc - Підписатися на BTC\n"
           "❌ /unsubscribe btc - Відписатися\n"
           "📌 /mysubs - Перегляд підписок\n"
           "🚨 /alert btc>50000 - Сповіщення про ціну\n"
           "💼 /portfolio - Портфель всіх підписок")
    send_message(chat_id, msg)

def handle_price(chat_id, args):
    if not args:
        send_message(chat_id, "ℹ️ Введіть монету, наприклад /price btc")
        return
    try:
        price = get_price(args[0])
        send_message(chat_id, f"{coin_icon(args[0])} {args[0].upper()}: ${price:.4f}")
    except Exception as e:
        send_message(chat_id, f"❌ Помилка: {e}")

def handle_top(chat_id):
    try:
        now = time.time()
        if not top_coins_cache['coins'] or now - top_coins_cache['timestamp'] > CACHE_TOP_TIME:
            all_coins = []
            for page in range(1, 21):  # 250*20=5000 монет
                data = coingecko_request("coins/markets", {
                    'vs_currency': 'usd',
                    'order': 'market_cap_desc',
                    'per_page': 250,
                    'page': page,
                    'price_change_percentage': '24h'
                })
                if data: all_coins.extend(data)
            top_coins_cache['coins'] = all_coins
            top_coins_cache['timestamp'] = now

        movers = [c for c in top_coins_cache['coins'] if c.get('price_change_percentage_24h')]
        positive = sorted([c for c in movers if c['price_change_percentage_24h']>0],
                          key=lambda x: x['price_change_percentage_24h'], reverse=True)[:5]
        negative = sorted([c for c in movers if c['price_change_percentage_24h']<0],
                          key=lambda x: x['price_change_percentage_24h'])[:5]

        msg = ["🚀 Топ 5 зростання 🟩"]
        for c in positive:
            msg.append(f"{coin_icon(c['symbol'])} {c['symbol'].upper()} ${c['current_price']:.4f} 🟩 {c['price_change_percentage_24h']:+.2f}%")

        msg.append("\n📉 Топ 5 падіння 🟥")
        for c in negative:
            msg.append(f"{coin_icon(c['symbol'])} {c['symbol'].upper()} ${c['current_price']:.4f} 🟥 {c['price_change_percentage_24h']:+.2f}%")

        send_message(chat_id, "\n".join(msg))
    except Exception as e:
        send_message(chat_id, f"❌ Помилка: {e}")

# ===== ІНШІ КОМАНДИ =====
def handle_change(chat_id, args):
    if not args: 
        send_message(chat_id,"ℹ️ Введіть монету")
        return
    coin=args[0].lower()
    data=coingecko_request("coins/markets",{'vs_currency':'usd','ids':coin})
    if not data: 
        send_message(chat_id,"❌ Не знайдено")
        return
    c=data[0]
    arrow="🔺" if c['price_change_percentage_24h']>0 else "🔻"
    send_message(chat_id,f"{coin_icon(c['symbol'])} {c['symbol'].upper()} (${c['current_price']:.4f})\nЗміна 24h: {arrow} {c['price_change_percentage_24h']:+.2f}%")

def handle_info(chat_id, args):
    if not args:
        send_message(chat_id,"ℹ️ Введіть монету")
        return
    coin=args[0].lower()
    data=coingecko_request(f"coins/{coin}")
    if not data: 
        send_message(chat_id,"❌ Не знайдено")
        return
    c=data
    price=c['market_data']['current_price']['usd']
    change=c['market_data']['price_change_percentage_24h']
    msg=(f"{coin_icon(c['symbol'])} {c['name']} ({c['symbol'].upper()})\n"
         f"💰 Ціна: ${price:.4f}\n📊 Зміна 24h: {change:+.2f}%\n"
         f"💹 Ринок: ${c['market_data']['market_cap']['usd']:,}\n"
         f"📦 Обсяг: ${c['market_data']['total_volume']['usd']:,}\n"
         f"🔗 Сайт: {c['links']['homepage'][0]}")
    send_message(chat_id,msg)

def handle_subscribe(chat_id, args):
    if not args: 
        send_message(chat_id,"ℹ️ Введіть монету")
        return
    coin=args[0].lower()
    subscriptions.setdefault(chat_id,set()).add(coin)
    send_message(chat_id,f"✅ Підписано на {coin.upper()}")

def handle_unsubscribe(chat_id, args):
    if not args: 
        send_message(chat_id,"ℹ️ Введіть монету")
        return
    coin=args[0].lower()
    subscriptions.setdefault(chat_id,set()).discard(coin)
    send_message(chat_id,f"❌ Відписано від {coin.upper()}")

def handle_mysubs(chat_id,args):
    subs=subscriptions.get(chat_id,set())
    if not subs: send_message(chat_id,"ℹ️ Немає підписок"); return
    send_message(chat_id,"📌 Ваші підписки: "+", ".join([s.upper() for s in subs]))

def handle_alert(chat_id,args):
    if not args: send_message(chat_id,"ℹ️ Приклад: /alert btc>50000"); return
    m=re.match(r"([a-zA-Z0-9]+)([<>])([0-9.]+)",args[0])
    if not m: send_message(chat_id,"ℹ️ Неправильний формат"); return
    coin,op,target=m.group(1).lower(),m.group(2),float(m.group(3))
    alerts.setdefault(chat_id,[]).append({'coin':coin,'operator':op,'target':target,'triggered':False})
    send_message(chat_id,f"🚨 Сповіщення встановлено: {coin.upper()} {op} {target}")

def check_alerts():
    for chat_id,user_alerts in list(alerts.items()):
        for alert in user_alerts[:]:
            try:
                price=get_price(alert['coin'])
                cond=(alert['operator']==">" and price>alert['target']) or (alert['operator']=="<" and price<alert['target'])
                if cond and not alert['triggered']:
                    send_message(chat_id,f"🚨 {alert['coin'].upper()} {alert['operator']} {alert['target']}\nПоточна ціна: ${price:.2f}")
                    alert['triggered']=True
                    user_alerts.remove(alert)
            except Exception as e: logging.error(f"Alert error: {e}")

def handle_portfolio(chat_id):
    subs=subscriptions.get(chat_id,set())
    if not subs: send_message(chat_id,"ℹ️ Немає монет для портфеля"); return
    total=0
    lines=[]
    for coin in subs:
        try:
            price=get_price(coin)
            lines.append(f"{coin_icon(coin)} {coin.upper()}: ${price:.4f}")
            total+=price
        except: continue
    lines.append(f"💰 Загальна вартість: ${total:.2f}")
    send_message(chat_id,"\n".join(lines))

# ===== ЦИКЛ =====
def process_updates(updates,last_update_id):
    new_last_id=last_update_id
    for update in updates.get('result',[]):
        try:
            new_last_id=max(new_last_id,update['update_id'])
            msg=update.get('message',{})
            chat_id=msg.get('chat',{}).get('id')
            text=msg.get('text','').strip()
            if not text or not chat_id: continue
            parts=text.split()
            cmd=parts[0].lower()
            args=parts[1:] if len(parts)>1 else []

            if cmd=="/start": handle_start(chat_id)
            elif cmd=="/help": handle_help(chat_id)
            elif cmd=="/price": handle_price(chat_id,args)
            elif cmd=="/top": handle_top(chat_id)
            elif cmd=="/change": handle_change(chat_id,args)
            elif cmd=="/info": handle_info(chat_id,args)
            elif cmd=="/subscribe": handle_subscribe(chat_id,args)
            elif cmd=="/unsubscribe": handle_unsubscribe(chat_id,args)
            elif cmd=="/mysubs": handle_mysubs(chat_id,args)
            elif cmd=="/alert": handle_alert(chat_id,args)
            elif cmd=="/portfolio": handle_portfolio(chat_id)
            else: send_message(chat_id,"❌ Невідома команда. /help")
        except Exception as e: logging.error(f"Processing update error: {e}")
    return new_last_id

def main():
    if not init_db(): 
        print("❌ Не вдалося ініціалізувати бота")
        return
    print("🟢 Бот запущено")
    last_update_id=0
    while True:
        try:
            updates=telegram_request("getUpdates",{'offset':last_update_id+1,'timeout':30})
            if updates: last_update_id=process_updates(updates,last_update_id)
            check_alerts()
            time.sleep(1)
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            time.sleep(5)

if __name__=="__main__":
    main()