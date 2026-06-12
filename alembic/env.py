"""Alembic-окружение Uzum Tools.

Источник истины по схеме — database.models.Base.metadata; URL базы динамически
подтягивается из config.DATABASE_URL (env DATABASE_URL / UZUM_DB_URL / фолбэк
SQLite data/uzum.db), поэтому одна команда работает и на проде (PostgreSQL),
и на деве:  alembic upgrade head
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# prepend_sys_path = .  (alembic.ini) → корень проекта в sys.path, импорты работают.
from config import DATABASE_URL
from database.models import Base
from utils.crypto import EncryptedToken

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Метаданные моделей проекта — для autogenerate.
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):  # noqa: ANN001
    """Кастомный рендер типов в автогенерируемых миграциях.

    EncryptedToken — TypeDecorator(impl=String): шифрование живёт в Python-слое,
    для DDL это обычный VARCHAR. Рендерим sa.String, чтобы миграции оставались
    самодостаточными (без импорта utils.crypto и, как следствие, ENCRYPTION_KEY).
    """
    if type_ == "type" and isinstance(obj, EncryptedToken):
        return f"sa.String(length={obj.impl.length})"
    return False  # дефолтный рендер для остальных типов


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (генерация SQL без подключения к БД)."""
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (живое подключение из config.DATABASE_URL)."""
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_item=render_item,
            # SQLite не умеет большинство ALTER TABLE — batch-режим («пересоздать
            # таблицу рядом») делает миграции переносимыми между прод и дев.
            render_as_batch=connection.dialect.name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
