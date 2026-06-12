"""Общий механизм «свежих данных» для отчётов и финансов.

ensure_fresh() проверяет активный магазин, анти-спам и 30-мин кэш last_sync_at;
при устаревании запускает двухфазный синк run_full_sync (сеть параллельно под
per-user lock + FETCH_SEMAPHORE, запись параллельно — MVCC Postgres). Возвращает
статус-сообщение,
которое хендлер дальше правит под свой результат, либо None — если работу нужно
прервать (пользователю уже отправлено сообщение).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InputMediaPhoto, Message

from database.connection import session_scope
from database.repository import effective_data_owner, get_active_status
from services.uzum_sync import SyncError, run_full_sync
from utils.logger import get_logger

log = get_logger(__name__)

CACHE_TTL = dt.timedelta(minutes=30)
_UTC = dt.timezone.utc

# Лимит подписи под фото в Telegram — РОВНО 1024 символа (текст-сообщение — 4096).
CAPTION_LIMIT = 1024


def clip_caption(text: str) -> str:
    """Гарантировать, что подпись фото уложится в лимит Telegram (1024)."""
    if len(text) <= CAPTION_LIMIT:
        return text
    return text[: CAPTION_LIMIT - 1].rstrip() + "…"


def _is_media(message: Message) -> bool:
    return bool(message.photo or message.video or message.document or message.animation)


async def smart_edit(
    message: Message, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Показать ТЕКСТОВЫЙ экран независимо от текущего типа сообщения.

    Текст → edit_text; медиа (фото-карточка) → удалить и отправить текст заново
    (editMessageText не умеет превращать фото-сообщение в текстовое). Это нужно,
    чтобы выходы из фото-карточки SKU (юнит-экономика, симулятор, «назад») не падали.
    """
    try:
        if _is_media(message):
            await message.delete()
            await message.answer(text, reply_markup=reply_markup)
        else:
            await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        # сообщение неизменно/устарело/уже удалено — просто отправим новое
        await message.answer(text, reply_markup=reply_markup)


async def smart_edit_photo(
    message: Message,
    photo: str | None,
    caption: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Показать ФОТО-карточку с подписью (≤1024) и клавиатурой.

    • уже фото → edit_media (смена модификации/размера БЕЗ новых сообщений);
    • текст → отправить фото и убрать старый текст;
    • нет URL / битая ссылка / Telegram отверг формат (webp и т.п.) → аккуратный
      откат на текстовый режим (smart_edit). Бот никогда не падает из-за картинки.
    """
    caption = clip_caption(caption)
    # DEBUG image_url: точный URL, который уходит в Telegram (или его отсутствие).
    log.info("DEBUG PHOTO URL: %r (chat=%s)", photo, message.chat.id)
    if not photo:
        await smart_edit(message, caption, reply_markup)
        return
    try:
        if message.photo:
            await message.edit_media(
                InputMediaPhoto(media=photo, caption=caption, parse_mode="HTML"),
                reply_markup=reply_markup,
            )
        else:
            await message.answer_photo(photo, caption=caption, reply_markup=reply_markup)
            try:
                await message.delete()          # убрать прежний текст (best-effort)
            except TelegramBadRequest:
                pass
    except TelegramBadRequest as exc:
        # Громкий лог: видим, не блокирует ли Telegram наш URL/формат (webp и т.п.).
        log.error("Telegram photo error: %s | url=%r", exc, photo)
        await smart_edit(message, caption, reply_markup)


# Анти-спам: синк уже идёт у этого юзера.
_running_sync: set[int] = set()

# Анти-спам: синк уже идёт у этого юзера.
_running_sync: set[int] = set()


def _load_status(telegram_id: int) -> tuple[bool, dt.datetime | None]:
    with session_scope() as session:
        return get_active_status(session, telegram_id)


def _data_owner(telegram_id: int) -> int:
    with session_scope() as session:
        return effective_data_owner(session, telegram_id)


async def resolve_owner(telegram_id: int) -> int:
    """telegram_id ЧЬИ данные показывать: сам владелец или владелец-наниматель
    (если это менеджер). Применять в аналитике, чтобы менеджер видел магазин владельца.
    """
    return await asyncio.to_thread(_data_owner, telegram_id)


def _is_fresh(last_sync_at: dt.datetime | None) -> bool:
    if last_sync_at is None:
        return False
    aware = last_sync_at if last_sync_at.tzinfo else last_sync_at.replace(tzinfo=_UTC)
    return (dt.datetime.now(_UTC) - aware) < CACHE_TTL


def _make_progress(loop: asyncio.AbstractEventLoop, status: Message):
    """Колбэк прогресса для воркер-потока: правит статус-сообщение в чате."""
    last = {"text": ""}

    async def _edit(text: str) -> None:
        try:
            await status.edit_text(text)
        except Exception:  # noqa: BLE001
            pass

    def on_progress(text: str) -> None:
        if text == last["text"]:
            return
        last["text"] = text
        fut = asyncio.run_coroutine_threadsafe(_edit(text), loop)
        try:
            fut.result(timeout=10)
        except Exception:  # noqa: BLE001
            pass

    return on_progress


async def _run_sync(message: Message, telegram_id: int) -> Message | None:
    """Двухфазный синк (сеть параллельно, запись single-writer). Возвращает
    статус-сообщение при успехе, иначе None.
    """
    status = await message.answer("🔄 Данные устарели. Запускаю синхронизацию с Uzum Merchant…")
    loop = asyncio.get_running_loop()
    on_progress = _make_progress(loop, status)
    try:
        # run_full_sync сам разводит фазы: сеть под per-user lock + FETCH_SEMAPHORE
        # (потолок RAM), запись параллельно (MVCC Postgres, без write-лока).
        await run_full_sync(telegram_id, on_progress)
    except SyncError as exc:
        await status.edit_text(escape(f"❌ {exc}"))
        return None
    except Exception as exc:  # noqa: BLE001
        log.exception("Sync failed for %s", telegram_id)
        await status.edit_text(escape(f"❌ Непредвиденная ошибка синхронизации: {exc}"))
        return None
    await status.edit_text("✅ Синхронизация завершена. Готовлю результат…")
    return status


async def ensure_fresh(message: Message, telegram_id: int) -> Message | None:
    """Гарантировать свежие данные. Возвращает статус-сообщение или None (прервать)."""
    if telegram_id in _running_sync:
        await message.answer("🔄 Синхронизация уже выполняется, пожалуйста, подождите.")
        return None

    has_active, last_sync_at = await asyncio.to_thread(_load_status, telegram_id)
    if not has_active:
        await message.answer("Сначала подключите магазин — команда /start.")
        return None

    if _is_fresh(last_sync_at):
        return await message.answer(
            "⚡ Использую свежие данные (обновлено менее 30 мин. назад)…"
        )

    _running_sync.add(telegram_id)
    try:
        return await _run_sync(message, telegram_id)
    finally:
        _running_sync.discard(telegram_id)


__all__ = [
    "ensure_fresh",
    "resolve_owner",
    "CACHE_TTL",
    "smart_edit",
    "smart_edit_photo",
    "clip_caption",
    "CAPTION_LIMIT",
]
