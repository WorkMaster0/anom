from dotenv import load_dotenv
import os

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
MOBULA_API_KEY = os.getenv("MOBULA_API_KEY", "").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

MIN_CAP = int(os.getenv("MIN_CAP", "100000000"))
MIN_VOLUME = int(os.getenv("MIN_VOLUME", "10000"))
MAX_VOLUME = int(os.getenv("MAX_VOLUME", "100000000"))
MIN_PRICE_CHANGE = float(os.getenv("MIN_PRICE_CHANGE", "20"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "20"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задано")
if not MOBULA_API_KEY:
    raise RuntimeError("MOBULA_API_KEY не задано")
