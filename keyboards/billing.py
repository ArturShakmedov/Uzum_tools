"""Клавиатуры биллинга (вынесены из handlers, чтобы не было циклов импорта)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def buy_premium_kb() -> InlineKeyboardMarkup:
    """Кнопка «💎 Купить Premium» → открывает меню выбора тарифа (billing:choose_plan).

    Используется в сообщении SubscriptionMiddleware при блокировке free-юзера.
    """
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Купить Premium", callback_data="billing:choose_plan")]
    ])


__all__ = ["buy_premium_kb"]
