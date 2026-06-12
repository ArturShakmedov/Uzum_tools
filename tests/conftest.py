"""Бутстрап тестов: временная SQLite-БД ДО импорта модулей проекта.

database.connection создаёт engine при импорте из config.DATABASE_URL, поэтому
переменная окружения подменяется здесь — conftest pytest импортирует раньше
любых тестов. Схема строится боевым путём: init_db() → alembic upgrade head.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Корень проекта в sys.path (pytest сам его не добавляет).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_db_path = tempfile.mkstemp(prefix="uzum_tests_", suffix=".db")[1]
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    """Один раз на прогон: схема через боевой миграционный контур Alembic."""
    from database.connection import init_db

    init_db()


@pytest.fixture()
def db_session():
    """Свежая сессия на тест (commit/rollback — как в проде через session_scope)."""
    from database.connection import session_scope

    with session_scope() as session:
        yield session
