from telegram import send_telegram


message = """🚨 ANOMALY RADAR TEST

Telegram connection works!

🔥 Scanner is ready.
"""

result = send_telegram(message)

print(result)
