"""Кнопки отчётов «⏳ Не вернулись» / «🚨 Утерянные»: кэш-синк + Excel.

Общий кэш-механизм (проверка активного магазина, анти-спам, 30-мин кэш,
двухфазный синк: сеть параллельно + запись single-writer) вынесен в
handlers.common.ensure_fresh.
"""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.types import FSInputFile, Message

from handlers.common import ensure_fresh, resolve_owner
from keyboards.menu import REPORT_BUTTONS, main_menu_kb
from services.analytics import generate_loss_report
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="analytics")


@router.message(F.text.in_(set(REPORT_BUTTONS)))
async def on_get_report(message: Message) -> None:
    # Менеджер видит аналитику ВЛАДЕЛЬЦА: данные грузим по effective_data_owner.
    telegram_id = await resolve_owner(message.from_user.id)
    report_type = REPORT_BUTTONS[message.text]  # "transit" | "lost"

    status = await ensure_fresh(message, telegram_id)
    if status is None:  # анти-спам / нет магазина / ошибка синка — уже отвечено
        return

    try:
        report = await asyncio.to_thread(generate_loss_report, telegram_id, report_type)
    except Exception as exc:  # noqa: BLE001
        log.exception("Report failed for %s", telegram_id)
        # escape: текст ошибки может содержать '<'/'>' и ломать HTML-парсер Telegram.
        await status.edit_text(escape(f"❌ Не удалось построить отчёт: {exc}"))
        return

    if not report.has_rows:
        empty = (
            "✅ Нет товаров в пути (<30 дней) — всё либо принято, либо уже просрочено."
            if report_type == "transit"
            else "✅ Утерянных товаров (30+ дней) не обнаружено."
        )
        await status.edit_text(escape(empty))
        return

    if report_type == "transit":
        caption = (
            "⏳ Ещё не вернулись (<30 дней)\n"
            f"• Позиций в пути/ожидании: {report.row_count}\n"
            f"• Всего возвратов/отмен: {report.total_returns}"
        )
    else:
        caption = (
            "🚨 Утеряны (30+ дней) — для претензии\n"
            f"• Позиций к претензии: {report.row_count}\n"
            f"• Всего возвратов/отмен: {report.total_returns}"
        )
    # escape: подпись содержит '<30 дней' — без экранирования Telegram падает.
    await message.answer_document(
        FSInputFile(report.path), caption=escape(caption), reply_markup=main_menu_kb()
    )
    await status.delete()


__all__ = ["router"]
