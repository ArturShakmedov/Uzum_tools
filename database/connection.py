"""Управление подключением к БД (SQLAlchemy 2.0).

Прод — PostgreSQL через синхронный psycopg3 (`postgresql+psycopg://…`) с пулом
соединений. Слой сессий остаётся синхронным; параллелизм записи обеспечивает сам
PostgreSQL (MVCC), а сервисы уже исполняются в `asyncio.to_thread`. SQLite оставлен
ТОЛЬКО как дефолт для локалки/CI без поднятого Postgres.

Эволюция схемы — ТОЛЬКО Alembic (alembic/, базовая ревизия init_enterprise_schema).
Самописные SQLite-хаки миграций (additive-колонки/индексы, UPPER(role), backfill)
удалены: init_db() приводит любую БД к head через `alembic upgrade head`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config import BASE_DIR, DATABASE_URL, DATABASE
from utils.logger import get_logger

log = get_logger(__name__)

_IS_SQLITE = DATABASE_URL.startswith("sqlite")

# Пул соединений настраиваем для боевой СУБД (PostgreSQL). На каждый воркер-поток
# (asyncio.to_thread) берётся своё соединение из пула — запись разных юзеров идёт
# ПАРАЛЛЕЛЬНО (MVCC PostgreSQL), без глобального write-семафора.
if _IS_SQLITE:
    # Дев/CI-фолбэк: file-SQLite + многопоточный доступ из to_thread.
    _engine_kwargs: dict = {"connect_args": {"check_same_thread": False}}
else:
    _engine_kwargs = {
        "pool_size": 20,        # постоянных соединений в пуле
        "max_overflow": 10,     # временных сверх пула на пиках
        "pool_recycle": 3600,   # пересоздавать соединение раз в час (против stale)
        "pool_pre_ping": True,  # проверять живость соединения перед выдачей
    }

engine: Engine = create_engine(
    DATABASE_URL,
    echo=DATABASE.echo,
    future=True,
    **_engine_kwargs,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Привести схему БД к актуальной ревизии (`alembic upgrade head`).

    Один код-путь для прода (PostgreSQL) и дева (SQLite): и пустая база, и база
    на старой ревизии доводятся до head миграциями Alembic.

    Bridge для баз, созданных ДО внедрения Alembic (таблицы есть, alembic_version
    нет): их схема уже соответствует базовой ревизии — помечаем `stamp head`
    разово, без повторного наката DDL. Удалить после перевода всех окружений.
    """
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(BASE_DIR / "alembic.ini"))
    # Абсолютный путь к скриптам: программный запуск не зависит от CWD процесса.
    cfg.set_main_option("script_location", str(BASE_DIR / "alembic"))

    tables = set(inspect(engine).get_table_names())
    if "users" in tables and "alembic_version" not in tables:
        log.warning(
            "БД создана до внедрения Alembic — выполняю разовый `stamp head` "
            "(схема уже соответствует базовой ревизии)."
        )
        command.stamp(cfg, "head")

    command.upgrade(cfg, "head")
    log.info("Схема БД на актуальной ревизии Alembic (upgrade head выполнен).")


@contextmanager
def session_scope() -> Iterator[Session]:
    """Транзакционная область видимости: commit при успехе, rollback при ошибке."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = ["engine", "SessionLocal", "init_db", "session_scope"]
