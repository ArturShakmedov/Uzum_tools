"""SubscriptionMiddleware — гейт Premium-функционала.

Вешается на роутеры premium-фич (Мои товары / финансы-отчёты / аналитика). Free-
юзеру такие хэндлеры не выполняются: вместо них показывается предложение купить
Premium. Калькулятор, /start, биллинг, меню и админка НЕ закрыты этим middleware.

Доступ открыт, если юзер — администратор (ADMIN_IDS) ИЛИ имеет активный Premium
(subscription_tier='premium' и subscription_expires_at в будущем).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TgUser

from config import ADMIN_IDS
from database.connection import session_scope
from database.repository import (
    ensure_user,
    get_owner_for_manager,
    get_user,
    is_user_premium,
)
from keyboards.billing import buy_premium_kb
from utils.logger import get_logger

log = get_logger(__name__)

_DENIED_TEXT = (
    "🔒 <b>Этот функционал доступен только в Premium-тарифе!</b>\n"
    "В бесплатной версии вам доступен только калькулятор. Нажмите кнопку ниже, "
    "чтобы активировать Premium-доступ всего за 150 000 сум/мес."
)


def _has_access(telegram_id: int) -> bool:
    """Доступ к Premium-фичам: админ, активный Premium ИЛИ менеджер премиум-владельца.

    Заодно гарантирует наличие строки User (free) при первом взаимодействии.
    """
    if telegram_id in set(ADMIN_IDS):
        return True
    with session_scope() as session:
        user = ensure_user(session, telegram_id)  # авто-создание при взаимодействии
        if is_user_premium(user):
            return True
        # Менеджер видит фичи, если его владелец на активном Premium.
        owner = get_owner_for_manager(session, telegram_id)
        return owner is not None and is_user_premium(get_user(session, owner))


class SubscriptionMiddleware(BaseMiddleware):
    """Inner-middleware: пропускает только premium/админов, иначе — оффер Premium."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: TgUser | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        if await asyncio.to_thread(_has_access, user.id):
            return await handler(event, data)

        await _send_denied(event)
        return None  # хэндлер premium-фичи НЕ вызываем


async def _send_denied(event: TelegramObject) -> None:
    kb = buy_premium_kb()
    if isinstance(event, CallbackQuery):
        await event.answer()  # снять «часики» на кнопке
        if event.message is not None:
            await event.message.answer(_DENIED_TEXT, reply_markup=kb)
    elif isinstance(event, Message):
        await event.answer(_DENIED_TEXT, reply_markup=kb)


__all__ = ["SubscriptionMiddleware"]
