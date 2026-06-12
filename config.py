"""Центральная конфигурация проекта Uzum_tools.

Все настройки читаются из переменных окружения (опционально через .env),
значения по умолчанию подобраны под боевой контур Uzum Seller OpenAPI.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("config")

try:  # необязательная зависимость — если установлен python-dotenv, подхватим .env
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


# --------------------------------------------------------------------------- #
#  Пути проекта
# --------------------------------------------------------------------------- #
BASE_DIR: Path = Path(__file__).resolve().parent
DATA_DIR: Path = BASE_DIR / "data"
LOG_DIR: Path = BASE_DIR / "logs"

DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- #
#  Интернационализация (i18n): ru / uz / en
# --------------------------------------------------------------------------- #
# GNU gettext через aiogram.utils.i18n — переводы компилируются в .mo и читаются
# из памяти процесса (ноль запросов к БД на горячем пути). Каталоги:
#   locales/<lang>/LC_MESSAGES/messages.po (.mo)
# Воркфлоу: pybabel extract → update → перевод → compile (см. базу знаний).
from aiogram.utils.i18n import I18n

LOCALES_DIR: Path = BASE_DIR / "locales"
SUPPORTED_LOCALES: tuple[str, ...] = ("ru", "uz", "en")
DEFAULT_LOCALE: str = "ru"

i18n = I18n(path=LOCALES_DIR, default_locale=DEFAULT_LOCALE, domain="messages")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    return value.strip() if isinstance(value, str) else value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip().isdigit() else default


def _env_int_list(name: str) -> list[int]:
    raw = _env(name, "") or ""
    return [int(x) for x in raw.replace(" ", "").split(",") if x.lstrip("-").isdigit()]


# --------------------------------------------------------------------------- #
#  Telegram-бот
# --------------------------------------------------------------------------- #
# Токен самого бота — строго из окружения, без хардкода.
TELEGRAM_BOT_TOKEN: str | None = _env("TELEGRAM_BOT_TOKEN")

# Ключ шифрования токенов Uzum (Fernet). Если пуст — utils/crypto сгенерирует
# новый при первом обращении, допишет в .env и предупредит в логах.
ENCRYPTION_KEY: str | None = _env("ENCRYPTION_KEY")

# Telegram-id администраторов (через запятую в .env) — доступ к /stats.
ADMIN_IDS: list[int] = _env_int_list("ADMIN_IDS")

# Супергруппа техподдержки с включёнными топиками (Forum). Бот должен быть админом
# с правом «Manage Topics» и иметь Privacy Mode = OFF (чтобы видеть ответы админов).
# Формат id: -100XXXXXXXXXX. 0 → поддержка не настроена (хендлеры мягко сообщают).
def _parse_support_chat_id() -> int:
    """ID супергруппы поддержки. ВАЖНО: он отрицательный (-100…), поэтому обычный
    _env_int (через .isdigit()) его бы отбросил — парсим со знаком и предупреждаем."""
    raw = (os.getenv("SUPPORT_CHAT_ID") or "").strip()
    if not raw:
        _log.warning(
            "SUPPORT_CHAT_ID не задан (=0) — техподдержка ОТКЛЮЧЕНА. Пропишите id "
            "супергруппы с топиками в .env: SUPPORT_CHAT_ID=-100…"
        )
        return 0
    try:
        value = int(raw)
    except ValueError:
        _log.warning("SUPPORT_CHAT_ID=%r не является числом — поддержка отключена.", raw)
        return 0
    if value == 0:
        _log.warning("SUPPORT_CHAT_ID=0 — техподдержка ОТКЛЮЧЕНА (нужен id -100…).")
    elif value > 0:
        if str(value).startswith("100"):
            # Юзер вписал -100… без минуса (1001245866204) → автоисправляем.
            _log.info(
                "🔧 Автоисправление: SUPPORT_CHAT_ID указан как 100..., преобразую в "
                "отрицательный супергрупповой ID: -%s", value,
            )
            value = -value
        else:
            _log.warning(
                "SUPPORT_CHAT_ID=%s указан БЕЗ префикса -100…: для супергрупп нужен минус "
                "(например, -100%s). С положительным id бот не сможет писать в группу.",
                value, value,
            )
    return value


SUPPORT_CHAT_ID: int = _parse_support_chat_id()

# Потокобезопасный (dict — атомарные операции на отдельных ключах в CPython) кэш
# глобальных настроек. Заполняется при старте бота из SystemSettings, чтобы
# InfrastructureShield не ходил в БД за maintenance_mode на каждый апдейт.
SYSTEM_CACHE: dict[str, str] = {"maintenance_mode": "false"}

# Провайдер платежей Telegram Payments (Click Terminal). Тестовый токен выдаёт
# @BotFather (/mybots → Payments) либо кабинет Click. Без него send_invoice не
# работает — биллинг-хендлеры отвечают «оплата не настроена». Читаем из env.
CLICK_PROVIDER_TOKEN: str = _env("CLICK_PROVIDER_TOKEN", "") or ""

# Базовая цена/срок (1 месяц) — фолбэк и опорная точка для оффера.
PREMIUM_PRICE_UZS: int = _env_int("PREMIUM_PRICE_UZS", 150_000)   # сум/мес
PREMIUM_DAYS: int = _env_int("PREMIUM_DAYS", 30)

# Тарифная сетка пакетов Premium (декларативно). Ключ пакета (1_month/…) маппится в
# billing._PLAN_VIEW: callback `sub:buy:premium_<key>`, payload инвойса `premium_<key>`,
# сумма инвойса = price_uzs×100 (тийины). days начисляются при успешной оплате; чем
# длиннее пакет — тем больше скидка (вшита в price_uzs).
PREMIUM_PACKAGES: dict[str, dict] = {
    "1_month":  {"days": 30,  "price_uzs": 150_000,  "label": "Premium 1 месяц",      "discount": "0%"},
    "3_months": {"days": 90,  "price_uzs": 380_000,  "label": "Premium 3 месяца",     "discount": "15%"},
    "6_months": {"days": 180, "price_uzs": 675_000,  "label": "Premium 6 месяцев",    "discount": "25%"},
    "1_year":   {"days": 365, "price_uzs": 1_170_000, "label": "Premium 1 год (-35%)", "discount": "35%"},
}


# --------------------------------------------------------------------------- #
#  Авторизация (Uzum API)
# --------------------------------------------------------------------------- #
# По спецификации securityScheme называется "TokenAuth":
#   type: apiKey, in: header, name: Authorization
#   ВАЖНО: токен передаётся БЕЗ префикса "Bearer".
AUTH_HEADER_NAME: str = "Authorization"
AUTH_TOKEN: str | None = _env("UZUM_API_TOKEN")
# Префикс оставлен пустым намеренно — Uzum ждёт «голый» токен.
AUTH_TOKEN_PREFIX: str = ""


# --------------------------------------------------------------------------- #
#  HTTP / API
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class APISettings:
    base_url: str = "https://api-seller.uzum.uz/api/seller-openapi"
    # Машиночитаемая OpenAPI-схема (для генерации/валидации, не для боевых вызовов)
    openapi_schema_url: str = (
        "https://api-seller.uzum.uz/api/seller-openapi/swagger/api-docs"
    )

    timeout: float = 30.0          # секунды на запрос
    max_retries: int = 4           # повторы при 429/5xx/сетевых ошибках
    backoff_factor: float = 0.8    # экспоненциальная пауза между повторами
    default_accept_language: str = "ru"  # ru | uz

    # Лимиты пагинации, продиктованные API.
    # ВНИМАНИЕ: лимиты разные по эндпоинтам:
    #   /v2/fbs/orders, /v1/return  → size ≤ 50
    #   /v1/fbs/invoice             → size ≤ 20 (maximum:20 в схеме)
    page_size: int = 50
    max_page_size: int = 50
    invoice_max_page_size: int = 20

    # Rate limiting. Uzum возвращает заголовки X-RateLimit-* (token bucket
    # с пополнением в секунду). Клиент адаптивно подстраивается под
    # X-RateLimit-Replenish-Rate из ответов (см. _RateLimiter). Стартовая
    # скорость = ИЗМЕРЕННЫЙ лимит /v1/shops (диагностика 2026-06-12,
    # utils/check_api_health.py): Burst-Capacity=2, Replenish-Rate=2/с.
    # Прежний старт 3.0 превышал лимит и ловил 429 до адаптации.
    requests_per_second: float = 2.0


API = APISettings()


# --------------------------------------------------------------------------- #
#  Эндпоинты (относительно API.base_url)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Endpoints:
    # --- Магазины / организация ---
    shops: str = "/v1/shops"

    # --- FBS заказы (v2) ---
    fbs_orders: str = "/v2/fbs/orders"
    fbs_orders_count: str = "/v2/fbs/orders/count"
    fbs_order_detail: str = "/v1/fbs/order/{order_id}"
    fbs_order_labels_print: str = "/v1/fbs/order/{order_id}/labels/print"
    fbs_order_return_reasons: str = "/v1/fbs/order/return-reasons"

    # --- FBS накладные ---
    fbs_invoice: str = "/v1/fbs/invoice"
    fbs_invoice_detail: str = "/v1/fbs/invoice/{invoice_id}"
    fbs_invoice_orders: str = "/v1/fbs/invoice/{invoice_id}/orders"
    fbs_invoice_print: str = "/v1/fbs/invoice/{invoice_id}/print"
    fbs_invoice_closing_docs: str = "/v1/fbs/invoice/{invoice_id}/closing-documents"

    # --- Возвраты ---
    returns: str = "/v1/return"
    shop_returns: str = "/v1/shop/{shop_id}/return"
    shop_return_detail: str = "/v1/shop/{shop_id}/return/{return_id}"

    # --- Каталог товаров/SKU (штрихкоды + остаток FBO из quantityActive) ---
    product_shop: str = "/v1/product/shop/{shop_id}"

    # --- Остатки SKU по схеме FBS/DBS ---
    # ЧТЕНИЕ остатков — GET /v3/fbs/sku/stocks (конверт payload.skuAmountList[], поле
    # `amount`). Это авторитетный источник fbs_stock, точнее снимка quantityFbs.
    fbs_sku_stocks: str = "/v3/fbs/sku/stocks"
    # ЗАПИСЬ (обновление) остатков — POST /v2/fbs/sku/stocks. ВНИМАНИЕ: по живой
    # OpenAPI-схеме Uzum метод POST есть ТОЛЬКО на v2; на /v3/fbs/sku/stocks
    # доступен лишь GET. Тело — SkuStockUpdateApiRequestDto {skuAmountList:[{skuId,
    # barcode(req), amount(req), …}]}. Когда Uzum опубликует POST на v3 — поменять
    # тут одну строку на "/v3/fbs/sku/stocks".
    fbs_sku_stocks_update: str = "/v2/fbs/sku/stocks"

    # --- Финансы (баланс/выплаты) ---
    finance_orders: str = "/v1/finance/orders"
    finance_expenses: str = "/v1/finance/expenses"


ENDPOINTS = Endpoints()


# --------------------------------------------------------------------------- #
#  Публичный каталог Uzum Market (анализ конкурентов, без авторизации)
# --------------------------------------------------------------------------- #
# Базовый URL публичного API маркетплейса. ВНИМАНИЕ: публичные эндпоинты Uzum
# не документированы и могут меняться/требовать заголовков → парсер защитный,
# при недоступности фича деградирует (показывает свой балл + примечание).
UZUM_PUBLIC_API_BASE: str = (
    _env("UZUM_PUBLIC_API_BASE") or "https://api.uzum.uz/api/v2"
)
# GraphQL-эндпоинт публичного каталога (фолбэк-путь поиска, если REST закрыт).
UZUM_PUBLIC_GRAPHQL: str = (
    _env("UZUM_PUBLIC_GRAPHQL") or "https://api.uzum.uz/api/graphql/"
)
# Web-версия каталога (парсинг публичных HTML-страниц вместо закрытого app-API).
UZUM_WEB_BASE: str = _env("UZUM_WEB_BASE") or "https://uzum.uz/ru"
# Ротируемый прокси для парсера (http://user:pass@ip:port). Пусто → без прокси.
PROXY_URL: str = os.getenv("PROXY_URL", "")


# --------------------------------------------------------------------------- #
#  База данных (PostgreSQL) + Redis (FSM)
# --------------------------------------------------------------------------- #
# Прод (highload): PostgreSQL через синхронный psycopg3.
#   DATABASE_URL=postgresql+psycopg://user:pass@host:5432/uzum
# Локалка / CI без поднятого Postgres — дефолт SQLite (тот же файл data/uzum.db),
# чтобы dry-run и тесты работали без внешних сервисов. Прод ОБЯЗАН задать env.
# (Поддержан и UZUM_DB_URL как legacy-алиас.)
DATABASE_URL: str = (
    _env("DATABASE_URL")
    or _env("UZUM_DB_URL")
    or f"sqlite:///{DATA_DIR / 'uzum.db'}"
)

# Redis для FSM-состояний aiogram (RedisStorage) → бот stateless, можно запускать
# несколько инстансов за балансировщиком без потери контекста диалогов.
REDIS_URL: str = _env("REDIS_URL") or "redis://localhost:6379/0"


@dataclass(frozen=True, slots=True)
class DatabaseSettings:
    url: str = DATABASE_URL
    echo: bool = bool(int(_env("UZUM_DB_ECHO", "0") or "0"))


DATABASE = DatabaseSettings()


# --------------------------------------------------------------------------- #
#  Логирование
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class LogSettings:
    level: str = _env("UZUM_LOG_LEVEL", "INFO") or "INFO"
    file: Path = LOG_DIR / "uzum_tools.log"
    max_bytes: int = 5 * 1024 * 1024  # 5 МБ на файл
    backup_count: int = 7             # хранить 7 ротаций


LOG = LogSettings()
