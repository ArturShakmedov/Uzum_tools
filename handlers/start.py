"""Хендлер /start: ввод токена → выбор магазина (inline) → подключение."""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from database.connection import session_scope
from database.repository import (
    activate_welcome_trial,
    add_shop_manager,
    connect_shop,
    ensure_user,
    get_active_shop,
)
from keyboards.menu import main_menu_kb, shops_inline_kb
from services.uzum_sync import fetch_shops
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="start")


class Onboarding(StatesGroup):
    waiting_token = State()
    choosing_shop = State()


def _has_active_shop(telegram_id: int) -> bool:
    with session_scope() as session:
        return get_active_shop(session, telegram_id) is not None


def _ensure_user(telegram_id: int, username: str | None, first_name: str | None) -> None:
    """Авто-создать/освежить User при /start (с именем/юзернеймом для UI менеджеров)."""
    with session_scope() as session:
        ensure_user(session, telegram_id, username=username, first_name=first_name)


def _accept_invite(
    owner_id: int, manager_id: int, username: str | None, first_name: str | None
) -> tuple[bool, int | None]:
    """Записать менеджера в ShopManager (+ сохранить его профиль). (created, shop_id)."""
    with session_scope() as session:
        ensure_user(session, manager_id, username=username, first_name=first_name)
        owner_shop = get_active_shop(session, owner_id)
        shop_id = owner_shop.uzum_shop_id if owner_shop else None
        _, created = add_shop_manager(session, owner_id, manager_id, shop_id=shop_id)
        return created, shop_id


async def _handle_invite(message: Message, payload: str) -> bool:
    """Обработать deep-link invite_<owner_id>. True, если это был валидный инвайт."""
    raw = payload.removeprefix("invite_")
    if not raw.isdigit():
        return False
    owner_id = int(raw)
    manager = message.from_user
    if owner_id == manager.id:
        await message.answer("Нельзя добавить самого себя в менеджеры 🙂")
        return True

    created, _ = await asyncio.to_thread(
        _accept_invite, owner_id, manager.id, manager.username, manager.first_name
    )
    await message.answer(
        "✅ Вы добавлены менеджером магазина. Теперь вам доступна его аналитика.",
        reply_markup=main_menu_kb(),
    )
    if created:  # уведомляем владельца
        uname = f"@{manager.username}" if manager.username else f"id {manager.id}"
        try:
            await message.bot.send_message(
                owner_id,
                f"👥 Пользователь {escape(uname)} добавлен как менеджер вашего магазина.",
            )
        except Exception as exc:  # noqa: BLE001 — владелец мог не /start'нуть/заблокировать
            log.warning("Уведомление владельцу %s не доставлено: %s", owner_id, exc)
    return True


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: CommandObject) -> None:
    await state.clear()
    u = message.from_user
    await asyncio.to_thread(_ensure_user, u.id, u.username, u.first_name)

    # Deep-link приглашения менеджера: /start invite_<owner_id>
    if command.args and command.args.startswith("invite_"):
        if await _handle_invite(message, command.args):
            return

    if await asyncio.to_thread(_has_active_shop, message.from_user.id):
        await message.answer(
            "С возвращением! 👋 Выберите действие в меню.", reply_markup=main_menu_kb()
        )
        return

    await message.answer(
        "Привет! Это бот аналитики невозвратов Uzum.\n\n"
        "Пришлите ваш <b>API-токен Uzum Seller</b> одним сообщением (без префикса "
        "Bearer). Я проверю его и покажу список ваших магазинов.\n\n"
        "🔒 Токен используется только для запросов к вашему магазину и хранится "
        "в зашифрованном виде."
    )
    await state.set_state(Onboarding.waiting_token)


@router.message(Onboarding.waiting_token, F.text)
async def receive_token(message: Message, state: FSMContext) -> None:
    token = (message.text or "").strip()
    if len(token) < 10:
        await message.answer("Это не похоже на токен. Пришлите корректный токен Uzum.")
        return

    status = await message.answer("⏳ Проверяю токен и получаю список магазинов…")
    shops = await asyncio.to_thread(fetch_shops, token)

    if shops is None:
        await status.edit_text("❌ Токен невалиден или нет доступа к API. Пришлите ещё раз.")
        return
    if not shops:
        await status.edit_text("⚠️ По токену не найдено ни одного магазина. Пришлите другой токен.")
        return

    # Кладём токен и магазины в FSM, ждём выбор по inline-кнопке.
    await state.update_data(token=token, shops={int(s["id"]): s.get("name") for s in shops})
    await state.set_state(Onboarding.choosing_shop)
    items = [(int(s["id"]), s.get("name") or str(s["id"]), False) for s in shops]
    await status.edit_text("Выберите магазин для подключения к боту:")
    await message.answer("Доступные магазины:", reply_markup=shops_inline_kb(items, "connect"))


@router.callback_query(Onboarding.choosing_shop, F.data.startswith("shop:connect:"))
async def connect_chosen_shop(callback: CallbackQuery, state: FSMContext) -> None:
    uzum_shop_id = int(callback.data.rsplit(":", 1)[-1])
    data = await state.get_data()
    token = data.get("token")
    shops: dict = data.get("shops") or {}
    shop_name = shops.get(uzum_shop_id) or shops.get(str(uzum_shop_id))
    if not token:
        await callback.message.edit_text("Сессия истекла. Отправьте /start заново.")
        await state.clear()
        await callback.answer()
        return

    def _save() -> bool:
        """Сохранить магазин и (в той же транзакции) попытаться выдать Welcome-триал."""
        with session_scope() as session:
            connect_shop(
                session,
                callback.from_user.id,
                uzum_shop_id,
                shop_name=shop_name,
                uzum_token=token,
                username=callback.from_user.username,
            )
            # 7 дней Premium при ПЕРВОМ магазине; повторно не начислится (abuse-защита).
            return activate_welcome_trial(session, callback.from_user.id)

    trial_granted = await asyncio.to_thread(_save)
    await state.clear()

    text = f"✅ Магазин «{escape(str(shop_name or uzum_shop_id))}» подключён и активен."
    if trial_granted:
        text += (
            "\n\n🎉 <b>Вам начислен Welcome-бонус!</b>\n"
            "🔥 Активирован полный Premium-доступ на 7 дней. Тестируйте аналитику "
            "конкурентов, невозвраты и подключайте менеджеров без ограничений!"
        )
    await callback.message.edit_text(text)
    await callback.message.answer(
        "Готово! Выберите отчёт.", reply_markup=main_menu_kb()
    )
    await callback.answer()


__all__ = ["router"]
