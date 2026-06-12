"""Кнопка «💰 Выплаты и баланс»: финсводка активного магазина.

Финансы развязаны от тяжёлого общего синка и имеют свой быстрый путь:
  • есть свежий снимок (<30 мин) → отдаём мгновенно из БД (0 запросов к API);
  • снимок устарел, но есть → показываем кэш сразу + обновляем в фоне;
  • снимка нет → грузим с ЖЁСТКИМ timeout; на медленный/упавший Uzum — понятная
    заглушка, без блокировки бота (тяжёлое — в asyncio.to_thread под asyncio.timeout).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html

from aiogram import F, Router
from aiogram.types import Message

from database.connection import session_scope
from database.repository import get_active_shop, get_finance_snapshot
from keyboards.menu import BTN_FINANCE
from services.uzum_api import refresh_finance
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="finance")

_UTC = dt.timezone.utc
_CACHE_TTL = dt.timedelta(minutes=30)
_FIRST_LOAD_TIMEOUT = 20.0  # сек: максимум, сколько ждём первую загрузку финансов

# Дедуп обновлений финансов на пользователя (чтобы не плодить фоновые задачи).
_refreshing: set[int] = set()

_STATUS_RU = {
    "CONFIRMED": "Выплачено",
    "CREATED": "Создано",
    "REFUNDED": "Возвращено",
    "CANCELED": "Отменено",
}


def _load_finance(telegram_id: int) -> dict | None:
    """Снимок финансов + название магазина + время обновления (plain dict)."""
    with session_scope() as session:
        shop = get_active_shop(session, telegram_id)
        if shop is None:
            return None
        snap = get_finance_snapshot(session, telegram_id)
        return {
            "shop_name": shop.shop_name or str(shop.uzum_shop_id),
            "synced_at": snap.finance_synced_at if snap else None,
            "snapshot": None if snap is None else {
                "available": snap.available,
                "pending": snap.pending,
                "commissions": snap.commissions,
                "payments": snap.payments or [],
                "has_data": snap.has_data,
            },
        }


def _is_fresh(synced_at: dt.datetime | None) -> bool:
    if synced_at is None:
        return False
    aware = synced_at if synced_at.tzinfo else synced_at.replace(tzinfo=_UTC)
    return (dt.datetime.now(_UTC) - aware) < _CACHE_TTL


def _fmt_dt(synced_at: dt.datetime | None) -> str:
    if synced_at is None:
        return "—"
    aware = synced_at if synced_at.tzinfo else synced_at.replace(tzinfo=_UTC)
    return aware.strftime("%Y-%m-%d %H:%M UTC")


def _fmt_sum(value) -> str:
    try:
        return f"{int(value or 0):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _format_finance(shop_name: str, snap: dict) -> str:
    lines = [
        f"🏪 <b>Магазин:</b> {html.escape(shop_name)}",
        f"💵 <b>Доступно к выводу:</b> {_fmt_sum(snap['available'])} сум",
        f"⏳ <b>В ожидании (заморожено):</b> {_fmt_sum(snap['pending'])} сум",
        f"📉 <b>Удержания / Комиссии за период:</b> {_fmt_sum(snap['commissions'])} сум",
    ]
    payments = snap.get("payments") or []
    if payments:
        lines.append("💳 <b>Последние выплаты на расчётный счёт:</b>")
        for p in payments:
            day = (p.get("date") or "")[:10] or "—"
            amount = _fmt_sum(p.get("amount"))
            status = _STATUS_RU.get(p.get("status"), p.get("status") or "")
            name = (p.get("name") or "").strip()
            parts = [f"{amount} сум"]
            if name:
                parts.append(html.escape(name))
            if status:
                parts.append(f"Статус: {status}")
            lines.append(f"  • {day}: " + " · ".join(parts))
    else:
        lines.append("💳 <b>Последние выплаты на р/с:</b> за период не обнаружены.")
    return "\n".join(lines)


async def _do_refresh(telegram_id: int) -> bool:
    """Обновить финансы (под дедупом). True — снимок успешно обновлён."""
    if telegram_id in _refreshing:
        return False
    _refreshing.add(telegram_id)
    try:
        return await asyncio.to_thread(refresh_finance, telegram_id)
    finally:
        _refreshing.discard(telegram_id)


async def _bg_refresh(telegram_id: int) -> None:
    try:
        await _do_refresh(telegram_id)
    except Exception:  # noqa: BLE001
        log.warning("Фоновое обновление финансов %s не удалось", telegram_id, exc_info=True)


@router.message(F.text == BTN_FINANCE)
async def on_finance(message: Message) -> None:
    telegram_id = message.from_user.id

    data = await asyncio.to_thread(_load_finance, telegram_id)
    if data is None:
        await message.answer("Сначала подключите магазин — команда /start.")
        return

    snap, synced = data["snapshot"], data["synced_at"]

    # 1) Есть пригодный кэш → отдаём МГНОВЕННО (0 запросов к API).
    if snap and snap.get("has_data"):
        text = _format_finance(data["shop_name"], snap)
        if not _is_fresh(synced):
            text += f"\n\n🕒 Обновлено: {_fmt_dt(synced)} · актуализирую в фоне…"
            asyncio.create_task(_bg_refresh(telegram_id))
        await message.answer(text)
        return

    # 2) Кэша нет (первый раз) → грузим с жёстким timeout, без подвисания бота.
    status = await message.answer("🔄 Загружаю финансы из Uzum…")
    try:
        async with asyncio.timeout(_FIRST_LOAD_TIMEOUT):
            ok = await _do_refresh(telegram_id)
    except TimeoutError:
        asyncio.create_task(_bg_refresh(telegram_id))  # дотянем в фоне
        await status.edit_text(
            "⚠️ Uzum отвечает слишком медленно. Догружаю в фоне — нажмите 💰 через минуту."
        )
        return
    except Exception:  # noqa: BLE001
        log.exception("Первичная загрузка финансов %s не удалась", telegram_id)
        await status.edit_text("⚠️ Данные Uzum временно недоступны. Попробуйте позже.")
        return

    if not ok:
        await status.edit_text(
            "⚠️ Не удалось получить финансы (медленный ответ или сбой API). Попробуйте позже."
        )
        return

    data2 = await asyncio.to_thread(_load_finance, telegram_id)
    snap2 = data2["snapshot"] if data2 else None
    if snap2 and snap2.get("has_data"):
        await status.edit_text(_format_finance(data2["shop_name"], snap2))
    else:
        await status.edit_text(
            "💰 Финансовые данные для этого магазина пока отсутствуют "
            "или эндпоинт временно недоступен."
        )


__all__ = ["router"]
