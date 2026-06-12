"""Аналитика невозвратов: что зависло в ПВЗ или потерялось по дороге на склад.

Бизнес-правило Uzum: когда товар получает статус RETURNED/CANCELED в ПВЗ,
у Uzum есть 7 дней, чтобы довезти его на склад и выдать продавцу по накладной
(Invoice). Факт выдачи = появление штрихкода товара в таблице `barcodes`.

ВАЖНО: накладные возврата НЕ ссылаются на номер заказа — Uzum связывает их по
SKU/штрихкоду самого товара. Поэтому сопоставление идёт не «заказ↔накладная», а
хронологической очередью (FIFO): из накладных собирается пул товаров, а возвраты
заказов (от старых к новым) «гасят» по одному экземпляру из пула.

Два нюанса данных:
  • у позиций заказа из /v2/fbs/orders нет skuId — матчим по текстовому артикулу
    (skuTitle, напр. 'DUALLOK-САРАФА3-КОРИЧН-L'), он есть и в заказах, и в накладных;
  • строка barcodes = одна SKU-линия накладной с полем amount, поэтому в пул
    кладём amount физических единиц, а не одну запись на линию.
"""

from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database.models import Barcode, Invoice, Order, OrderItem, ReturnItem, SkuBarcode

# Статусы заказа, по которым товар обязан вернуться на склад.
WATCHED_STATUSES: tuple[str, ...] = ("RETURNED", "CANCELED")
DEFAULT_THRESHOLD_DAYS = 7

# Вердикты проверки.
RESULT_ACCEPTED = "✅ Принято на склад"
RESULT_IN_TRANSIT = "🚚 В транзите (едет на склад)"
RESULT_LOST = "⚠️ НЕ ВЕРНУЛИ (Сроки вышли!)"

# Заглушка, если штрихкод не нашёлся ни в каталоге, ни в накладных.
NO_BARCODE = "Нет штрихкода"
# Заглушка для человеческого названия товара, если его нет ни в каталоге, ни в заказе.
NO_NAME = "Неизвестный товар"

# Порядок серьёзности для сортировки отчёта (потери — наверх).
_SEVERITY = {RESULT_LOST: 0, RESULT_IN_TRANSIT: 1, RESULT_ACCEPTED: 2}

_UTC = dt.timezone.utc


@dataclass
class LossRow:
    """Одна строка отчёта по невозвратам."""

    order_id: int
    event_date: dt.datetime | None  # дата отсчёта SLA (возврат/отмена, фолбэк — создание)
    sku_id: int | None
    article: str | None             # артикул/skuTitle — то, по чему реально матчим
    barcode: str                    # штрихкод для претензии (или NO_BARCODE)
    title: str | None
    uzum_status: str
    days_elapsed: int | None
    result: str
    returned: bool


@dataclass
class ReturnsAnalysis:
    """Результат FIFO-сопоставления возвратов с пулом товаров из накладных."""

    rows: list[LossRow] = field(default_factory=list)
    total_returns: int = 0      # позиций RETURNED/CANCELED в заказах
    total_received: int = 0     # физических единиц в возвратах (пул) — ожидаемо 146
    matched: int = 0            # погашено (вернулось на склад)
    unmatched: int = 0          # не сопоставлено (в транзите + потеряно)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite отдаёт naive datetime — трактуем хранимое время как UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=_UTC)


def _event_date(order) -> dt.datetime | None:
    """Дата старта 7-дневного SLA: фактическое событие в ПВЗ, иначе создание.

    RETURNED → returnDate, CANCELED → dateCancelled. Если нужной даты нет в
    ответе API — откатываемся на dateCreated, чтобы отсчёт всё равно работал.
    """
    if order.status == "RETURNED":
        chosen = order.return_date
    elif order.status == "CANCELED":
        chosen = order.date_cancelled
    else:
        chosen = None
    return _aware(chosen or order.date_created)


def _norm(text: str | None) -> str | None:
    """Нормализовать текст для сравнения: схлопнуть пробелы и регистр."""
    if not text:
        return None
    return " ".join(str(text).split()).casefold()


def _match_key(sku_title, fallback_title, sku_id) -> str | None:
    """Единый ключ сопоставления для заказов и накладных.

    Приоритет — текстовый артикул skuTitle (есть в обеих таблицах), затем
    запасное название, затем sku_id. Главное — обе стороны строят ключ одинаково.
    """
    return _norm(sku_title) or _norm(fallback_title) or (
        str(sku_id) if sku_id is not None else None
    )


def _build_barcode_index(session: Session, telegram_id: int) -> dict[str, str]:
    """Полный словарь {ключ артикула → штрихкод} для отчёта (в рамках юзера).

    Приоритет — каталог из API (/v1/product/shop → SkuBarcode): он покрывает
    весь ассортимент. Затем дополняем данными из таблицы barcodes (FBS-накладные)
    для артикулов, которых нет в активном каталоге. Ключи строятся той же
    _match_key, что и при сопоставлении, поэтому гарантированно совпадают.
    """
    index: dict[str, str] = {}

    # 1) Каталог — приоритетный источник (первая запись по ключу побеждает).
    for sku_id, sku_title, full_title, product_title, barcode in session.execute(
        select(
            SkuBarcode.uzum_id,  # uzum_id = skuId каталога
            SkuBarcode.sku_title,
            SkuBarcode.sku_full_title,
            SkuBarcode.product_title,
            SkuBarcode.barcode,
        ).where(SkuBarcode.telegram_id == telegram_id)
    ).all():
        if not barcode:
            continue
        key = _match_key(sku_title, product_title or full_title, sku_id)
        if key:
            index.setdefault(key, str(barcode))

    # 2) Дополняем из barcodes тем, чего нет в каталоге.
    for sku_id, sku_title, title, barcode in session.execute(
        select(Barcode.sku_id, Barcode.sku_title, Barcode.title, Barcode.barcode)
        .where(Barcode.telegram_id == telegram_id)
    ).all():
        if not barcode:
            continue
        key = _match_key(sku_title, title, sku_id)
        if key:
            index.setdefault(key, str(barcode))

    return index


def _build_name_index(session: Session, telegram_id: int) -> dict[str, str]:
    """Словарь {ключ артикула → человеческое НАЗВАНИЕ товара} (в рамках юзера).

    Источник названий — каталог SkuBarcode.product_title (приоритет), затем
    barcodes.title (наименование из накладной). Ключи строятся той же _match_key,
    что и при сопоставлении. Нужен, чтобы в отчёте «Название» было реальным
    именем товара, а не его артикулом (SKU).
    """
    index: dict[str, str] = {}

    for sku_id, sku_title, full_title, product_title in session.execute(
        select(
            SkuBarcode.uzum_id,  # uzum_id = skuId каталога
            SkuBarcode.sku_title,
            SkuBarcode.sku_full_title,
            SkuBarcode.product_title,
        ).where(SkuBarcode.telegram_id == telegram_id)
    ).all():
        name = product_title or full_title
        if not name:
            continue
        key = _match_key(sku_title, name, sku_id)
        if key:
            index.setdefault(key, name)

    for sku_id, sku_title, title in session.execute(
        select(Barcode.sku_id, Barcode.sku_title, Barcode.title)
        .where(Barcode.telegram_id == telegram_id)
    ).all():
        if not title:
            continue
        key = _match_key(sku_title, title, sku_id)
        if key:
            index.setdefault(key, title)

    return index


def check_missing_goods(
    session: Session,
    telegram_id: int,
    *,
    threshold_days: int = DEFAULT_THRESHOLD_DAYS,
    now: dt.datetime | None = None,
    report_type: str | None = None,
) -> ReturnsAnalysis:
    """FIFO-сопоставление возвратов с пулом фактически принятых товаров.

    Алгоритм:
      1. Пул = все строки barcodes, сгруппированные по sku_id (мультимножество).
      2. Возвраты (order_items в RETURNED/CANCELED) сортируются по дате события
         от старых к новым.
      3. Для каждого: если в пуле есть экземпляр того же SKU — гасим возврат
         (✅ Принято на склад) и убираем один экземпляр из пула; иначе вердикт
         по сроку: ≤ threshold → 🚚 В транзите, > threshold → ⚠️ НЕ ВЕРНУЛИ.
    Отсчёт — от фактического события (returnDate/dateCancelled), фолбэк —
    dateCreated.

    report_type фильтрует НЕвозвращённые позиции по сроку давности:
      • "transit" — только 🚚 (со дня события прошло ≤ threshold_days);
      • "lost"    — только ⚠️ (прошло > threshold_days);
      • None      — все позиции (включая ✅), как раньше.
    Строки результата отсортированы: потери сверху.
    """
    now = now or dt.datetime.now(_UTC)

    # 1. Пул фактически принятых единиц — из ВОЗВРАТОВ (/v1/return → returnItems).
    #    Именно тут лежат все физические единицы за всё время (FBS-накладные дают
    #    лишь часть). Ключ — артикул (skuTitle), значение — сумма amount.
    pool: Counter = Counter()
    for sku_id, sku_title, product_title, amount in session.execute(
        select(
            ReturnItem.sku_id,
            ReturnItem.sku_title,
            ReturnItem.product_title,
            ReturnItem.amount,
        ).where(ReturnItem.telegram_id == telegram_id)
    ).all():
        key = _match_key(sku_title, product_title, sku_id)
        pool[key] += amount or 1
    total_received = sum(pool.values())

    # Справочники по артикулу: штрихкоды и человеческие названия (каталог + barcodes).
    barcode_by_key = _build_barcode_index(session, telegram_id)
    name_by_key = _build_name_index(session, telegram_id)

    # 2. Возвраты заказов, отсортированные по дате события (старые → новые).
    stmt = (
        select(OrderItem, Order)
        .join(Order, OrderItem.order_pk == Order.id)
        .where(Order.telegram_id == telegram_id, Order.status.in_(WATCHED_STATUSES))
    )
    returns: list[tuple[dt.datetime | None, OrderItem, Order]] = [
        (_event_date(order), item, order)
        for item, order in session.execute(stmt).all()
    ]
    _far_future = dt.datetime.max.replace(tzinfo=_UTC)  # позиции без даты — в конец
    returns.sort(key=lambda t: t[0] or _far_future)

    # 3. Гашение очереди по артикулу.
    rows: list[LossRow] = []
    matched = 0
    for event, item, order in returns:
        # Полных дней с даты возврата/отмены (целое — устойчиво на границе 30).
        days = (now - event).days if event is not None else None
        article = item.sku_title or item.product_title
        key = _match_key(item.sku_title, item.product_title, item.sku_id)

        if key is not None and pool.get(key, 0) > 0:
            pool[key] -= 1
            result, returned = RESULT_ACCEPTED, True
            matched += 1
        elif days is not None and days <= threshold_days:  # ≤ N дней → ещё в пути
            result, returned = RESULT_IN_TRANSIT, False
        else:                                              # > N дней → утеряно
            result, returned = RESULT_LOST, False

        rows.append(
            LossRow(
                order_id=order.uzum_id,
                event_date=event,
                sku_id=item.sku_id,
                article=article,
                barcode=(barcode_by_key.get(key) if key else None) or NO_BARCODE,
                # Человеческое название из каталога; фолбэк — имя позиции, иначе заглушка.
                # НЕ берём sku_title, чтобы не дублировать колонку «SKU / Артикул».
                title=((name_by_key.get(key) if key else None) or item.product_title or NO_NAME),
                uzum_status=order.status,
                days_elapsed=days,
                result=result,
                returned=returned,
            )
        )

    rows.sort(key=lambda r: (_SEVERITY[r.result], -(r.days_elapsed or 0)))

    total_watched = len(rows)  # все RETURNED/CANCELED позиции (до фильтра)

    # Фильтр по типу отчёта: только НЕвозвращённые, разделённые по сроку давности.
    if report_type == "transit":
        rows = [r for r in rows if r.result == RESULT_IN_TRANSIT]
    elif report_type == "lost":
        rows = [r for r in rows if r.result == RESULT_LOST]

    analysis = ReturnsAnalysis(
        rows=rows,
        total_returns=total_watched,
        total_received=total_received,
        matched=matched,
        unmatched=total_watched - matched,
    )

    return analysis


@dataclass
class ReturnsDebug:
    """Снимок состояния данных для диагностики невозвратов."""

    watched_items: int            # позиций в статусах RETURNED/CANCELED
    linked_watched_items: int     # из них уже со штрихкодом
    linked_items_total: int       # всего позиций со штрихкодом
    barcodes_total: int           # записей в таблице barcodes
    referenced_numbers: int       # уникальных invoiceNumber, на которые ссылаются заказы
    present_numbers: int          # из них реально есть в таблице invoices
    missing_numbers: int          # из них отсутствуют (API не отдал)
    missing_sample: list[int]     # примеры отсутствующих номеров
    invoices: list[tuple[int, int, int]]  # (uzum_id, number, кол-во штрихкодов)


def debug_returns(session: Session, telegram_id: int) -> ReturnsDebug:
    """Офлайн-диагностика (в рамках юзера): где рвётся цепочка заказ→накладная→ШК."""
    watched_items = session.execute(
        select(func.count())
        .select_from(OrderItem)
        .join(Order, OrderItem.order_pk == Order.id)
        .where(Order.telegram_id == telegram_id, Order.status.in_(WATCHED_STATUSES))
    ).scalar_one()

    linked_watched = session.execute(
        select(func.count())
        .select_from(OrderItem)
        .join(Order, OrderItem.order_pk == Order.id)
        .where(
            Order.telegram_id == telegram_id,
            Order.status.in_(WATCHED_STATUSES),
            OrderItem.barcode.is_not(None),
        )
    ).scalar_one()

    linked_total = session.execute(
        select(func.count())
        .select_from(OrderItem)
        .where(OrderItem.telegram_id == telegram_id, OrderItem.barcode.is_not(None))
    ).scalar_one()

    barcodes_total = session.execute(
        select(func.count()).select_from(Barcode).where(Barcode.telegram_id == telegram_id)
    ).scalar_one()

    # Какие номера накладных упомянуты в заказах и сколько из них есть в БД.
    referenced = {
        int(n)
        for (n,) in session.execute(
            select(Order.invoice_number)
            .where(Order.telegram_id == telegram_id, Order.invoice_number.is_not(None))
            .distinct()
        ).all()
    }
    present = {
        int(n)
        for (n,) in session.execute(
            select(Invoice.number)
            .where(Invoice.telegram_id == telegram_id, Invoice.number.is_not(None))
            .distinct()
        ).all()
    }
    missing = referenced - present

    # Накладные в БД + число штрихкодов в каждой.
    inv_rows = session.execute(
        select(Invoice.uzum_id, Invoice.number, func.count(Barcode.id))
        .outerjoin(Barcode, Barcode.invoice_pk == Invoice.id)
        .where(Invoice.telegram_id == telegram_id)
        .group_by(Invoice.id)
        .order_by(func.count(Barcode.id).desc())
    ).all()

    return ReturnsDebug(
        watched_items=watched_items,
        linked_watched_items=linked_watched,
        linked_items_total=linked_total,
        barcodes_total=barcodes_total,
        referenced_numbers=len(referenced),
        present_numbers=len(referenced & present),
        missing_numbers=len(missing),
        missing_sample=sorted(missing)[:10],
        invoices=[(r[0], r[1], r[2]) for r in inv_rows],
    )


def summarize(rows: list[LossRow]) -> dict[str, int]:
    """Свести вердикты в счётчики {result: count}."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.result] = counts.get(r.result, 0) + 1
    return counts


__all__ = [
    "LossRow",
    "ReturnsAnalysis",
    "ReturnsDebug",
    "check_missing_goods",
    "debug_returns",
    "summarize",
    "RESULT_ACCEPTED",
    "RESULT_IN_TRANSIT",
    "RESULT_LOST",
    "NO_BARCODE",
    "NO_NAME",
    "WATCHED_STATUSES",
    "DEFAULT_THRESHOLD_DAYS",
]
