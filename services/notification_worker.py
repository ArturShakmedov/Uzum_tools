"""Фоновый воркер Live-ленты уведомлений (Фича №3).

Раз в POLL_INTERVAL секунд обходит все активные магазины (один на telegram_id),
тянет лёгкую дельту заказов за последние WINDOW_MINUTES минут и шлёт в Telegram:
  • 💰 новый заказ — с расчётом чистой прибыли (себестоимость + комиссия категории
    + логистика Uzum, через services.calculator);
  • ⚠️ возврат — когда уже известный заказ сменил статус на RETURNED.

Архитектура (под наш стек Postgres + Redis):
  • сеть (httpx-sync UzumClient) — в asyncio.to_thread под общим FETCH_SEMAPHORE
    (тот же потолок RAM/конкуренции, что и у полного синка);
  • БД (sync SQLAlchemy) — тоже в to_thread; параллельную запись держит Postgres
    (MVCC), глобального write-лока нет;
  • дедупликация — сверка id заказа с таблицей orders (PostgreSQL): незнакомый id
    → новый заказ; знакомый со сменой статуса на RETURNED → возврат. После показа
    статус сохраняется, поэтому повторов нет.
  • отправка (bot.send_message) — в основном event loop, вне потоков.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass
from html import escape

from aiogram import Bot
from sqlalchemy import select

from api.client import UzumClient
from api.endpoints import UzumAPI
from database.connection import session_scope
from database.models import Order, UserProduct
from database.repository import (
    SyncReport,
    _parse_dt,
    get_category,
    list_all_active_shops,
    list_premium_telegram_ids,
    save_orders,
    sync_fbs_orders,
)
from services.calculator import auto_weight_class, compute_unit_economics
from services.products import sku_suffix
from services.uzum_sync import FETCH_SEMAPHORE
from utils.logger import get_logger

log = get_logger(__name__)

# Раз в 10 минут обходим всех; «новыми» считаем заказы за последний час.
POLL_INTERVAL = 600
WINDOW_MINUTES = 60

# Статусы Uzum: новый FBS-заказ стартует в CREATED; возврат — RETURNED.
_NEW_STATUS = "CREATED"
_RETURN_STATUSES = ("RETURNED",)

_UTC = dt.timezone.utc


@dataclass(frozen=True)
class ActiveShop:
    """Снимок активного магазина (без ORM-привязки — безопасно между потоками)."""

    telegram_id: int
    shop_id: int
    token: str


@dataclass(frozen=True)
class NotifyEvent:
    """Готовое уведомление к отправке."""

    kind: str   # "new" | "return"
    text: str   # HTML


# --------------------------------------------------------------------------- #
#  Утилиты
# --------------------------------------------------------------------------- #
def _fmt(value) -> str:
    try:
        return f"{int(round(value)):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _window() -> tuple[int, dt.datetime]:
    """(cutoff в unix-ms, cutoff как datetime UTC) — нижняя граница «новых»."""
    cutoff = dt.datetime.now(_UTC) - dt.timedelta(minutes=WINDOW_MINUTES)
    return int(cutoff.timestamp() * 1000), cutoff


# --------------------------------------------------------------------------- #
#  Сетевая фаза (sync httpx, в to_thread под FETCH_SEMAPHORE)
# --------------------------------------------------------------------------- #
def fetch_recent_orders(token: str, shop_id: int, cutoff_ms: int) -> list[dict]:
    """Лёгкая дельта: страница новых (CREATED, c date_from) + страница возвратов.

    Всего ~2 запроса на магазин за цикл. Возвраты без date_from — у них старая
    dateCreated; «свежесть» возврата ловится сменой статуса при дедупликации.
    """
    out: dict[int, dict] = {}
    with UzumClient(token=token) as client:
        api = UzumAPI(client)
        for i, status in enumerate((_NEW_STATUS, *_RETURN_STATUSES)):
            if i:
                # Микро-пауза между статусами одного токена (CREATED → RETURNED),
                # чтобы страницы не уходили в одну секунду и не дёргали 429.
                # Функция синхронная (to_thread), поэтому time.sleep, не asyncio.
                time.sleep(0.5)
            date_from = cutoff_ms if status == _NEW_STATUS else None
            for o in api.orders.list_page(
                [shop_id], status=status, date_from=date_from, page=0, size=50
            ):
                oid = o.get("id")
                if oid is not None:
                    out[oid] = o
    return list(out.values())


# --------------------------------------------------------------------------- #
#  Дедупликация + расчёт прибыли + рендер (sync БД, в to_thread)
# --------------------------------------------------------------------------- #
def detect_events(
    telegram_id: int, orders: list[dict], cutoff_dt: dt.datetime
) -> list[NotifyEvent]:
    """Сверить заказы с PostgreSQL, сохранить новые/обновлённые, собрать события.

    • незнакомый id + статус CREATED + создан в окне → 💰 новый заказ (с прибылью);
    • знакомый id, статус стал RETURNED → ⚠️ возврат;
    • прочее — молча сохраняем (дедуп), чтобы не слать дубли в следующих циклах.
    """
    events: list[NotifyEvent] = []
    with session_scope() as session:
        ids = [o["id"] for o in orders if o.get("id") is not None]
        if not ids:
            return events
        # Мост FBS-логистики: дельта заказов воркера освежает fbs_orders каждые
        # POLL_INTERVAL сек — таймер дедлайнов /fbs видит переходы статусов
        # (CREATED → PACKING → PENDING_DELIVERY → …) между полными синками.
        sync_fbs_orders(session, telegram_id, orders)
        existing: dict[int, str | None] = {
            row.uzum_id: row.status
            for row in session.execute(
                select(Order.uzum_id, Order.status).where(
                    Order.telegram_id == telegram_id, Order.uzum_id.in_(ids)
                )
            )
        }
        report = SyncReport()
        for o in orders:
            oid = o.get("id")
            if oid is None:
                continue
            status = o.get("status")

            if oid not in existing:
                created = _parse_dt(o.get("dateCreated"))
                is_fresh_new = (
                    status == _NEW_STATUS
                    and created is not None
                    and created >= cutoff_dt
                )
                save_orders(session, telegram_id, [o], report)  # дедуп: фиксируем id
                if is_fresh_new:
                    net, approx = _estimate_profit(session, telegram_id, o)
                    events.append(
                        NotifyEvent("new", _format_new(session, telegram_id, o, net, approx))
                    )
            elif status in _RETURN_STATUSES and existing[oid] not in _RETURN_STATUSES:
                save_orders(session, telegram_id, [o], report)  # обновит статус → нет повторов
                events.append(
                    NotifyEvent("return", _format_return(session, telegram_id, o))
                )
    return events


def _resolve_product(session, telegram_id: int, article: str | None) -> UserProduct | None:
    if not article:
        return None
    return session.execute(
        select(UserProduct).where(
            UserProduct.telegram_id == telegram_id, UserProduct.article == article
        )
    ).scalars().first()


def _estimate_profit(session, telegram_id: int, order: dict) -> tuple[int, bool]:
    """Чистая прибыль по заказу, та же модель, что в аналитике/калькуляторе.

    Выручка позиции = цена заказа / число позиций (как в CTE аналитики). Если у
    SKU задана закупка И известна комиссия категории — считаем полную юнит-эконо-
    мику (себестоимость + комиссия + логистика Uzum + налог, services.calculator).
    Иначе fallback «выручка − закупка» и пометка «≈ оценка». Возвращает (net, approx).
    """
    items = order.get("orderItems") or []
    price = order.get("price") or 0
    line_rev = price / (len(items) or 1)

    total = 0.0
    approx = False
    for it in items:
        up = _resolve_product(session, telegram_id, it.get("skuTitle"))
        purchase = up.purchase_price if up else None
        cat = get_category(session, up.category_id) if (up and up.category_id) else None
        comm = cat.comm_fbs if cat else None
        is_kgt = bool(cat.is_kgt) if cat else False
        if purchase is not None and comm is not None:
            econ = compute_unit_economics(
                sell_price=line_rev, purchase=purchase, extra=0,
                comm_pct=comm, weight_class=auto_weight_class(line_rev, is_kgt) or "mgt",
            )
            total += econ.net_profit
        else:
            total += line_rev - (purchase or 0)
            approx = True
    return int(round(total)), approx


def _title_size(session, telegram_id: int, items: list[dict]) -> tuple[str, str | None]:
    """Название + размер для шапки уведомления (по первой позиции заказа)."""
    if not items:
        return ("Заказ", None)
    it = items[0]
    article = it.get("skuTitle")
    up = _resolve_product(session, telegram_id, article)
    title = (up.title if up and up.title else None) or it.get("productTitle") \
        or it.get("skuTitle") or "Товар"
    return (title, sku_suffix(article) if article else None)


def _head(title: str, size: str | None, extra: str = "") -> str:
    sized = f" ({escape(size)})" if size else ""
    return f"📦 {escape(title)}{sized}{extra}"


def _format_new(session, telegram_id: int, order: dict, net: int, approx: bool) -> str:
    items = order.get("orderItems") or []
    price = order.get("price") or 0
    title, size = _title_size(session, telegram_id, items)
    extra = f" +{len(items) - 1} поз." if len(items) > 1 else ""
    sign = "+" if net >= 0 else "−"
    tail = " <i>(≈ оценка)</i>" if approx else "!"
    return (
        "💰 <b>Новый заказ!</b>\n"
        f"{_head(title, size, extra)}\n"
        f"💵 Цена: <b>{_fmt(price)}</b> сум\n"
        f"📈 Чистая прибыль: <b>{sign}{_fmt(abs(net))}</b> сум{tail}"
    )


def _format_return(session, telegram_id: int, order: dict) -> str:
    items = order.get("orderItems") or []
    title, size = _title_size(session, telegram_id, items)
    return (
        "⚠️ <b>Возврат товара!</b>\n"
        f"{_head(title, size)}\n"
        "↩️ Статус: <b>Возвращено на склад</b>"
    )


# --------------------------------------------------------------------------- #
#  Оркестрация цикла
# --------------------------------------------------------------------------- #
def _load_active_shops() -> list[ActiveShop]:
    with session_scope() as session:
        # Live-уведомления — Premium-фича: free-юзеров не дёргаем.
        premium = list_premium_telegram_ids(session)
        return [
            ActiveShop(s.telegram_id, s.uzum_shop_id, s.uzum_token)
            for s in list_all_active_shops(session)
            if s.telegram_id in premium
        ]


async def _process_shop(bot: Bot, shop: ActiveShop) -> None:
    cutoff_ms, cutoff_dt = _window()
    try:
        # Сеть под общим FETCH_SEMAPHORE — единый потолок RAM/конкуренции на процесс.
        async with FETCH_SEMAPHORE:
            orders = await asyncio.to_thread(
                fetch_recent_orders, shop.token, shop.shop_id, cutoff_ms
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("Live-уведомления: сбор заказов для %s не удался: %s", shop.telegram_id, exc)
        return
    if not orders:
        return

    events = await asyncio.to_thread(detect_events, shop.telegram_id, orders, cutoff_dt)
    for ev in events:
        try:
            await bot.send_message(shop.telegram_id, ev.text)
        except Exception as exc:  # noqa: BLE001 — юзер заблокировал бота и т.п.
            log.warning("Live-уведомления: отправка %s юзеру %s не удалась: %s",
                        ev.kind, shop.telegram_id, exc)


async def run_notification_cycle(bot: Bot) -> None:
    """Один проход по всем активным магазинам.

    Между магазинами — джиттер `asyncio.sleep(1.5)`: запуск разнесён во времени,
    чтобы запросы разных токенов к /v2/fbs/orders не улетали в одну секунду
    (чистим логи от кратковременных 429). Сами магазины при этом обрабатываются
    параллельно (под FETCH_SEMAPHORE) — пауза лишь сдвигает старт, не сериализует.
    """
    shops = await asyncio.to_thread(_load_active_shops)
    if not shops:
        return
    tasks: list[asyncio.Task] = []
    for i, shop in enumerate(shops):
        if i:
            await asyncio.sleep(1.5)   # джиттер между магазинами
        tasks.append(asyncio.create_task(_process_shop(bot, shop)))
    await asyncio.gather(*tasks, return_exceptions=True)


async def notification_loop(bot: Bot) -> None:
    """Бесконечный цикл Live-ленты (каждые POLL_INTERVAL секунд)."""
    log.info("Live-уведомления: воркер запущен (интервал %d с, окно %d мин).",
             POLL_INTERVAL, WINDOW_MINUTES)
    while True:
        try:
            await run_notification_cycle(bot)
        except Exception:  # noqa: BLE001 — цикл не должен падать целиком
            log.exception("Live-уведомления: цикл завершился с ошибкой")
        await asyncio.sleep(POLL_INTERVAL)


def start_notifications(bot: Bot) -> asyncio.Task:
    """Запустить воркер фоновой задачей (вызывать из работающего event loop)."""
    return asyncio.create_task(notification_loop(bot))


__all__ = [
    "notification_loop",
    "run_notification_cycle",
    "start_notifications",
    "fetch_recent_orders",
    "detect_events",
    "NotifyEvent",
    "ActiveShop",
    "POLL_INTERVAL",
    "WINDOW_MINUTES",
]
