"""Точка входа Telegram-бота (aiogram 3.x).

Запуск:  python bot.py
Требует переменную окружения TELEGRAM_BOT_TOKEN (см. .env).
"""

from __future__ import annotations

import argparse
import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage

from sqlalchemy import func, select

from config import REDIS_URL, TELEGRAM_BOT_TOKEN
from database.connection import init_db, session_scope
from database.models import UzumCategory
from handlers import register_handlers
from services.notification_worker import start_notifications
from utils.logger import get_logger

log = get_logger(__name__)


def _require_encryption_key() -> None:
    """Fail-fast: без ENCRYPTION_KEY токены в БД нечитаемы — стартовать нельзя.

    Раньше ключ генерировался «на лету» и дописывался в .env. В контейнере это
    означало, что при следующем рестарте без сохранённой переменной ключ менялся
    и ВСЕ токены пользователей становились нерасшифровываемыми. Теперь — жёсткий
    отказ запуска, ключ обязан быть задан в окружении заранее.
    """
    if not os.getenv("ENCRYPTION_KEY"):
        log.critical("ENCRYPTION_KEY не задан! Запуск невозможен.")
        raise SystemExit("CRITICAL: ENCRYPTION_KEY не задан! Запуск невозможен.")


def _check_categories_loaded() -> None:
    """Предупредить, если справочник комиссий пуст (калькулятор «ослепнет»)."""
    with session_scope() as session:
        count = session.execute(select(func.count()).select_from(UzumCategory)).scalar_one()
    if count == 0:
        log.warning(
            "⚠️ КРИТИЧЕСКАЯ ОШИБКА: Таблица uzum_categories пуста! "
            "Калькулятор юнит-экономики ослеп. Срочно запустите скрипт: "
            "python scripts/parse_commissions.py"
        )
    else:
        log.info("Справочник комиссий: %d категорий.", count)

# Заглушка валидного формата токена для dry-run (без выхода в сеть).
_DRY_RUN_TOKEN = "123456789:DRYRUNDRYRUNDRYRUNDRYRUNDRYRUNDRYRUN"


def _build_dispatcher() -> Dispatcher:
    # FSM-состояния — в Redis (а не в памяти процесса): бот становится stateless,
    # можно запускать несколько инстансов за балансировщиком, и пользователи не
    # теряют контекст диалога при переключении между ними. Клиент Redis ленивый —
    # реальное соединение открывается при первом обращении (polling), не на старте.
    storage = RedisStorage.from_url(REDIS_URL)
    dp = Dispatcher(storage=storage)
    register_handlers(dp)
    return dp


def dry_run() -> int:
    """Импорт + инициализация бота и роутеров без polling (проверка сборки)."""
    _require_encryption_key()  # fail-fast: без ключа токены нечитаемы
    init_db()
    _check_categories_loaded()
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN or _DRY_RUN_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = _build_dispatcher()
    routers = [r.name for r in dp.sub_routers]
    log.info("DRY-RUN OK: Bot создан, БД готова, роутеры зарегистрированы: %s", routers)
    print(f"DRY-RUN OK — routers: {routers}")
    return 0


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN (положите его в .env).")

    _require_encryption_key()  # fail-fast: без ключа токены нечитаемы — не стартуем
    init_db()
    _check_categories_loaded()
    # Прогрев кэша глобальных настроек (maintenance_mode) — Shield читает из памяти.
    from database.repository import load_maintenance_cache
    load_maintenance_cache()
    log.info("БД инициализирована, ключ шифрования задан.")

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = _build_dispatcher()

    # Live-лента уведомлений о новых заказах/возвратах — фоновая задача рядом
    # с polling (см. services/notification_worker). Запускаем под работающим loop.
    notifier = start_notifications(bot)

    log.info("Бот запущен (polling) + воркер Live-уведомлений.")
    try:
        await dp.start_polling(bot)
    finally:
        notifier.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Uzum_tools Telegram bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Проверить сборку (импорт + роутеры) без polling")
    args = parser.parse_args()

    if args.dry_run:
        raise SystemExit(dry_run())
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен.")
