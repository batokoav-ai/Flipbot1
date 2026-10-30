"""Конфигурация флип-бота Portals.

Значения можно задать прямо здесь или через переменные окружения:
    export BOT_TOKEN="123:ABC"
    export OWNER_ID="123456789"
"""
import os

# ---------------- Обязательные ----------------
BOT_TOKEN = "9224314435:AAHUHEYQNAdTjIPPpujfLYJHNhZkSp2Ybl0"
OWNER_ID = 7966050135

# Комиссия маркета в % (вычитается из флора при расчёте чистой прибыли)
MARKET_FEE_PERCENT = float(os.getenv("MARKET_FEE_PERCENT", "2"))

# Начальный authData (строка "tma query_id=...&user=...&hash=...").
# Можно оставить пустым и задать позже командой /auth или кнопкой в боте.
PORTALS_AUTH_DATA = os.getenv("PORTALS_AUTH_DATA", "")

# ---------------- API Portals ----------------
# Эндпоинт поиска лотов (по документации публичных библиотек: GET + заголовок Authorization).
API_BASE = "https://portal-market.com/api"
SEARCH_PATH = "/nfts/search"
SEARCH_METHOD = "GET"          # "GET" или "POST" (если Portals изменит API)
IMPERSONATE = "chrome110"      # обязательно для обхода Cloudflare
HTTP_TIMEOUT = 25
PAGE_LIMIT = 50
BACKOFF_STEPS = (30, 60, 120)  # паузы при 429 (сек)

# Прямая ссылка на лот
LOT_LINK_TEMPLATE = "https://t.me/portals/market?startapp=gift_{gift_id}"
# Ссылка на подарок в самом Telegram (просмотр NFT)
TG_NFT_LINK_TEMPLATE = "https://t.me/nft/{slug}"

# ---------------- Прочее ----------------
DB_PATH = os.getenv("DB_PATH", "portals_bot.db")
MAX_IMAGES_PER_SCAN = 80       # лимит анализа картинок за один цикл
IMAGE_CONCURRENCY = 4          # одновременных загрузок картинок

# Значения настроек по умолчанию (хранятся в SQLite, меняются из меню бота)
DEFAULTS = {
    "scan_enabled": "1",
    "floor_enabled": "1",
    "mono_enabled": "1",
    "floor_rel_pct": "-5",       # цена лота относительно флора, %: -10 = на 10% ниже, +10 = на 10% выше
    "require_profit": "1",       # требовать чистую прибыль > 0
    "max_age_min": "15",         # брать лоты не старше N минут (0 = любые)
    "min_profit": "0.1",         # мин. чистая прибыль, TON
    "mono_threshold": "85",      # порог схожести цвета, %
    "mono_use_rel": "1",         # ограничивать цену монохрома относительно флора
    "mono_rel_pct": "10",        # макс. цена монохрома к флору, %: +10 = до 10% выше флора, -5 = на 5% ниже
    "min_price": "0",
    "max_price": "0",            # 0 = без лимита
    "scan_interval": "60",       # сек
    "scan_pages": "1",           # страниц по 50 лотов
    "scan_sort": "latest",       # latest | price_asc | both
    "signal_mode": "any",        # any | all
    "max_signals": "10",         # макс. сигналов за один цикл
    "request_delay": "1.0",      # пауза между запросами, сек
    "auth_data": PORTALS_AUTH_DATA,
}
