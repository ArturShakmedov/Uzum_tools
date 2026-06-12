"""Админ-панель (только ADMIN_IDS): метрики, рассылка + сервисный /revoke.

Доступ ко ВСЕМ админ-хэндлерам ограничен фильтром IsAdmin (проверка
event.from_user.id in ADMIN_IDS). Обычный юзер, набравший /admin, просто не
матчит хэндлер — бот молчит.

Рассылка устойчива к блокировкам: TelegramForbiddenError → помечаем юзера
is_active=False (deactivate_user_shops), чтобы live-воркер и будущие рассылки его
не дёргали. Между сообщениями — пауза 0.05 с (анти-флуд Telegram).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import ADMIN_IDS
from database.connection import session_scope
from database.repository import (
    activate_premium,
    deactivate_user_shops,
    get_admin_stats,
    list_broadcast_recipients,
    list_payment_logs,
    wipe_user,
)
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="admin")

# Пауза между сообщениями рассылки — анти-флуд Telegram (~20 msg/s безопасно).
_BROADCAST_DELAY = 0.05

# Держим ссылки на фоновые задачи рассылки, чтобы их не собрал GC.
_bg_tasks: set[asyncio.Task] = set()


class IsAdmin(BaseFilter):
    """Пропускает только пользователей из ADMIN_IDS. Работает для Message и Callback."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return bool(user and user.id in set(ADMIN_IDS))


class AdminStates(StatesGroup):
    waiting_for_broadcast_text = State()     # ввод текста рассылки (HTML)
    waiting_for_broadcast_confirm = State()  # подтверждение запуска после превью


# --------------------------------------------------------------------------- #
#  Клавиатуры
# --------------------------------------------------------------------------- #
def _admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать рассылку", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🔄 Обновить метрики", callback_data="admin:refresh")],
    ])


def _broadcast_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:bcast_cancel")]
    ])


def _broadcast_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚀 Запустить", callback_data="admin:bcast_run"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="admin:bcast_cancel"),
    ]])


# --------------------------------------------------------------------------- #
#  Главное меню админа: метрики + кнопки
# --------------------------------------------------------------------------- #
def _load_admin_stats() -> dict[str, int]:
    with session_scope() as session:
        return get_admin_stats(session)


def _panel_text(s: dict[str, int]) -> str:
    """Структурированный дашборд метрик с древовидной разметкой (├──/└──).

    Число товаров — с разделителем тысяч (1186 → «1 186»). В конце — время сервера
    ЧЧ:ММ:СС, чтобы было видно, что «🔄 Обновить метрики» отработала (текст всегда
    меняется → edit_message_text не упирается в «message is not modified»).
    """
    products = f"{s['products']:,}".replace(",", " ")
    now = dt.datetime.now().strftime("%H:%M:%S")
    return (
        "🎛 <b>КОМАНДНЫЙ ПУНКТ | UZUM TOOLS</b>\n"
        "───────────────────\n\n"
        "📈 <b>МАСШТАБ СЕТИ</b>\n"
        f"├── 👥 Селлеров в базе: {s['users']}\n"
        f"└── 💎 Premium-подписок: {s['premium_users']}\n\n"
        "🏪 <b>ОБЪЕМ ИНТЕГРАЦИЙ</b>\n"
        f"├── 🔑 Активных магазинов: {s['active_shops']}\n"
        f"└── 📦 Товаров на мониторинге: {products} шт.\n\n"
        "🎯 <b>ЮНИТ-ЭКОНОМИКА</b>\n"
        f"└── 💰 Закупка задана для: {s['users_with_purchase']} SKU\n\n"
        "───────────────────\n"
        f"⏱ Обновлено: {now}"
    )


@router.message(Command("admin", "stats"), IsAdmin())
async def cmd_admin_panel(message: Message) -> None:
    s = await asyncio.to_thread(_load_admin_stats)
    await message.answer(_panel_text(s), reply_markup=_admin_menu_kb())


@router.callback_query(F.data == "admin:refresh", IsAdmin())
async def on_refresh(callback: CallbackQuery) -> None:
    s = await asyncio.to_thread(_load_admin_stats)
    try:
        await callback.message.edit_text(_panel_text(s), reply_markup=_admin_menu_kb())
    except TelegramBadRequest:
        pass  # «message is not modified» — метрики не изменились, это нормально
    await callback.answer("Обновлено")


# --------------------------------------------------------------------------- #
#  Рассылка: текст → превью → подтверждение → фоновый цикл отправки
# --------------------------------------------------------------------------- #
@router.callback_query(F.data == "admin:broadcast", IsAdmin())
async def on_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.waiting_for_broadcast_text)
    await callback.message.answer(
        "📢 Пришлите <b>текст рассылки</b> (поддерживается HTML-разметка):",
        reply_markup=_broadcast_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.waiting_for_broadcast_text, IsAdmin(), F.text)
async def on_broadcast_text(message: Message, state: FSMContext) -> None:
    # Берём СЫРОЙ текст: админ вводит HTML-разметку тегами (<b>…</b>), которую при
    # отправке рендерит parse_mode=HTML. (html_text экранировал бы литеральные теги.)
    text = message.text or ""
    # Превью «как увидят юзеры». Кривой HTML → Telegram бросит ошибку — просим
    # исправить, оставаясь в состоянии ввода текста.
    try:
        await message.answer(text)
    except TelegramBadRequest as exc:
        await message.answer(
            escape(f"⚠️ Ошибка HTML-разметки: {exc}\nИсправьте и пришлите текст снова.")
        )
        return
    await state.update_data(broadcast_text=text)
    await state.set_state(AdminStates.waiting_for_broadcast_confirm)
    await message.answer(
        "👆 Так увидят пользователи. Запустить рассылку?",
        reply_markup=_broadcast_confirm_kb(),
    )


@router.callback_query(F.data == "admin:bcast_cancel", IsAdmin())
async def on_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.edit_text("❌ Рассылка отменена.")
    except TelegramBadRequest:
        await callback.message.answer("❌ Рассылка отменена.")
    await callback.answer()


@router.callback_query(F.data == "admin:bcast_run", AdminStates.waiting_for_broadcast_confirm, IsAdmin())
async def on_broadcast_run(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    text = data.get("broadcast_text")
    await state.clear()
    if not text:
        await callback.answer("Текст пуст — начните заново.", show_alert=True)
        return
    try:
        await callback.message.edit_text("🚀 Рассылка запущена… отчёт придёт по завершении.")
    except TelegramBadRequest:
        pass
    await callback.answer()
    # Фоновая задача: не блокируем хэндлер на время рассылки сотням юзеров.
    task = asyncio.create_task(_run_broadcast(callback.bot, callback.message.chat.id, text))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def _load_recipients() -> list[int]:
    with session_scope() as session:
        return list_broadcast_recipients(session)


def _mark_blocked(telegram_id: int) -> None:
    with session_scope() as session:
        deactivate_user_shops(session, telegram_id)


async def _run_broadcast(bot: Bot, admin_chat_id: int, text: str) -> None:
    """Цикл отправки рассылки всем достижимым юзерам.

    На каждого — try/except: TelegramForbiddenError (заблокировал бота) → помечаем
    is_active=False и считаем в blocked; прочие ошибки → failed (юзер остаётся).
    Между отправками пауза 0.05 с (анти-флуд). По окончании — отчёт админу.
    """
    recipients = await asyncio.to_thread(_load_recipients)
    sent = blocked = failed = 0
    for telegram_id in recipients:
        try:
            await bot.send_message(telegram_id, text)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
            await asyncio.to_thread(_mark_blocked, telegram_id)
        except Exception as exc:  # noqa: BLE001 — сетевые/прочие, не валим рассылку
            failed += 1
            log.warning("Broadcast: отправка %s не удалась: %s", telegram_id, exc)
        await asyncio.sleep(_BROADCAST_DELAY)

    report = (
        "📢 <b>Рассылка завершена!</b>\n"
        f"✅ Успешно доставлено: <b>{sent}</b>\n"
        f"🚫 Заблокировали бота: <b>{blocked}</b>"
    )
    if failed:
        report += f"\n⚠️ Прочие ошибки: <b>{failed}</b>"
    log.info("Broadcast finished: sent=%d blocked=%d failed=%d", sent, blocked, failed)
    try:
        await bot.send_message(admin_chat_id, report)
    except Exception as exc:  # noqa: BLE001
        log.warning("Broadcast: отчёт админу не отправлен: %s", exc)


# --------------------------------------------------------------------------- #
#  /revoke — полная очистка данных и токена пользователя (любой юзер)
# --------------------------------------------------------------------------- #
def _revoke_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Да, удалить", callback_data="revoke:confirm"),
            InlineKeyboardButton(text="Отмена", callback_data="revoke:cancel"),
        ]]
    )


@router.message(Command("revoke"))
async def cmd_revoke(message: Message) -> None:
    await message.answer(
        "⚠️ Удалить <b>все ваши данные и токен</b> Uzum? Действие необратимо.\n"
        "После этого потребуется заново пройти /start.",
        reply_markup=_revoke_kb(),
    )


def _wipe(telegram_id: int) -> None:
    with session_scope() as session:
        wipe_user(session, telegram_id)  # доменные данные + все магазины/токены


@router.callback_query(F.data == "revoke:confirm")
async def revoke_confirm(callback: CallbackQuery) -> None:
    await asyncio.to_thread(_wipe, callback.from_user.id)
    log.info("Пользователь %s выполнил /revoke (данные удалены).", callback.from_user.id)
    await callback.message.edit_text("✅ Данные и токен удалены. Для входа — /start.")
    await callback.answer("Удалено")


@router.callback_query(F.data == "revoke:cancel")
async def revoke_cancel(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отменено — данные на месте.")
    await callback.answer()


# --------------------------------------------------------------------------- #
#  Платёжный аудит и ручное начисление подписки (только ADMIN_IDS)
# --------------------------------------------------------------------------- #
# payload пакета → короткая метка периода для списка транзакций.
_PERIOD_LABEL: dict[str, str] = {
    "premium_1_month": "1 мес",
    "premium_3_months": "3 мес",
    "premium_6_months": "6 мес",
    "premium_1_year": "1 год",
}


def _load_payments() -> list[dict]:
    with session_scope() as session:
        return list_payment_logs(session, limit=10)


@router.message(Command("admin_payments"), IsAdmin())
async def cmd_admin_payments(message: Message) -> None:
    """Последние 10 транзакций с именем юзера (для разбора зависших оплат)."""
    rows = await asyncio.to_thread(_load_payments)
    if not rows:
        await message.answer("📋 Платежей пока нет.")
        return
    lines = ["📋 <b>Последние транзакции:</b>"]
    for i, p in enumerate(rows, 1):
        name = escape((p["first_name"] or "—").strip())
        handle = f"@{escape(p['username'])}" if p["username"] else f"ID: {p['telegram_id']}"
        amount = f"{p['amount']:,}".replace(",", " ")
        period = _PERIOD_LABEL.get(p["payload"], p["payload"])
        if p["status"] == "completed":
            badge, ts = "🟢 COMPLETED", (p["updated_at"] or p["created_at"])
        else:
            badge, ts = "🟡 CREATED", p["created_at"]
        when = ts.strftime("%d.%m %H:%M") if ts else "—"
        lines.append(
            f"{i}. 👤 {name} ({handle}) — {amount} сум ({period}) | {badge} ({when})"
        )
    await message.answer("\n".join(lines))


def _grant_premium(telegram_id: int, days: int) -> None:
    with session_scope() as session:
        activate_premium(
            session, telegram_id, days=days, plan_name="Premium (Вручную)"
        )


@router.message(Command("grant_premium"), IsAdmin())
async def cmd_grant_premium(
    message: Message, command: CommandObject, bot: Bot
) -> None:
    """Ручное начисление Premium: /grant_premium <telegram_id> <days>."""
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].lstrip("-").isdigit() or not args[1].isdigit():
        await message.answer(
            "Использование: <code>/grant_premium &lt;telegram_id&gt; &lt;days&gt;</code>\n"
            "Например: <code>/grant_premium 123456789 30</code>"
        )
        return
    target_id, days = int(args[0]), int(args[1])
    if days <= 0:
        await message.answer("Количество дней должно быть больше 0.")
        return

    await asyncio.to_thread(_grant_premium, target_id, days)
    log.info("Админ %s начислил %s дней Premium юзеру %s", message.from_user.id, days, target_id)
    await message.answer(
        f"✅ Юзеру {target_id} успешно начислено {days} дней Premium."
    )
    try:  # уведомить самого юзера (мог не общаться с ботом / заблокировать)
        await bot.send_message(
            target_id,
            f"✨ Администратор активировал вам Premium-доступ на {days} дней!",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Уведомление о ручном Premium юзеру %s не доставлено: %s", target_id, exc)


__all__ = ["router", "IsAdmin", "AdminStates"]
