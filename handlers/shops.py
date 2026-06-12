"""Переключение между уже подключёнными магазинами («🏪 Выбрать магазин»)."""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from database.connection import session_scope
from database.repository import list_user_shops, switch_active_shop
from keyboards.menu import BTN_SHOPS, main_menu_kb, shops_inline_kb
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="shops")


def _load_shops(telegram_id: int) -> list[tuple[int, str, bool]]:
    with session_scope() as session:
        return [
            (s.uzum_shop_id, s.shop_name or str(s.uzum_shop_id), s.is_active)
            for s in list_user_shops(session, telegram_id)
        ]


@router.message(F.text == BTN_SHOPS)
async def on_choose_shop(message: Message) -> None:
    items = await asyncio.to_thread(_load_shops, message.from_user.id)
    if not items:
        await message.answer("Пока нет подключённых магазинов. Отправьте /start.")
        return
    await message.answer(
        "Ваши магазины (активный отмечен ✅):",
        reply_markup=shops_inline_kb(items, "switch"),
    )


@router.callback_query(F.data.startswith("shop:switch:"))
async def switch_chosen_shop(callback: CallbackQuery) -> None:
    uzum_shop_id = int(callback.data.rsplit(":", 1)[-1])

    def _switch() -> str | None:
        with session_scope() as session:
            shop = switch_active_shop(session, callback.from_user.id, uzum_shop_id)
            return shop.shop_name or str(shop.uzum_shop_id) if shop else None

    name = await asyncio.to_thread(_switch)
    if name is None:
        await callback.answer("Магазин не найден", show_alert=True)
        return
    await callback.message.edit_text(
        f"✅ Активный магазин: «{escape(name)}».\n"
        "Данные обновятся при следующем запросе отчёта."
    )
    await callback.message.answer("Готово.", reply_markup=main_menu_kb())
    await callback.answer()


__all__ = ["router"]
