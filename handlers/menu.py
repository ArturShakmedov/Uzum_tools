"""Хендлер главного меню (/menu и /help)."""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database.connection import session_scope
from database.repository import get_active_shop
from keyboards.menu import main_menu_kb
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="menu")


def _has_active_shop(telegram_id: int) -> bool:
    with session_scope() as session:
        return get_active_shop(session, telegram_id) is not None


@router.message(Command("menu", "help"))
async def cmd_menu(message: Message) -> None:
    if not await asyncio.to_thread(_has_active_shop, message.from_user.id):
        await message.answer("Сначала подключите магазин — команда /start.")
        return
    await message.answer(
        escape(
            "Главное меню:\n"
            "⏳ Не вернулись (<30 дней) — ещё в пути/ожидании.\n"
            "🚨 Утерянные (30+ дней) — для подачи претензии по утере.\n"
            "💰 Выплаты и баланс — финансовая сводка магазина.\n"
            "🧮 Калькулятор — юнит-экономика товара (комиссии Uzum).\n"
            "📦 Мои товары — карточки товаров из Uzum (цена, остатки, расчёт).\n"
            "🏪 Выбрать магазин — переключиться между подключёнными магазинами.\n\n"
            "Данные синхронизируются автоматически (кэш 30 мин)."
        ),
        reply_markup=main_menu_kb(),
    )


__all__ = ["router"]
