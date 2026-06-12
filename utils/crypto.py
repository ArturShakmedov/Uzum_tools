"""Модуль безопасности: шифрование токенов Uzum (Fernet, симметричное).

Токены пользователей не хранятся в открытом виде. Шифрование прозрачно
встроено в тип колонки SQLAlchemy (EncryptedToken): в Python всегда чистый
токен, в БД — шифртекст.

Ключ берётся из ENCRYPTION_KEY (окружение/.env). Если его нет — fail-fast
(RuntimeError): автогенерация ключа вырезана, чтобы рестарт без сохранённого
ключа не подменил его и не сделал все токены в БД нечитаемыми.
"""

from __future__ import annotations

import os
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

from utils.logger import get_logger

log = get_logger(__name__)


class TokenKeyMismatch(RuntimeError):
    """ENCRYPTION_KEY не соответствует зашифрованным данным в БД.

    Бросается, когда Fernet получает корректный шифртекст, но не может его
    расшифровать текущим ключом (ключ потерян/сменился). Раньше эта ситуация
    маскировалась под «legacy-токен» и тихо отдавала шифртекст как открытый
    токен — теперь это явная авария, а не молчаливая порча данных.
    """


def ensure_encryption_key() -> str:
    """Вернуть ключ шифрования из окружения. Без него — fail-fast.

    Автогенерация ключа ПОЛНОСТЬЮ вырезана (и для бота, и для CLI): раньше при
    отсутствии ключа генерировался новый и дописывался в .env, из-за чего рестарт
    контейнера без сохранённой переменной менял ключ и делал ВСЕ токены в БД
    нечитаемыми. Теперь ключ обязан быть задан в окружении заранее — иначе
    RuntimeError, не допускающий работу с заведомо некорректным шифрованием.
    """
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        log.critical("ENCRYPTION_KEY не обнаружен в окружении — работа невозможна.")
        raise RuntimeError("Критическая ошибка: ENCRYPTION_KEY не обнаружен в окружении!")
    return key


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(ensure_encryption_key().encode())


def encrypt_token(plain: str) -> str:
    """Зашифровать токен (str → str)."""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_token(token: str) -> str:
    """Расшифровать токен Fernet.

    • InvalidToken → шифртекст корректен, но ключ не подходит (ENCRYPTION_KEY
      потерян/сменился). Это авария: бросаем TokenKeyMismatch, НЕ маскируем.
    • ValueError → значение не в формате Fernet (legacy-plaintext, записанный до
      внедрения шифрования) — отдаём как есть для обратной совместимости.
    """
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        log.error("Токен не расшифрован текущим ENCRYPTION_KEY — ключ сменился/потерян.")
        raise TokenKeyMismatch(
            "Ключ шифрования ENCRYPTION_KEY не соответствует данным в БД"
        ) from exc
    except ValueError:
        # Не Fernet-формат — старый незашифрованный токен. Отдаём без изменений.
        log.warning("Токен не в формате Fernet — трактую как открытый (legacy).")
        return token


class EncryptedToken(TypeDecorator):
    """Прозрачный тип SQLAlchemy: шифрует при записи, дешифрует при чтении."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect) -> str | None:  # noqa: ANN001
        return encrypt_token(value) if value is not None else None

    def process_result_value(self, value: str | None, dialect) -> str | None:  # noqa: ANN001
        return decrypt_token(value) if value is not None else None


__all__ = [
    "ensure_encryption_key",
    "encrypt_token",
    "decrypt_token",
    "EncryptedToken",
    "TokenKeyMismatch",
]
