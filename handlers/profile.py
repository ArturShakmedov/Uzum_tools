"""Раздел «👤 Мой Кабинет»: подписка + управление менеджерами + инвайт-ссылка.

Открыт всем (не под SubscriptionMiddleware) — кабинет должен видеть и free-юзер.
"""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from aiogram.exceptions import TelegramBadRequest

from database.connection import session_scope
from database.repository import (
    ensure_user,
    get_detailed_managers,
    is_user_premium,
    list_shop_managers,
    remove_shop_manager,
    subscription_days_left,
)
from keyboards.menu import BTN_PROFILE
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="profile")


# --------------------------------------------------------------------------- #
#  Главный экран кабинета
# --------------------------------------------------------------------------- #
def _load_profile(telegram_id: int) -> dict:
    with session_scope() as session:
        user = ensure_user(session, telegram_id)
        premium = is_user_premium(user)
        return {
            "plan_name": user.plan_name or "Бесплатный",
            "premium": premium,
            "days_left": subscription_days_left(user) if premium else 0,
            "managers_count": len(list_shop_managers(session, telegram_id)),
        }


def _profile_text(p: dict) -> str:
    if p["premium"]:
        sub = (
            f"💎 План: <b>{escape(p['plan_name'])}</b>\n"
            f"⏳ Осталось дней: <b>{p['days_left']}</b>"
        )
    else:
        sub = "🆓 План: <b>Бесплатный</b>. Доступ ограничен."
    return (
        "👤 <b>Мой Кабинет</b>\n"
        "───────────────────\n"
        f"{sub}\n\n"
        f"👥 Менеджеров с доступом: <b>{p['managers_count']}</b>"
    )


def _profile_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Продлить подписку", callback_data="billing:choose_plan")],
        [InlineKeyboardButton(text="👥 Управление менеджерами", callback_data="profile:managers")],
        [InlineKeyboardButton(text="🔗 Поделиться магазином", callback_data="profile:share")],
    ])


@router.message(F.text == BTN_PROFILE)
async def on_profile(message: Message) -> None:
    p = await asyncio.to_thread(_load_profile, message.from_user.id)
    await message.answer(_profile_text(p), reply_markup=_profile_kb())


@router.callback_query(F.data == "profile:open")
async def on_profile_open(callback: CallbackQuery) -> None:
    p = await asyncio.to_thread(_load_profile, callback.from_user.id)
    await callback.message.edit_text(_profile_text(p), reply_markup=_profile_kb())
    await callback.answer()


# --------------------------------------------------------------------------- #
#  Управление менеджерами
# --------------------------------------------------------------------------- #
def _load_managers(owner_telegram_id: int) -> list[dict]:
    """Детальный список менеджеров (имя/юзернейм из users через LEFT JOIN)."""
    with session_scope() as session:
        return get_detailed_managers(session, owner_telegram_id)


def _managers_text(managers: list[dict]) -> str:
    """Нумерованный список: «N. 👤 Имя (@username|ID: …) — Добавлен: ДД.ММ»."""
    if not managers:
        return (
            "👥 <b>Менеджеры магазина</b>\n\nПока никто не добавлен.\n"
            "Нажмите «🔗 Поделиться магазином» и отправьте ссылку сотруднику."
        )
    lines = []
    for i, m in enumerate(managers, 1):
        name = escape((m.get("first_name") or "Сотрудник").strip())
        handle = f"@{escape(m['username'])}" if m.get("username") else f"ID: {m['manager_telegram_id']}"
        created = m.get("created_at")
        date = created.strftime("%d.%m") if created else "—"
        lines.append(f"{i}. 👤 <b>{name}</b> ({handle}) — Добавлен: {date}")
    return f"👥 <b>Менеджеры магазина</b> ({len(managers)}):\n" + "\n".join(lines)


def _managers_kb(managers: list[dict], per_row: int = 4) -> InlineKeyboardMarkup:
    """Компактные кнопки удаления [❌ N] (сетка по `per_row`) + «Назад»."""
    buttons = [
        InlineKeyboardButton(
            text=f"❌ {i}",
            callback_data=f"mgr:delete:{m['manager_telegram_id']}",
        )
        for i, m in enumerate(managers, 1)
    ]
    rows = [buttons[j:j + per_row] for j in range(0, len(buttons), per_row)]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="profile:open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_managers(callback: CallbackQuery) -> None:
    """Перерисовать экран менеджеров свежими данными (мягко, edit_text)."""
    managers = await asyncio.to_thread(_load_managers, callback.from_user.id)
    try:
        await callback.message.edit_text(
            _managers_text(managers), reply_markup=_managers_kb(managers)
        )
    except TelegramBadRequest:
        pass  # «message is not modified» — например, удалять было нечего


@router.callback_query(F.data == "profile:managers")
async def on_managers(callback: CallbackQuery) -> None:
    await _show_managers(callback)
    await callback.answer()


def _remove_manager(owner_telegram_id: int, manager_telegram_id: int) -> int:
    with session_scope() as session:
        return remove_shop_manager(session, owner_telegram_id, manager_telegram_id)


@router.callback_query(F.data.startswith("mgr:delete:"))
async def on_remove_manager(callback: CallbackQuery) -> None:
    manager_id = int(callback.data.rsplit(":", 1)[-1])
    removed = await asyncio.to_thread(_remove_manager, callback.from_user.id, manager_id)
    if removed:
        try:  # уведомить уволенного менеджера в личку
            await callback.bot.send_message(
                manager_id,
                "ℹ️ Ваш доступ к магазину был отозван владельцем.",
            )
        except Exception as exc:  # noqa: BLE001 — менеджер мог заблокировать бота
            log.warning("Уведомление уволенному %s не доставлено: %s", manager_id, exc)
    await callback.answer("Доступ отозван" if removed else "Менеджер уже удалён")
    await _show_managers(callback)            # мягкое обновление списка


# --------------------------------------------------------------------------- #
#  Инвайт-ссылка (deep-link)
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "profile:share")
async def on_share(callback: CallbackQuery) -> None:
    owner_id = callback.from_user.id
    me = await callback.bot.me()
    link = f"https://t.me/{me.username}?start=invite_{owner_id}"
    await callback.message.answer(
        "🔗 <b>Пригласить менеджера</b>\n\n"
        "Отправьте эту ссылку сотруднику — перейдя по ней, он получит доступ к "
        "аналитике вашего магазина:\n\n"
        f"<code>{escape(link)}</code>"
    )
    await callback.answer()


__all__ = ["router"]
