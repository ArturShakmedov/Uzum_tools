"""Настройка логирования с ротацией файлов."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from config import LOG

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging() -> None:
    """Инициализировать корневой логгер (консоль + ротируемый файл). Идемпотентно."""
    global _configured
    if _configured:
        return

    root = logging.getLogger()
    root.setLevel(LOG.level)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG.file,
        maxBytes=LOG.max_bytes,
        backupCount=LOG.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # httpx/urllib3 болтливы на DEBUG — приглушаем до WARNING.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """Получить именованный логгер, гарантируя инициализацию."""
    setup_logging()
    return logging.getLogger(name)


__all__ = ["setup_logging", "get_logger"]
