"""Модуль FBS-логистики: акты приёма-передачи + таймер дедлайнов отгрузки.

/fbs → меню из двух разделов:
  • 📄 Акты отправки (fbs:acts)   — последние 5 ShippingAct юзера со ссылками на PDF;
  • ⏰ Текущие поставки / Таймер (fbs:timer) — активные FBSOrder через calculate_fbs_deadline,
    отсортированные по критичности (горящие 🔴 первыми), чтобы селлер успел
    отгрузиться до штрафа.

i18n: ВСЕ пользовательские строки — через gettext `_()` (ru/uz/en). Вызовы `_()`
только в рантайме хэндлеров (под локаль из I18n-middleware), на уровне модуля
строк нет. Математика дедлайна — utils.fbs_calc (без локализации).
"""

from __future__ import annotations

import asyncio
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.i18n import gettext as _
from aiogram.utils.i18n import lazy_gettext as __
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database.connection import session_scope
from database.repository import (
    ASSEMBLED_FBS_STATUSES,
    list_active_fbs_orders,
    list_shipping_acts,
)
from keyboards.menu import BTN_FBS_MSGID
from utils.fbs_calc import FBS_DEADLINE_HOURS, calculate_fbs_deadline
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="fbs")


def _fbs_menu_kb() -> InlineKeyboardMarkup:
    """Меню /fbs: две кнопки вертикально (adjust(1) — один столбец).

    Тексты — через `_()` в момент рендера: клавиатура собирается в рантайме
    хэндлера под локаль юзера из I18n-middleware (ru/uz/en).
    """
    builder = InlineKeyboardBuilder()
    builder.button(text=_("📄 Акты отправки"), callback_data="fbs:acts")
    builder.button(text=_("⏰ Текущие поставки (Таймер)"), callback_data="fbs:timer")
    builder.adjust(1)
    return builder.as_markup()


@router.message(Command("fbs"))
@router.message(F.text == __(BTN_FBS_MSGID))
async def cmd_fbs(message: Message) -> None:
    """Меню FBS-логистики: команда /fbs ИЛИ кнопка «🚚 FBS Логистика» главного меню.

    Фильтр кнопки — через lazy_gettext (`__`), НЕ обычный `_()`: прокси
    резолвится в момент проверки фильтра внутри i18n-контекста апдейта, поэтому
    матчит локализованный текст кнопки каждой локали (ru/uz/en). Обычный `_()`
    в декораторе падал бы LookupError при импорте модуля (контекста ещё нет).
    """
    await message.answer(
        _(
            "🚚 <b>FBS-логистика</b>\n\n"
            "Акты приёма-передачи и контроль дедлайнов отгрузки — выберите раздел:"
        ),
        reply_markup=_fbs_menu_kb(),
    )


# --------------------------------------------------------------------------- #
#  📄 Акты отправки
# --------------------------------------------------------------------------- #
def _load_acts(telegram_id: int) -> list[dict[str, Any]]:
    """Последние 5 актов юзера (маппинг в dict — сессия закрывается в потоке)."""
    with session_scope() as session:
        return [
            {
                "act_number": a.act_number,
                "total_items": a.total_items,
                "created_at": a.created_at,
                "pdf_url": a.pdf_url,
            }
            for a in list_shipping_acts(session, telegram_id, limit=5)
        ]


@router.callback_query(F.data == "fbs:acts")
async def on_fbs_acts(callback: CallbackQuery) -> None:
    acts = await asyncio.to_thread(_load_acts, callback.from_user.id)
    if not acts:
        await callback.answer(
            _("Актов отправки пока нет — они появятся после первой отгрузки."),
            show_alert=True,
        )
        return

    lines = [_("📄 <b>Последние акты отправки:</b>"), ""]
    for act in acts:
        when = act["created_at"].strftime("%d.%m.%Y") if act["created_at"] else "—"
        line = _("▪️ Акт №{number} от {date} — Количество: {items} шт.").format(
            number=escape(act["act_number"]), date=when, items=act["total_items"],
        )
        if act["pdf_url"]:
            line += " " + _('<a href="{url}">[Посмотреть акт]</a>').format(
                url=escape(act["pdf_url"], quote=True)
            )
        lines.append(line)

    await callback.message.answer("\n".join(lines), disable_web_page_preview=True)
    await callback.answer()


# --------------------------------------------------------------------------- #
#  ⏰ Таймер дедлайнов
# --------------------------------------------------------------------------- #
def _load_active_orders(telegram_id: int) -> list[dict[str, Any]]:
    with session_scope() as session:
        return [
            {
                "uzum_order_id": o.uzum_order_id,
                "sku_title": o.sku_title,
                "order_created_at": o.order_created_at,
                "status": o.status,   # NEW/PACKING → сборка; DELIVERY/SHIPPING → довезти
            }
            for o in list_active_fbs_orders(session, telegram_id)
        ]


@router.callback_query(F.data == "fbs:timer")
async def on_fbs_timer(callback: CallbackQuery) -> None:
    orders = await asyncio.to_thread(_load_active_orders, callback.from_user.id)
    if not orders:
        await callback.answer(
            _("Активных FBS-заказов нет — дедлайны не горят 👌"), show_alert=True
        )
        return

    # Считаем дедлайн каждого и сортируем по критичности: горящие — первыми.
    ranked = sorted(
        ((calculate_fbs_deadline(o["order_created_at"]), o) for o in orders),
        key=lambda pair: pair[0]["seconds_left"],
    )

    lines = [
        _("⏰ <b>Дедлайны отгрузки FBS</b> (регламент: {hours} ч. на сборку):").format(
            hours=FBS_DEADLINE_HOURS
        ),
        _("<i>🟢 безопасно (&gt;12 ч.) · 🟡 срочно (4–12 ч.) · 🔴 горишь (&lt;4 ч.)</i>"),
        "",
    ]
    for deadline, order in ranked:
        oid = escape(order["uzum_order_id"])
        title = escape(order["sku_title"])
        # Подсказка этапа: «В поставке» (DELIVERY/SHIPPING) — товар уже собран,
        # его осталось довезти; иначе — сначала сборка и упаковка.
        if order["status"] in ASSEMBLED_FBS_STATUSES:
            action = _("🚐 Собран — доставь на пункт приёма!")
        else:
            action = _("📦 Собери и упакуй товар")
        if deadline["overdue"]:
            lines.append(
                _("Заказ #{oid} ({title}) — ⚠️ <b>ПРОСРОЧЕН</b>, риск штрафа! 🔴").format(
                    oid=oid, title=title
                )
            )
        else:
            lines.append(
                _("Заказ #{oid} ({title}) — Оставшееся время: {hours} ч. {minutes} мин. {emoji} · {action}").format(
                    oid=oid,
                    title=title,
                    hours=deadline["hours"],
                    minutes=deadline["minutes"],
                    emoji=deadline["emoji"],
                    action=action,
                )
            )

    await callback.message.answer("\n".join(lines))
    await callback.answer()


__all__ = ["router"]
