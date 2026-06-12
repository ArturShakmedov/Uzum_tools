"""Центральный пункт управления суперадмина (/root). Доступ — только UserRole.ROOT.

Роль прокидывает InfrastructureShieldMiddleware в data['current_role'] (ADMIN_IDS →
ROOT). Здесь: stateful-дашборд (обновление/переключение техработ) + прямые команды
управления RBAC и баном со строгой валидацией аргументов.
"""

from __future__ import annotations

import asyncio
import re
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import SYSTEM_CACHE
from database.connection import session_scope
from database.models import UserRole
from database.repository import (
    get_dashboard_stats,
    set_setting,
    set_user_banned,
    set_user_role,
)
from middlewares.infrastructure import invalidate_user_status
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="root")

_ID_RE = re.compile(r"^-?\d{1,15}$")
_VALID_ROLES = {r.value for r in UserRole}        # {'user','manager','admin','root'}


class IsRoot(BaseFilter):
    """Пропускает только суперадмина (current_role из Shield-middleware)."""

    async def __call__(self, event: Any, current_role: UserRole | None = None) -> bool:
        return current_role == UserRole.ROOT


# --------------------------------------------------------------------------- #
#  Дашборд (stateful)
# --------------------------------------------------------------------------- #
def _fmt(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _load_dashboard() -> dict[str, Any]:
    with session_scope() as session:
        return get_dashboard_stats(session)


def _dashboard_text(s: dict[str, Any]) -> str:
    maintenance = SYSTEM_CACHE.get("maintenance_mode") == "true"
    maint = "🔴 ВКЛЮЧЕН" if maintenance else "🟢 выключен"
    return (
        "🛰 <b>ROOT CONTROL CORE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 Пользователей: <b>{_fmt(s['users'])}</b>  (💎 Premium: <b>{_fmt(s['premium'])}</b>)\n"
        f"🏪 Магазинов Uzum: <b>{_fmt(s['shops'])}</b>\n"
        f"🎫 Тикетов поддержки: <b>{_fmt(s['tickets'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💰 <b>Финансовый аудит (UZS):</b>\n"
        f"├ Сегодня: <b>{_fmt(s['rev_today'])}</b>\n"
        f"├ Этот месяц: <b>{_fmt(s['rev_month'])}</b>\n"
        f"└ Общий оборот: <b>{_fmt(s['rev_all'])}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🛠 Тех-работы: <b>{maint}</b>"
    )


def _dashboard_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить показатели", callback_data="root:refresh")],
        [InlineKeyboardButton(text="🛑 Переключить тех-работы", callback_data="root:toggle_maint")],
    ])


@router.message(Command("root"), IsRoot())
async def cmd_root(message: Message) -> None:
    stats = await asyncio.to_thread(_load_dashboard)
    await message.answer(_dashboard_text(stats), reply_markup=_dashboard_kb())


@router.callback_query(F.data == "root:refresh", IsRoot())
async def on_refresh(callback: CallbackQuery) -> None:
    stats = await asyncio.to_thread(_load_dashboard)
    try:
        await callback.message.edit_text(_dashboard_text(stats), reply_markup=_dashboard_kb())
        await callback.answer("Обновлено")
    except TelegramBadRequest:
        await callback.answer("Данные не изменились")  # message is not modified


def _toggle_maintenance() -> bool:
    """Инвертировать maintenance_mode в БД и кэше. Возвращает новое состояние (bool)."""
    new_on = SYSTEM_CACHE.get("maintenance_mode") != "true"
    value = "true" if new_on else "false"
    with session_scope() as session:
        set_setting(
            session, "maintenance_mode", value,
            description="Глобальный режим техобслуживания",
        )
    SYSTEM_CACHE["maintenance_mode"] = value        # атомарное обновление кэша
    return new_on


@router.callback_query(F.data == "root:toggle_maint", IsRoot())
async def on_toggle_maintenance(callback: CallbackQuery) -> None:
    new_on = await asyncio.to_thread(_toggle_maintenance)
    log.warning("ROOT %s переключил maintenance_mode → %s", callback.from_user.id, new_on)
    stats = await asyncio.to_thread(_load_dashboard)
    try:
        await callback.message.edit_text(_dashboard_text(stats), reply_markup=_dashboard_kb())
    except TelegramBadRequest:
        pass
    await callback.answer("🛠 Тех-работы ВКЛЮЧЕНЫ" if new_on else "✅ Тех-работы выключены", show_alert=True)


# --------------------------------------------------------------------------- #
#  Прямые команды управления (строгая валидация)
# --------------------------------------------------------------------------- #
def _do_set_role(telegram_id: int, role: UserRole) -> None:
    with session_scope() as session:
        set_user_role(session, telegram_id, role)


@router.message(Command("set_role"), IsRoot())
async def cmd_set_role(message: Message, command: CommandObject) -> None:
    """/set_role <telegram_id> <user|manager|admin|root>."""
    parts = (command.args or "").split()
    if len(parts) != 2 or not _ID_RE.match(parts[0]) or parts[1].lower() not in _VALID_ROLES:
        await message.answer(
            "Использование: <code>/set_role &lt;telegram_id&gt; &lt;user|manager|admin|root&gt;</code>"
        )
        return
    target_id = int(parts[0])
    role = UserRole(parts[1].lower())
    await asyncio.to_thread(_do_set_role, target_id, role)
    invalidate_user_status(target_id)  # TTL-кэш Shield: новая роль действует сразу
    log.info("ROOT %s назначил роль %s юзеру %s", message.from_user.id, role.value, target_id)
    await message.answer(
        f"✅ Роль пользователя <code>{target_id}</code> изменена на <b>{role.value}</b>."
    )


def _do_set_banned(telegram_id: int, banned: bool) -> None:
    with session_scope() as session:
        set_user_banned(session, telegram_id, banned)


@router.message(Command("ban"), IsRoot())
async def cmd_ban(message: Message, command: CommandObject) -> None:
    """/ban <telegram_id> — деактивировать аккаунт."""
    raw = (command.args or "").split(maxsplit=1)
    if not raw or not _ID_RE.match(raw[0]):
        await message.answer("Использование: <code>/ban &lt;telegram_id&gt;</code>")
        return
    target_id = int(raw[0])
    await asyncio.to_thread(_do_set_banned, target_id, True)
    invalidate_user_status(target_id)  # TTL-кэш Shield: бан действует сразу
    log.info("ROOT %s забанил %s", message.from_user.id, target_id)
    await message.answer(f"🚫 Пользователь <code>{target_id}</code> заблокирован.")


@router.message(Command("unban"), IsRoot())
async def cmd_unban(message: Message, command: CommandObject) -> None:
    """/unban <telegram_id> — снять блокировку."""
    raw = (command.args or "").strip()
    if not _ID_RE.match(raw):
        await message.answer("Использование: <code>/unban &lt;telegram_id&gt;</code>")
        return
    target_id = int(raw)
    await asyncio.to_thread(_do_set_banned, target_id, False)
    invalidate_user_status(target_id)  # TTL-кэш Shield: разбан действует сразу
    log.info("ROOT %s разбанил %s", message.from_user.id, target_id)
    await message.answer(f"✅ Пользователь <code>{target_id}</code> разблокирован.")


__all__ = ["router", "IsRoot"]
