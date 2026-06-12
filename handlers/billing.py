"""Биллинг: оплата Premium по утверждённой тарифной сетке (UZS, Telegram Payments).

Поток: «💳 Продлить подписку»/«💎 Купить Premium»/`/premium` → меню тарифов
(billing:choose_plan) → выбор (sub:buy:<payload>) → answer_invoice → PreCheckoutQuery
→ SUCCESSFUL_PAYMENT → начисление дней пакета + plan_name в БД.

Цены/дни — единый источник config.PREMIUM_PACKAGES (key `1_month`/…); payload инвойса
= `premium_<key>` (premium_1_month/…). Этот роутер НЕ под SubscriptionMiddleware.
"""

from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from sqlalchemy.exc import IntegrityError

from config import CLICK_PROVIDER_TOKEN, PREMIUM_DAYS, PREMIUM_PACKAGES
from database.connection import session_scope
from database.repository import (
    activate_premium,
    complete_payment_log,
    create_payment_log,
)
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="billing")

_DESCRIPTION = (
    "Доступ к аналитике невозвратов, лимитам менеджеров и скорингу карточек Uzum Tools."
)

# Презентация тарифа по payload (эмодзи + чистый title). Деньги/дни — из
# PREMIUM_PACKAGES[key] (key = payload без префикса 'premium_'). amount = price_uzs×100.
_PLAN_VIEW: dict[str, tuple[str, str, str]] = {   # payload → (key, emoji, title)
    "premium_1_month":  ("1_month",  "📦", "Premium 1 месяц"),
    "premium_3_months": ("3_months", "🔥", "Premium 3 месяца"),
    "premium_6_months": ("6_months", "🚀", "Premium 6 месяцев"),
    "premium_1_year":   ("1_year",   "🌟", "Premium 1 год"),
}


def _fmt_uzs(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _choose_plan_kb() -> InlineKeyboardMarkup:
    """Меню выбора тарифа: по строке на пакет, callback `sub:buy:<payload>`."""
    rows = []
    for payload, (key, emoji, title) in _PLAN_VIEW.items():
        price = PREMIUM_PACKAGES[key]["price_uzs"]
        rows.append([InlineKeyboardButton(
            text=f"{emoji} {title} — {_fmt_uzs(price)} сум",
            callback_data=f"sub:buy:{payload}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


_CHOOSE_TEXT = "💳 <b>Выберите тариф Premium</b>\nЧем длиннее пакет — тем выгоднее:"


@router.message(Command("premium"))
async def cmd_premium(message: Message) -> None:
    await message.answer(_CHOOSE_TEXT, reply_markup=_choose_plan_kb())


@router.callback_query(F.data == "billing:choose_plan")
async def on_choose_plan(callback: CallbackQuery) -> None:
    """Показать меню выбора тарифа (из профиля / оффера блокировки / команды)."""
    await callback.message.answer(_CHOOSE_TEXT, reply_markup=_choose_plan_kb())
    await callback.answer()


def _log_created(telegram_id: int, payload: str, amount_uzs: int) -> None:
    with session_scope() as session:
        create_payment_log(session, telegram_id, payload, amount_uzs)


@router.callback_query(F.data.startswith("sub:buy:"))
async def on_buy(callback: CallbackQuery) -> None:
    """Выставить инвойс на выбранный тариф (точная сумма в тийинах)."""
    payload = callback.data.removeprefix("sub:buy:")
    view = _PLAN_VIEW.get(payload)
    if view is None:
        await callback.answer("Тариф не найден.", show_alert=True)
        return
    if not CLICK_PROVIDER_TOKEN:
        await callback.answer("💳 Оплата пока не настроена администратором.", show_alert=True)
        log.warning("answer_invoice пропущен: CLICK_PROVIDER_TOKEN пуст.")
        return

    key, _emoji, title = view
    price_uzs = PREMIUM_PACKAGES[key]["price_uzs"]
    amount = price_uzs * 100                            # сум × 100 = тийины

    # Аудит: фиксируем «created» ДО инвойса — если Click зависнет, запись останется.
    await asyncio.to_thread(_log_created, callback.from_user.id, payload, price_uzs)

    await callback.message.answer_invoice(
        title=title,
        description=_DESCRIPTION,
        provider_token=CLICK_PROVIDER_TOKEN,
        currency="UZS",
        start_parameter="premium-subscription",
        payload=payload,                                # premium_1_month / …
        prices=[LabeledPrice(label="Uzum Tools Premium", amount=amount)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    """Подтвердить готовность принять платёж (ответить ≤10 c)."""
    await query.answer(ok=True)


def _plan_for_payload(payload: str | None) -> tuple[int, str]:
    """payload → (дни, человекочитаемое название тарифа)."""
    view = _PLAN_VIEW.get(payload or "")
    if view is not None:
        key, _emoji, title = view
        return int(PREMIUM_PACKAGES[key]["days"]), title
    return PREMIUM_DAYS, "Premium"                       # фолбэк (легаси/неизвестный payload)


def _apply_payment(
    telegram_id: int, days: int, plan_name: str, payload: str,
    charge_id: str | None, amount_uzs: int,
) -> None:
    with session_scope() as session:
        # Сначала закрываем аудит (created → completed) с charge_id: flush внутри
        # упрётся в unique при повторной обработке того же платежа (IntegrityError)
        # ДО начисления дней — двойного апдейта подписки не будет.
        complete_payment_log(
            session, telegram_id, payload, charge_id=charge_id, amount=amount_uzs
        )
        # activate_premium: если подписка активна — прибавляет дни к expires_at,
        # иначе от now; ставит plan_name и subscription_tier='premium'.
        # Строка User — под SELECT ... FOR UPDATE (защита от гонки начислений).
        activate_premium(session, telegram_id, days=days, plan_name=plan_name)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    """Платёж прошёл → начислить дни пакета, обновить тариф, закрыть лог, выдать чек."""
    payment = message.successful_payment
    payload = payment.invoice_payload
    charge_id = payment.telegram_payment_charge_id
    days, plan_name = _plan_for_payload(payload)
    try:
        await asyncio.to_thread(
            _apply_payment, message.from_user.id, days, plan_name, payload,
            charge_id, payment.total_amount // 100,   # тийины → сумы
        )
    except IntegrityError:
        # Telegram передоставил апдейт (рестарт/сбой offset'а) — платёж уже учтён.
        log.warning(
            "Повторный SUCCESSFUL_PAYMENT (charge_id=%s, user=%s) — начисление пропущено.",
            charge_id, message.from_user.id,
        )
        return
    log.info("Premium активирован для %s: %s (+%d дн., payload=%s, charge=%s)",
             message.from_user.id, plan_name, days, payload, charge_id)
    await message.answer(
        f"🎉 <b>Оплата прошла успешно!</b> Ваш тариф обновлён до "
        f"<b>{plan_name}</b>. Доступ предоставлен."
    )


__all__ = ["router"]
