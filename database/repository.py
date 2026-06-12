"""Слой персистентности: маппинг dict → ORM + идемпотентный upsert.

Разделение ответственности:
  • _parse_dt / _synthetic_id  — утилиты нормализации;
  • map_*                      — чистые функции dict → набор полей модели;
  • _upsert                    — общий upsert по бизнес-ключу uzum_id;
  • save_*                     — высокоуровневые операции, возвращающие статистику.

Upsert защищает от IntegrityError: запись с существующим uzum_id обновляется
(и ей переустанавливается synced_at), а не вставляется повторно.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.orm import Session

from database.models import (
    Barcode,
    FBSOrder,
    Invoice,
    Order,
    OrderItem,
    FinanceSnapshot,
    PaymentLog,
    ShippingAct,
    Return,
    ReturnItem,
    ShopManager,
    SkuBarcode,
    SupportTicket,
    SystemSettings,
    User,
    UserProduct,
    UserRole,
    UserShop,
    UzumCategory,
)
from utils.logger import get_logger

log = get_logger(__name__)

_UTC = dt.timezone.utc


# --------------------------------------------------------------------------- #
#  Утилиты нормализации
# --------------------------------------------------------------------------- #
def _parse_dt(value: Any) -> dt.datetime | None:
    """Привести значение к timezone-aware datetime (UTC).

    Uzum отдаёт даты неоднородно:
      • int/числовая строка — Unix-epoch (мс, иногда сек);
      • ISO-строка ('2024-09-26T12:00:00', с 'Z' или смещением);
      • человекочитаемая строка 'yyyy-MM-dd HH:mm:ss'.
    Непарсимое значение → None (с предупреждением), пайплайн не падает.
    """
    if value is None or value == "":
        return None

    # --- числовой epoch ---
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and value.strip().lstrip("-").isdigit()
    ):
        num = int(value)
        # > 10^12 ≈ мс (после 2001 г.); иначе секунды
        seconds = num / 1000 if abs(num) >= 1_000_000_000_000 else num
        try:
            return dt.datetime.fromtimestamp(seconds, tz=_UTC)
        except (OverflowError, OSError, ValueError):
            log.warning("Не удалось разобрать epoch-дату: %r", value)
            return None

    # --- строковые форматы ---
    text = str(value).strip()
    iso = text.replace("Z", "+00:00")
    for candidate in (iso, text):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=_UTC)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=_UTC)
        except ValueError:
            continue

    log.warning("Не удалось разобрать дату: %r", value)
    return None


def _synthetic_id(*parts: Any) -> int:
    """Детерминированный положительный 60-битный id из набора полей.

    Нужен для сущностей без собственного id на стороне Uzum (штрихкоды),
    чтобы upsert оставался идемпотентным между повторными прогонами.
    """
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(digest[:15], 16)


# --------------------------------------------------------------------------- #
#  Мапперы dict → поля модели
# --------------------------------------------------------------------------- #
def map_order(dto: dict[str, Any]) -> dict[str, Any]:
    return {
        "shop_id": dto.get("shopId"),
        "status": dto.get("status"),
        "scheme": dto.get("scheme"),
        "price": dto.get("price"),
        "invoice_number": dto.get("invoiceNumber"),
        "date_created": _parse_dt(dto.get("dateCreated")),
        "accept_until": _parse_dt(dto.get("acceptUntil")),
        "deliver_until": _parse_dt(dto.get("deliverUntil")),
        "return_date": _parse_dt(dto.get("returnDate")),
        "date_cancelled": _parse_dt(dto.get("dateCancelled")),
        "raw_payload": dto,
    }


def map_order_item(item: dict[str, Any], order_pk: int, order_uzum_id: int) -> dict[str, Any]:
    return {
        "order_pk": order_pk,
        "order_uzum_id": item.get("orderId") or order_uzum_id,
        "sku_id": item.get("skuId"),
        "sku_title": item.get("skuTitle"),
        "product_id": item.get("productId"),
        "product_title": item.get("productTitle"),
        "status": item.get("status"),
        "amount": item.get("amount"),
        "seller_price": item.get("sellerPrice"),
        "raw_payload": item,
    }


def map_invoice(dto: dict[str, Any]) -> dict[str, Any]:
    status = dto.get("status") or {}
    stock = dto.get("stock") or {}
    ettn = dto.get("ettn") or {}
    return {
        "number": dto.get("number"),
        # status — это ColorizedTextContainer: {text, color, value}
        "status": status.get("value") if isinstance(status, dict) else status,
        "full_price": dto.get("fullPrice"),
        "accepted_price": dto.get("acceptedPrice"),
        "number_orders": dto.get("numberOrders"),
        "number_accepted_orders": dto.get("numberAcceptedOrders"),
        "stock_id": stock.get("id") if isinstance(stock, dict) else None,
        "stock_title": stock.get("title") if isinstance(stock, dict) else None,
        "ettn_id": ettn.get("ettnId") if isinstance(ettn, dict) else None,
        "date_created": _parse_dt(dto.get("dateCreated")),
        "raw_payload": dto,
    }


def map_barcode(item: dict[str, Any], invoice_pk: int, invoice_uzum_id: int) -> dict[str, Any]:
    return {
        "invoice_pk": invoice_pk,
        "order_uzum_id": item.get("orderId"),
        "barcode": str(item.get("barcode")),
        "sku_id": item.get("skuId"),
        "sku_title": item.get("skuTitle"),
        "title": item.get("title"),
        "amount": item.get("amount"),
        "price": item.get("price"),
        "status": item.get("status"),
        "raw_payload": item,
    }


def map_sku_barcode(sku: dict[str, Any], card: dict[str, Any], shop_id: int) -> dict[str, Any]:
    barcode = sku.get("barcode")
    return {
        "shop_id": shop_id,
        "sku_title": sku.get("skuTitle"),
        "sku_full_title": sku.get("skuFullTitle"),
        "product_title": sku.get("productTitle") or card.get("title"),
        "article": sku.get("article"),
        "seller_item_code": sku.get("sellerItemCode"),
        "barcode": str(barcode) if barcode is not None else None,
        "raw_payload": sku,
    }


def map_return(dto: dict[str, Any]) -> dict[str, Any]:
    return {
        "shop_id": dto.get("shopId"),
        "status": dto.get("status"),
        "type": dto.get("type"),
        "external_number": dto.get("externalNumber"),
        "total_amount": dto.get("totalAmount"),
        "date_created": _parse_dt(dto.get("dateCreated")),
        "completed_date": _parse_dt(dto.get("completedDate")),
        "raw_payload": dto,
    }


def map_return_item(item: dict[str, Any], return_pk: int, return_uzum_id: int) -> dict[str, Any]:
    return {
        "return_pk": return_pk,
        "return_uzum_id": return_uzum_id,
        "sku_id": item.get("skuId"),
        "sku_title": item.get("skuTitle"),
        "product_title": item.get("productTitle"),
        "amount": item.get("amount"),
        "packed_amount": item.get("packedAmount"),
        "purchase_price": item.get("purchasePrice"),
        "raw_payload": item,
    }


# --------------------------------------------------------------------------- #
#  Статистика
# --------------------------------------------------------------------------- #
@dataclass
class Tally:
    """Счётчик результатов upsert по одной сущности."""

    name: str
    created: int = 0
    updated: int = 0
    failed: int = 0

    def record(self, was_created: bool) -> None:
        if was_created:
            self.created += 1
        else:
            self.updated += 1

    @property
    def total(self) -> int:
        return self.created + self.updated


@dataclass
class SyncReport:
    """Сводка по всему прогону пайплайна."""

    tallies: dict[str, Tally] = field(default_factory=dict)

    def tally(self, name: str) -> Tally:
        return self.tallies.setdefault(name, Tally(name))


# --------------------------------------------------------------------------- #
#  Общий upsert
# --------------------------------------------------------------------------- #
def _upsert(
    session: Session, model: type, telegram_id: int, uzum_id: int, values: dict[str, Any]
) -> tuple[Any, bool]:
    """Вставить/обновить запись по бизнес-ключу (telegram_id, uzum_id).

    Изоляция данных: запись всегда привязана к telegram_id владельца.
    Возвращает (объект, created). При обновлении переустанавливает synced_at.
    """
    obj = session.execute(
        select(model).filter_by(telegram_id=telegram_id, uzum_id=uzum_id)
    ).scalar_one_or_none()

    if obj is None:
        obj = model(telegram_id=telegram_id, uzum_id=uzum_id, **values)
        session.add(obj)
        return obj, True

    for key, val in values.items():
        setattr(obj, key, val)
    obj.synced_at = dt.datetime.now(_UTC)
    return obj, False


# --------------------------------------------------------------------------- #
#  Высокоуровневые операции сохранения
# --------------------------------------------------------------------------- #
def save_orders(
    session: Session, telegram_id: int, orders: list[dict[str, Any]], report: SyncReport
) -> None:
    """Сохранить заказы вместе с позициями (orderItems) для конкретного юзера."""
    t_order = report.tally("orders")
    t_item = report.tally("order_items")

    for dto in orders:
        order_id = dto.get("id")
        if order_id is None:
            t_order.failed += 1
            continue
        try:
            with session.begin_nested():  # savepoint: сбой одного заказа не валит остальные
                order, created = _upsert(session, Order, telegram_id, order_id, map_order(dto))
                session.flush()  # получить order.id для FK позиций
                for item in dto.get("orderItems") or []:
                    item_id = item.get("id") or _synthetic_id(order_id, item.get("skuId"))
                    _, ic = _upsert(
                        session,
                        OrderItem,
                        telegram_id,
                        item_id,
                        map_order_item(item, order.id, order_id),
                    )
                    t_item.record(ic)
            t_order.record(created)
        except Exception as exc:  # noqa: BLE001
            t_order.failed += 1
            log.error("Заказ %s не сохранён: %s", order_id, exc)


def save_invoice_with_barcodes(
    session: Session,
    telegram_id: int,
    invoice_dto: dict[str, Any],
    barcode_items: list[dict[str, Any]],
    report: SyncReport,
) -> None:
    """Сохранить накладную и штрихкоды из её состава (upsert) для юзера.

    barcode_items — плоский список позиций со штрихкодами
    (InvoicesAPI.barcode_items): каждая несёт orderId / skuId / barcode.
    """
    t_inv = report.tally("invoices")
    t_bc = report.tally("barcodes")

    invoice_id = invoice_dto.get("id")
    if invoice_id is None:
        t_inv.failed += 1
        return
    try:
        with session.begin_nested():
            invoice, created = _upsert(
                session, Invoice, telegram_id, invoice_id, map_invoice(invoice_dto)
            )
            session.flush()
            for item in barcode_items:
                if not item.get("barcode"):
                    continue
                bc_id = _synthetic_id(
                    invoice_id, item.get("orderId"), item.get("skuId"), item.get("barcode")
                )
                _, bc_created = _upsert(
                    session,
                    Barcode,
                    telegram_id,
                    bc_id,
                    map_barcode(item, invoice.id, invoice_id),
                )
                t_bc.record(bc_created)
        t_inv.record(created)
    except Exception as exc:  # noqa: BLE001
        t_inv.failed += 1
        log.error("Накладная %s не сохранена: %s", invoice_id, exc)


def link_order_item_barcodes(session: Session, telegram_id: int) -> int:
    """Проставить barcode в order_items по совпадению (order_uzum_id, sku_id).

    Строго в рамках одного telegram_id. Идемпотентно. Возвращает число
    затронутых позиций заказов.
    """
    result = session.execute(
        text(
            """
            UPDATE order_items
            SET barcode = (
                SELECT b.barcode FROM barcodes b
                WHERE b.order_uzum_id = order_items.order_uzum_id
                  AND b.sku_id = order_items.sku_id
                  AND b.telegram_id = order_items.telegram_id
                LIMIT 1
            )
            WHERE order_items.telegram_id = :tg
              AND order_items.sku_id IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM barcodes b
                WHERE b.order_uzum_id = order_items.order_uzum_id
                  AND b.sku_id = order_items.sku_id
                  AND b.telegram_id = order_items.telegram_id
            )
            """
        ),
        {"tg": telegram_id},
    )
    return result.rowcount or 0


def save_returns(
    session: Session, telegram_id: int, returns: list[dict[str, Any]], report: SyncReport
) -> None:
    """Сохранить возвраты вместе с позициями (returnItems) для юзера."""
    t_ret = report.tally("returns")
    t_item = report.tally("return_items")

    for dto in returns:
        return_id = dto.get("id")
        if return_id is None:
            t_ret.failed += 1
            continue
        try:
            with session.begin_nested():
                ret, created = _upsert(session, Return, telegram_id, return_id, map_return(dto))
                session.flush()
                for item in dto.get("returnItems") or []:
                    item_id = item.get("id") or _synthetic_id(return_id, item.get("skuId"))
                    _, ic = _upsert(
                        session,
                        ReturnItem,
                        telegram_id,
                        item_id,
                        map_return_item(item, ret.id, return_id),
                    )
                    t_item.record(ic)
            t_ret.record(created)
        except Exception as exc:  # noqa: BLE001
            t_ret.failed += 1
            log.error("Возврат %s не сохранён: %s", return_id, exc)


def save_sku_catalog(
    session: Session,
    telegram_id: int,
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    shop_id: int,
    report: SyncReport,
) -> None:
    """Сохранить SKU каталога со штрихкодами (upsert по skuId) для юзера.

    pairs — список (карточка продукта, SKU) из ProductsAPI.iter_skus.
    """
    t = report.tally("sku_catalog")
    for card, sku in pairs:
        sku_id = sku.get("skuId")
        if sku_id is None:
            t.failed += 1
            continue
        try:
            with session.begin_nested():
                _, created = _upsert(
                    session, SkuBarcode, telegram_id, sku_id,
                    map_sku_barcode(sku, card, shop_id),
                )
            t.record(created)
        except Exception as exc:  # noqa: BLE001
            t.failed += 1
            log.error("SKU %s каталога не сохранён: %s", sku_id, exc)


# --------------------------------------------------------------------------- #
#  Подключённые магазины пользователя (мультимагазинность)
# --------------------------------------------------------------------------- #
def purge_user_data(session: Session, telegram_id: int) -> None:
    """Удалить все доменные данные юзера (purge перед ре-синком нового магазина)."""
    for model in (
        OrderItem, Order, Barcode, Invoice, ReturnItem, Return, SkuBarcode,
        FinanceSnapshot, UserProduct,
    ):
        session.execute(delete(model).where(model.telegram_id == telegram_id))


def save_finance_snapshot(
    session: Session,
    telegram_id: int,
    *,
    shop_id: int | None,
    available: int,
    pending: int,
    commissions: int,
    payments: list[dict] | None,
    has_data: bool,
) -> None:
    """Сохранить/обновить снимок финансов активного магазина (один на юзера)."""
    snap = session.get(FinanceSnapshot, telegram_id)
    if snap is None:
        snap = FinanceSnapshot(telegram_id=telegram_id)
        session.add(snap)
    snap.shop_id = shop_id
    snap.available = available
    snap.pending = pending
    snap.commissions = commissions
    snap.payments = payments
    snap.has_data = has_data
    snap.finance_synced_at = dt.datetime.now(_UTC)  # отметка успешного обновления


def get_finance_snapshot(session: Session, telegram_id: int) -> FinanceSnapshot | None:
    """Снимок финансов пользователя (или None)."""
    return session.get(FinanceSnapshot, telegram_id)


# --------------------------------------------------------------------------- #
#  Справочник комиссий (калькулятор)
# --------------------------------------------------------------------------- #
def _stem(word: str) -> str:
    """Лёгкий стемминг: срезаем окончание, чтобы 'блузка' нашла 'блузки',
    'платье' → 'платья' и т.п. Слова короче 5 символов не трогаем.
    """
    return word[:-1] if len(word) >= 5 else word


def _like_escape(s: str) -> str:
    """Экранировать спецсимволы оператора LIKE (`%`, `_`) и сам экранирующий слэш.

    Защита от LIKE-инъекции: без этого ввод «%» в поиске матчил бы все строки,
    а «_» — любой одиночный символ. Применять только вместе с модификатором
    .like(..., escape="\\"). Порядок важен: слэш экранируем первым.
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_categories(session: Session, query: str, limit: int = 5) -> list[UzumCategory]:
    """Поиск категорий по ключевым словам (регистронезависимо, с учётом
    словоформ).

    Запрос чистится (.strip().lower()), бьётся на слова; каждое слово ищется по
    корню через LOWER(search_text) LIKE LOWER('%корень%'). Несколько слов
    объединяются по И (все должны встретиться) — устойчиво к множественному
    числу и фразам вроде «женская одежда».
    """
    cleaned = query.strip().lower()
    if not cleaned:
        return []

    stmt = select(UzumCategory)
    for word in cleaned.split():
        pattern = f"%{_like_escape(_stem(word))}%"
        stmt = stmt.where(
            func.lower(UzumCategory.search_text).like(func.lower(pattern), escape="\\")
        )
    return list(session.execute(stmt.limit(limit)).scalars())


def get_category(session: Session, category_id: int) -> UzumCategory | None:
    """Категория справочника по id (или None)."""
    return session.get(UzumCategory, category_id)


# --------------------------------------------------------------------------- #
#  Товары пользователя (карточки)
# --------------------------------------------------------------------------- #
def _resolve_category_id(session: Session, name: str | None, cache: dict) -> int | None:
    """Best-effort: id категории uzum_categories по имени (категории нет в API)."""
    if not name:
        return None
    key = name.strip().lower()
    if key in cache:
        return cache[key]
    row = session.execute(
        select(UzumCategory.id)
        .where(UzumCategory.search_text.like(f"%{_like_escape(key)}%", escape="\\"))
        .limit(1)
    ).scalar_one_or_none()
    cache[key] = row
    return row


def save_user_products(
    session: Session, telegram_id: int, shop_id: int, products: list[dict]
) -> int:
    """Upsert товаров пользователя (по skuId). Сохраняет покупную цену, если она
    уже была задана вручную (не затираем при ресинке). Возвращает число товаров.
    """
    cat_cache: dict = {}
    for p in products:
        sku_id = p.get("sku_id")
        if sku_id is None:
            continue
        existing = session.execute(
            select(UserProduct).filter_by(telegram_id=telegram_id, uzum_id=sku_id)
        ).scalar_one_or_none()
        category_id = _resolve_category_id(session, p.get("category_name"), cat_cache)
        values = {
            "shop_id": shop_id,
            "product_id": p.get("product_id"),
            "title": p.get("title"),
            "sku_title": p.get("sku_title"),
            "current_price": p.get("current_price"),
            "fbo_stock": p.get("fbo_stock"),
            "fbs_stock": p.get("fbs_stock"),
            "category_id": category_id,
            "article": p.get("article"),
            "sku_root": p.get("sku_root"),
            "image_url": p.get("image_url"),
            "barcode": p.get("barcode"),
            "raw_payload": p.get("raw"),
        }
        if existing is None:
            session.add(UserProduct(
                telegram_id=telegram_id, uzum_id=sku_id,
                purchase_price=p.get("purchase_price"), **values,
            ))
        else:
            for k, v in values.items():
                setattr(existing, k, v)
            existing.synced_at = dt.datetime.now(_UTC)
            # purchase_price НЕ трогаем — это ручной ввод пользователя.
    # DEBUG image_url: сколько строк реально получили непустой image_url в БД.
    n_img = sum(1 for p in products if p.get("image_url"))
    log.info(
        "save_user_products %s: записано товаров=%d, с image_url=%d",
        telegram_id, len(products), n_img,
    )
    return len(products)


def count_user_products(session: Session, telegram_id: int) -> int:
    return session.execute(
        select(func.count()).select_from(UserProduct).where(UserProduct.telegram_id == telegram_id)
    ).scalar_one()


def needs_product_resync(session: Session, telegram_id: int) -> bool:
    """True, если нужен ресинк каталога для добивки полей из новой схемы.

    Условия (любое):
      • есть товары без sku_root (старая схема без группировки), ИЛИ
      • НИ У ОДНОГО товара нет image_url (каталог синкан до колонки картинок), ИЛИ
      • НИ У ОДНОГО товара нет barcode (каталог синкан до колонки штрихкодов —
        без них нельзя обновлять остаток FBS).
    Проверяем «нет ни одного с полем», а не «есть хоть один без» — иначе магазины,
    где у части SKU поля пусты, ресинкались бы вечно. Один ресинк добивает поля.
    """
    no_root = session.execute(
        select(func.count()).select_from(UserProduct).where(
            UserProduct.telegram_id == telegram_id, UserProduct.sku_root.is_(None)
        )
    ).scalar_one() > 0
    if no_root:
        return True

    total = session.execute(
        select(func.count()).select_from(UserProduct).where(
            UserProduct.telegram_id == telegram_id
        )
    ).scalar_one()
    if total == 0:
        return False

    def _none_have(column) -> bool:
        return session.execute(
            select(func.count()).select_from(UserProduct).where(
                UserProduct.telegram_id == telegram_id, column.is_not(None)
            )
        ).scalar_one() == 0

    # Нет ни картинок, ни штрихкодов ни у одного товара → нужна добивка.
    return _none_have(UserProduct.image_url) or _none_have(UserProduct.barcode)


def count_product_groups(session: Session, telegram_id: int) -> int:
    """Число групп товаров (по корню артикула) — для пагинации."""
    sub = (
        select(UserProduct.sku_root)
        .where(UserProduct.telegram_id == telegram_id)
        .group_by(UserProduct.sku_root)
        .subquery()
    )
    return session.execute(select(func.count()).select_from(sub)).scalar_one()


def list_product_groups(
    session: Session, telegram_id: int, *, offset: int = 0, limit: int = 5
) -> list[tuple[int, str | None, str | None]]:
    """Группы товаров по корню артикула: (repr_sku_id, title, sku_root).

    GROUP BY sku_root; representative SKU — min(uzum_id), название — min(title).
    """
    stmt = (
        select(
            func.min(UserProduct.uzum_id).label("repr_sku"),
            func.min(UserProduct.title).label("title"),
            UserProduct.sku_root,
        )
        .where(UserProduct.telegram_id == telegram_id)
        .group_by(UserProduct.sku_root)
        .order_by(func.min(UserProduct.title).asc())
        .offset(offset).limit(limit)
    )
    return [(r.repr_sku, r.title, r.sku_root) for r in session.execute(stmt)]


def list_products_by_root(
    session: Session, telegram_id: int, sku_root: str | None
) -> list[UserProduct]:
    """Все SKU (размеры/цвета) одной группы — по корню артикула."""
    return list(session.execute(
        select(UserProduct)
        .where(UserProduct.telegram_id == telegram_id, UserProduct.sku_root == sku_root)
        .order_by(UserProduct.article.asc(), UserProduct.id.asc())
    ).scalars())


# --------------------------------------------------------------------------- #
#  Аналитика продаж по модели (sku_root) + данные для ABC-классификации
# --------------------------------------------------------------------------- #
# Реальность данных Uzum: у order_items пусты sku_id / product_title / seller_price,
# заполнен только sku_title — это ПОЛНЫЙ артикул («DUALLOK-SARAFAN-СИНИЙ-S/M»),
# который 1:1 совпадает с user_products.article. Поэтому связь продажи с моделью —
# через JOIN order_items.sku_title = user_products.article (даёт сразу sku_root и
# purchase_price). Выручку несёт только orders.price (на весь заказ), поэтому
# на позицию делим её на число позиций в заказе.
_SOLD_STATUSES = (
    "COMPLETED", "DELIVERED", "DELIVERED_TO_CUSTOMER_DELIVERY_POINT", "ACCEPTED_AT_DP",
)
_RETURNED_STATUSES = ("RETURNED",)


def _sql_list(values: tuple[str, ...]) -> str:
    """Безопасный IN-список из захардкоженных констант статусов."""
    return ", ".join(f"'{v}'" for v in values)


# Число позиций в заказе считаем ОДИН раз в CTE (а не коррелированным подзапросом
# на каждую строку — это было O(n²)). JOIN lines подмешивает счётчик l.n к строке.
_LINES_CTE = """
    WITH lines AS (
        SELECT order_uzum_id, telegram_id, COUNT(*) AS n
        FROM order_items
        GROUP BY order_uzum_id, telegram_id
    )
"""
# Выручка позиции = цена заказа / число позиций. NULLIF(...,0) — защита от деления
# на ноль при возможном рассинхроне данных (в SQLite это вернёт NULL, не упадёт).
_LINE_REVENUE = "o.price * 1.0 / NULLIF(l.n, 0)"

_MODEL_STATS_SQL = text(f"""
    {_LINES_CTE}
    SELECT
        COALESCE(SUM(CASE WHEN o.status IN ({_sql_list(_SOLD_STATUSES)})
                          THEN oi.amount ELSE 0 END), 0)                       AS units_sold,
        COALESCE(SUM(CASE WHEN o.status IN ({_sql_list(_SOLD_STATUSES)})
                          THEN {_LINE_REVENUE} ELSE 0 END), 0)                 AS revenue,
        COALESCE(SUM(CASE WHEN o.status IN ({_sql_list(_RETURNED_STATUSES)})
                          THEN oi.amount ELSE 0 END), 0)                       AS returns_qty,
        COALESCE(SUM(CASE WHEN o.status IN ({_sql_list(_RETURNED_STATUSES)})
                          THEN {_LINE_REVENUE} ELSE 0 END), 0)                 AS returns_sum,
        COALESCE(SUM(CASE WHEN o.status IN ({_sql_list(_SOLD_STATUSES)})
                          THEN COALESCE(p.purchase_price, 0) * oi.amount ELSE 0 END), 0) AS cogs,
        COALESCE(SUM(CASE WHEN o.status IN ({_sql_list(_SOLD_STATUSES)})
                          AND p.purchase_price IS NULL THEN oi.amount ELSE 0 END), 0)    AS units_no_cost
    FROM order_items oi
    JOIN orders        o ON o.uzum_id = oi.order_uzum_id AND o.telegram_id = oi.telegram_id
    JOIN user_products p ON p.article = oi.sku_title     AND p.telegram_id = oi.telegram_id
    JOIN lines         l ON l.order_uzum_id = oi.order_uzum_id AND l.telegram_id = oi.telegram_id
    WHERE p.telegram_id = :tg
      AND p.sku_root    = :root
      AND o.date_created >= :cutoff
""")

_SHOP_PROFIT_SQL = text(f"""
    {_LINES_CTE}
    SELECT
        p.sku_root AS root,
        COALESCE(SUM(CASE WHEN o.status IN ({_sql_list(_SOLD_STATUSES)})
                          THEN {_LINE_REVENUE} - COALESCE(p.purchase_price, 0) * oi.amount
                          ELSE 0 END), 0) AS net_profit
    FROM order_items oi
    JOIN orders        o ON o.uzum_id = oi.order_uzum_id AND o.telegram_id = oi.telegram_id
    JOIN user_products p ON p.article = oi.sku_title     AND p.telegram_id = oi.telegram_id
    JOIN lines         l ON l.order_uzum_id = oi.order_uzum_id AND l.telegram_id = oi.telegram_id
    WHERE p.telegram_id = :tg
      AND o.date_created >= :cutoff
    GROUP BY p.sku_root
""")


def get_model_sales_stats(
    session: Session, telegram_id: int, sku_root: str, days: int = 30
) -> dict[str, Any]:
    """Сводка продаж модели (всех её размеров) за `days` дней.

    Возвращает выручку, проданные штуки, возвраты (шт/сумма), себестоимость
    проданного и чистую прибыль. `cost_complete=False` → у части проданных SKU
    не задана закупка, прибыль приблизительная.
    """
    cutoff = dt.datetime.now(_UTC) - dt.timedelta(days=days)
    row = session.execute(
        _MODEL_STATS_SQL, {"tg": telegram_id, "root": sku_root, "cutoff": cutoff}
    ).one()
    revenue = int(round(row.revenue))
    cogs = int(round(row.cogs))
    net_profit = revenue - cogs
    return {
        "sku_root": sku_root,
        "days": days,
        "units_sold": int(row.units_sold),
        "revenue": revenue,
        "returns_qty": int(row.returns_qty),
        "returns_sum": int(round(row.returns_sum)),
        "cogs": cogs,
        "net_profit": net_profit,
        "units_no_cost": int(row.units_no_cost),
        "cost_complete": int(row.units_no_cost) == 0,
        "margin_pct": (net_profit / revenue * 100) if revenue > 0 else None,
    }


def get_shop_profit_by_root(
    session: Session, telegram_id: int, days: int = 30
) -> dict[str, int]:
    """Чистая прибыль по КАЖДОЙ модели магазина за период (для ABC-анализа)."""
    cutoff = dt.datetime.now(_UTC) - dt.timedelta(days=days)
    rows = session.execute(_SHOP_PROFIT_SQL, {"tg": telegram_id, "cutoff": cutoff})
    return {r.root: int(round(r.net_profit)) for r in rows if r.root is not None}


def list_user_products(
    session: Session, telegram_id: int, *, offset: int = 0, limit: int = 5
) -> list[UserProduct]:
    return list(session.execute(
        select(UserProduct)
        .where(UserProduct.telegram_id == telegram_id)
        .order_by(UserProduct.title.asc(), UserProduct.id.asc())
        .offset(offset).limit(limit)
    ).scalars())


def get_user_product(session: Session, telegram_id: int, sku_id: int) -> UserProduct | None:
    return session.execute(
        select(UserProduct).filter_by(telegram_id=telegram_id, uzum_id=sku_id)
    ).scalar_one_or_none()


def search_user_products(
    session: Session, telegram_id: int, query: str, *, shop_id: int | None = None, limit: int = 10
) -> list[tuple[int, str | None, str | None]]:
    """Поиск по товарам, сгруппированный по корню артикула.

    Возвращает группы (repr_sku_id, title, sku_root) — не дублирует размеры.
    Совпадение по названию, характеристике ИЛИ артикулу (включая корень).
    Фильтрация в Python: SQLite LOWER()/LIKE регистронезависимы только для ASCII,
    а кириллица в названиях хранится в исходном регистре (str.lower() её понижает).
    """
    cleaned = query.strip().lower()
    if not cleaned:
        return []
    stmt = select(UserProduct).where(UserProduct.telegram_id == telegram_id)
    if shop_id is not None:
        stmt = stmt.where(UserProduct.shop_id == shop_id)
    stmt = stmt.order_by(UserProduct.title.asc(), UserProduct.id.asc())

    groups: dict[str, tuple[int, str | None, str | None]] = {}
    for p in session.execute(stmt).scalars():
        haystack = f"{p.title or ''} {p.sku_title or ''} {p.article or ''}".lower()
        if cleaned not in haystack:
            continue
        key = p.sku_root or f"sku{p.uzum_id}"
        if key not in groups:                       # один представитель на группу
            groups[key] = (p.uzum_id, p.title, p.sku_root)
            if len(groups) >= limit:
                break
    return list(groups.values())


def find_local_competitors(
    session: Session,
    telegram_id: int,
    title: str,
    exclude_sku_id: int,
    limit: int = 3,
) -> list[UserProduct]:
    """Похожие товары ДРУГИХ пользователей бота по ключевым словам названия.

    Конкуренты = товары, загруженные не текущим юзером (`telegram_id !=`), с
    исключением самого SKU (`uzum_id != exclude_sku_id`). Сопоставление по словам
    названия через ILIKE (на PostgreSQL — регистронезависимо для кириллицы; на
    SQLite-деве ILIKE менее точен, но фолбэк-mock компенсирует нехватку).
    Дедуп по sku_root, чтобы не вернуть три размера одной модели.
    """
    keywords = [w for w in re.findall(r"\w+", (title or "").lower()) if len(w) >= 4][:2]
    if not keywords:
        return []
    stmt = (
        select(UserProduct)
        .where(
            UserProduct.telegram_id != telegram_id,        # товары ДРУГИХ юзеров
            UserProduct.uzum_id != exclude_sku_id,         # не сам товар
            or_(*[UserProduct.title.ilike(f"%{kw}%") for kw in keywords]),
        )
        .limit(limit * 4)                                  # с запасом под дедуп
    )
    seen: set = set()
    out: list[UserProduct] = []
    for p in session.execute(stmt).scalars():
        key = p.sku_root or p.uzum_id
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= limit:
            break
    return out


def set_product_purchase_price(
    session: Session, telegram_id: int, sku_id: int, purchase_price: int
) -> None:
    prod = get_user_product(session, telegram_id, sku_id)
    if prod is not None:
        prod.purchase_price = purchase_price


def update_fbs_stocks(
    session: Session, telegram_id: int, stock_map: dict[int, int | None]
) -> int:
    """Обновить оперативные остатки FBS (v3 `amount`) у существующих user_products.

    Обновляются ТОЛЬКО уже синканные товары (по uzum_id = skuId); отсутствующие
    SKU пропускаются (их создаёт sync_products). Возвращает число затронутых строк.
    """
    affected = 0
    for sku_id, amount in stock_map.items():
        affected += session.execute(
            update(UserProduct)
            .where(
                UserProduct.telegram_id == telegram_id,
                UserProduct.uzum_id == sku_id,
            )
            .values(fbs_stock=amount)
        ).rowcount or 0
    return affected


def list_user_shops(session: Session, telegram_id: int) -> list[UserShop]:
    """Все подключённые магазины пользователя (активный — первым)."""
    return list(
        session.execute(
            select(UserShop)
            .where(UserShop.telegram_id == telegram_id)
            .order_by(UserShop.is_active.desc(), UserShop.id.asc())
        ).scalars()
    )


def get_active_shop(session: Session, telegram_id: int) -> UserShop | None:
    """Активный магазин пользователя (или None)."""
    return session.execute(
        select(UserShop).where(
            UserShop.telegram_id == telegram_id, UserShop.is_active.is_(True)
        )
    ).scalar_one_or_none()


def list_all_active_shops(session: Session) -> list[UserShop]:
    """Активные магазины ВСЕХ пользователей (для фонового воркера Live-уведомлений).

    По одному активному магазину на telegram_id (инвариант is_active). Токен
    приходит уже расшифрованным (EncryptedToken).
    """
    return list(
        session.execute(
            select(UserShop).where(UserShop.is_active.is_(True))
        ).scalars()
    )


def _deactivate_all(session: Session, telegram_id: int) -> None:
    session.execute(
        update(UserShop)
        .where(UserShop.telegram_id == telegram_id, UserShop.is_active.is_(True))
        .values(is_active=False)
    )


def connect_shop(
    session: Session,
    telegram_id: int,
    uzum_shop_id: int,
    *,
    shop_name: str | None,
    uzum_token: str,
    username: str | None = None,
) -> UserShop:
    """Подключить (или обновить) магазин и сделать его активным.

    Старый активный магазин деактивируется; локальные данные юзера чистятся —
    БД будет наполнена заново синком активного магазина (purge + ре-синк).
    """
    _deactivate_all(session, telegram_id)
    shop = session.execute(
        select(UserShop).where(
            UserShop.telegram_id == telegram_id,
            UserShop.uzum_shop_id == uzum_shop_id,
        )
    ).scalar_one_or_none()
    if shop is None:
        shop = UserShop(telegram_id=telegram_id, uzum_shop_id=uzum_shop_id)
        session.add(shop)
    shop.shop_name = shop_name
    shop.uzum_token = uzum_token
    if username is not None:
        shop.username = username
    shop.is_active = True
    shop.last_sync_at = None  # данные ещё не синканы под этот магазин
    purge_user_data(session, telegram_id)
    return shop


def switch_active_shop(
    session: Session, telegram_id: int, uzum_shop_id: int
) -> UserShop | None:
    """Сделать активным уже подключённый магазин. None — если такого нет."""
    target = session.execute(
        select(UserShop).where(
            UserShop.telegram_id == telegram_id,
            UserShop.uzum_shop_id == uzum_shop_id,
        )
    ).scalar_one_or_none()
    if target is None:
        return None
    _deactivate_all(session, telegram_id)
    target.is_active = True
    target.last_sync_at = None       # форсируем ре-синк нового магазина
    purge_user_data(session, telegram_id)  # чистим данные прежнего магазина
    return target


def touch_active_shop_sync(session: Session, telegram_id: int) -> None:
    """Отметить время успешной синхронизации активного магазина (кэш 30 мин)."""
    shop = get_active_shop(session, telegram_id)
    if shop is not None:
        shop.last_sync_at = dt.datetime.now(_UTC)


def get_active_status(session: Session, telegram_id: int) -> tuple[bool, dt.datetime | None]:
    """(есть_активный_магазин, его_last_sync_at) — для проверки кэша отчёта."""
    shop = get_active_shop(session, telegram_id)
    return (shop is not None, shop.last_sync_at if shop else None)


def wipe_user(session: Session, telegram_id: int) -> None:
    """Полностью удалить пользователя: доменные данные + все его магазины/токены."""
    purge_user_data(session, telegram_id)
    session.execute(delete(UserShop).where(UserShop.telegram_id == telegram_id))


# --------------------------------------------------------------------------- #
#  Пользователи и подписки (Free / Premium)
# --------------------------------------------------------------------------- #
def get_user(session: Session, telegram_id: int) -> User | None:
    """Запись User по telegram_id (или None)."""
    return session.get(User, telegram_id)


def ensure_user(
    session: Session,
    telegram_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
) -> User:
    """Вернуть User (создав строку с тарифом 'free', если её ещё нет).

    Если переданы username/first_name (есть message.from_user) — освежаем профиль,
    чтобы менеджеры выводились по имени/юзернейму, а не по сырому ID.
    """
    user = session.get(User, telegram_id)
    if user is None:
        user = User(telegram_id=telegram_id, subscription_tier="free")
        session.add(user)
        # autoflush=False: без flush повторный ensure_user в ЭТОЙ ЖЕ сессии не
        # увидел бы pending-юзера через session.get и продублировал бы INSERT
        # (UNIQUE violation по PK на commit).
        session.flush()
    if username is not None:
        user.username = username
    if first_name is not None:
        user.first_name = first_name
    return user


def is_user_premium(user: User | None) -> bool:
    """True, если тариф 'premium' И срок ещё не истёк.

    Сравнение устойчиво к naive/aware datetime (SQLite отдаёт naive, Postgres —
    aware): naive нормализуем к UTC перед сравнением.
    """
    if user is None or user.subscription_tier != "premium":
        return False
    exp = user.subscription_expires_at
    if exp is None:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=_UTC)
    return exp > dt.datetime.now(_UTC)


def activate_premium(
    session: Session, telegram_id: int, *, days: int, plan_name: str = "Premium"
) -> User:
    """Выдать/продлить Premium на `days` дней (создаёт User при отсутствии).

    Если подписка ещё активна — продлеваем от текущего expires_at, иначе от now.
    plan_name — витринное название плана для «Моего кабинета».

    Строка User берётся под пессимистической блокировкой (SELECT ... FOR UPDATE):
    параллельные начисления (двойной SUCCESSFUL_PAYMENT, /grant_premium во время
    оплаты) выстраиваются в очередь на уровне СУБД и не теряют дни друг друга.
    На SQLite (дев) FOR UPDATE — no-op: запись там и так сериализована.
    """
    user = session.execute(
        select(User).where(User.telegram_id == telegram_id).with_for_update()
    ).scalar_one_or_none()
    if user is None:
        user = User(telegram_id=telegram_id, subscription_tier="free")
        session.add(user)
        session.flush()  # строка должна существовать до начисления (и быть нашей)
    now = dt.datetime.now(_UTC)
    base = now
    if is_user_premium(user) and user.subscription_expires_at is not None:
        exp = user.subscription_expires_at
        base = exp if exp.tzinfo else exp.replace(tzinfo=_UTC)
    user.subscription_tier = "premium"
    user.plan_name = plan_name
    user.subscription_expires_at = base + dt.timedelta(days=days)
    return user


def activate_welcome_trial(session: Session, telegram_id: int) -> bool:
    """Начислить 7 дней Welcome-триала при первом подключении магазина.

    Abuse-защита: если `subscription_expires_at` УЖЕ заполнено (юзер когда-то платил
    ИЛИ уже брал триал — даже истёкший), возвращаем False и ничего не меняем. Триал
    выдаётся один раз на аккаунт, а не на магазин — перепривязка не сбрасывает дату.
    Возвращает True, только если триал реально начислен.
    """
    ensure_user(session, telegram_id)  # строка существует (flush внутри)
    # FOR UPDATE: параллельная выдача триала (двойной тап по «выбрать магазин»)
    # сериализуется СУБД — второй поток увидит уже заполненный expires_at.
    user = session.execute(
        select(User).where(User.telegram_id == telegram_id).with_for_update()
    ).scalar_one()
    if user.subscription_expires_at is not None:
        return False
    user.subscription_tier = "premium"
    user.plan_name = "Premium (Триал)"
    user.subscription_expires_at = dt.datetime.now(_UTC) + dt.timedelta(days=7)
    return True


def subscription_days_left(user: User | None) -> int:
    """Сколько ПОЛНЫХ дней осталось до конца подписки (0, если истекла/нет)."""
    if user is None or user.subscription_expires_at is None:
        return 0
    exp = user.subscription_expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=_UTC)
    delta = exp - dt.datetime.now(_UTC)
    return max(0, delta.days)


# --------------------------------------------------------------------------- #
#  Менеджеры магазина (доступ по инвайту) + проверка прав на аналитику
# --------------------------------------------------------------------------- #
def add_shop_manager(
    session: Session, owner_telegram_id: int, manager_telegram_id: int,
    *, shop_id: int | None = None,
) -> tuple[ShopManager, bool]:
    """Дать менеджеру доступ к магазину владельца. Идемпотентно: (объект, created)."""
    existing = session.execute(
        select(ShopManager).where(
            ShopManager.owner_telegram_id == owner_telegram_id,
            ShopManager.manager_telegram_id == manager_telegram_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    sm = ShopManager(
        owner_telegram_id=owner_telegram_id,
        manager_telegram_id=manager_telegram_id,
        shop_id=shop_id,
    )
    session.add(sm)
    return sm, True


def list_shop_managers(session: Session, owner_telegram_id: int) -> list[ShopManager]:
    """Менеджеры владельца (по дате добавления)."""
    return list(session.execute(
        select(ShopManager)
        .where(ShopManager.owner_telegram_id == owner_telegram_id)
        .order_by(ShopManager.created_at.asc())
    ).scalars())


def get_detailed_managers(session: Session, owner_id: int) -> list[dict[str, Any]]:
    """Менеджеры владельца с профилем из users (LEFT JOIN по manager_telegram_id).

    Возвращает список dict: manager_telegram_id, created_at, plan_name, username,
    first_name. Поля профиля могут быть None (если юзер ещё не /start'нул) — UI
    подставит «ID: …».
    """
    rows = session.execute(
        select(
            ShopManager.manager_telegram_id,
            ShopManager.created_at,
            User.plan_name,
            User.username,
            User.first_name,
        )
        .outerjoin(User, User.telegram_id == ShopManager.manager_telegram_id)
        .where(ShopManager.owner_telegram_id == owner_id)
        .order_by(ShopManager.created_at.asc())
    ).all()
    return [
        {
            "manager_telegram_id": r.manager_telegram_id,
            "created_at": r.created_at,
            "plan_name": r.plan_name,
            "username": r.username,
            "first_name": r.first_name,
        }
        for r in rows
    ]


def remove_shop_manager(
    session: Session, owner_telegram_id: int, manager_telegram_id: int
) -> int:
    """Отозвать доступ менеджера. Возвращает число удалённых строк."""
    res = session.execute(
        delete(ShopManager).where(
            ShopManager.owner_telegram_id == owner_telegram_id,
            ShopManager.manager_telegram_id == manager_telegram_id,
        )
    )
    return res.rowcount or 0


def is_shop_manager(
    session: Session, manager_telegram_id: int, owner_telegram_id: int
) -> bool:
    """True, если manager — менеджер магазина owner."""
    return session.execute(
        select(func.count()).select_from(ShopManager).where(
            ShopManager.owner_telegram_id == owner_telegram_id,
            ShopManager.manager_telegram_id == manager_telegram_id,
        )
    ).scalar_one() > 0


def get_owner_for_manager(session: Session, manager_telegram_id: int) -> int | None:
    """Владелец, которого обслуживает менеджер (первый по дате), или None."""
    return session.execute(
        select(ShopManager.owner_telegram_id)
        .where(ShopManager.manager_telegram_id == manager_telegram_id)
        .order_by(ShopManager.created_at.asc())
        .limit(1)
    ).scalar_one_or_none()


def effective_data_owner(session: Session, telegram_id: int) -> int:
    """Чьи данные/аналитику видит юзер.

    • есть свой активный магазин → он сам (владелец);
    • иначе он менеджер → telegram_id владельца, которого обслуживает;
    • иначе → он сам (своих данных нет).
    """
    if get_active_shop(session, telegram_id) is not None:
        return telegram_id
    owner = get_owner_for_manager(session, telegram_id)
    return owner if owner is not None else telegram_id


def can_view_shop_analytics(
    session: Session, viewer_telegram_id: int, owner_telegram_id: int
) -> bool:
    """Может ли viewer смотреть аналитику магазина owner: сам владелец ИЛИ менеджер."""
    return viewer_telegram_id == owner_telegram_id or is_shop_manager(
        session, viewer_telegram_id, owner_telegram_id
    )


# --------------------------------------------------------------------------- #
#  Тикеты техподдержки (пользователь ↔ топик супергруппы)
# --------------------------------------------------------------------------- #
def get_support_ticket(session: Session, telegram_id: int) -> SupportTicket | None:
    """Тикет пользователя (прямой поиск: юзер → его топик)."""
    return session.get(SupportTicket, telegram_id)


def get_ticket_user_by_topic(session: Session, topic_id: int) -> int | None:
    """telegram_id пользователя по topic_id (обратный поиск: ответ админа → юзеру)."""
    return session.execute(
        select(SupportTicket.telegram_id).where(SupportTicket.topic_id == topic_id)
    ).scalar_one_or_none()


def create_support_ticket(
    session: Session, telegram_id: int, topic_id: int
) -> SupportTicket:
    """Создать связку юзер↔топик (идемпотентно: обновляет topic_id, если был)."""
    ticket = session.get(SupportTicket, telegram_id)
    if ticket is None:
        ticket = SupportTicket(telegram_id=telegram_id, topic_id=topic_id)
        session.add(ticket)
    else:
        ticket.topic_id = topic_id
    return ticket


# --------------------------------------------------------------------------- #
#  FBS-логистика: акты приёма-передачи + заказы под контролем дедлайна
# --------------------------------------------------------------------------- #
# Статусы «собран, но ещё НЕ сдан на пункт приёма» (вкладка «В поставке» ЛК Uzum;
# API отдаёт оба варианта). Дедлайн по ним продолжает тикать — товар нужно довезти.
ASSEMBLED_FBS_STATUSES: tuple[str, ...] = ("DELIVERY", "SHIPPING")
# Статусы FBS-заказа, по которым дедлайн ещё «тикает» (не сдан в ПВЗ/не отменён):
# NEW/PACKING — ждёт сборки; DELIVERY/SHIPPING — собран, ждёт передачи на приёмку.
_ACTIVE_FBS_STATUSES: tuple[str, ...] = ("NEW", "PACKING", *ASSEMBLED_FBS_STATUSES)


def list_shipping_acts(
    session: Session, telegram_id: int, *, limit: int = 5
) -> list[ShippingAct]:
    """Последние акты приёма-передачи юзера (свежие первыми)."""
    return list(session.execute(
        select(ShippingAct)
        .where(ShippingAct.telegram_id == telegram_id)
        .order_by(ShippingAct.created_at.desc(), ShippingAct.id.desc())
        .limit(limit)
    ).scalars())


def list_active_fbs_orders(session: Session, telegram_id: int) -> list[FBSOrder]:
    """Активные FBS-заказы юзера для таймера дедлайнов (старые первыми)."""
    return list(session.execute(
        select(FBSOrder)
        .where(
            FBSOrder.telegram_id == telegram_id,
            FBSOrder.status.in_(_ACTIVE_FBS_STATUSES),
        )
        .order_by(FBSOrder.order_created_at.asc())
    ).scalars())


# Карта сырых статусов Uzum API (/v2/fbs/orders, словарь ORDER_STATUSES синка) →
# внутренние статусы fbs_orders. «В поставке» в ЛК = PENDING_DELIVERY/DELIVERING
# на API (литералов DELIVERY/SHIPPING Uzum НЕ отдаёт — это наши внутренние коды).
# Неизвестный статус проходит как есть (UPPER) и в таймер не попадает, пока его
# не внесут в карту, — рассинхрон виден в БД, а не маскируется.
UZUM_TO_FBS_STATUS: dict[str, str] = {
    "CREATED": "NEW",                                  # ждёт сборки
    "PACKING": "PACKING",                              # сборка/упаковка
    "PENDING_DELIVERY": "DELIVERY",                    # «В поставке»: собран, сдать в ПВЗ
    "DELIVERING": "SHIPPING",                          # передан в логистику
    "DELIVERED": "SHIPPED",                            # терминальные — вне таймера
    "ACCEPTED_AT_DP": "SHIPPED",
    "DELIVERED_TO_CUSTOMER_DELIVERY_POINT": "SHIPPED",
    "COMPLETED": "SHIPPED",
    "CANCELED": "CANCELLED",
    "PENDING_CANCELLATION": "CANCELLED",
    "RETURNED": "RETURNED",
}


def sync_fbs_orders(
    session: Session, telegram_id: int, orders: list[dict[str, Any]]
) -> int:
    """Мост синка: сырые FBS-заказы Uzum API → fbs_orders (таймер дедлайнов).

    Upsert по (telegram_id, uzum_order_id): новый заказ вставляется, у известного
    обновляется статус (CREATED → PACKING → PENDING_DELIVERY → …). Статус
    нормализуется .upper() (защита от lowercase в ответах) и переводится картой
    UZUM_TO_FBS_STATUS. DBS/FBO-заказы пропускаются — дедлайн сборки только у FBS.
    Возвращает число затронутых строк.
    """
    ensure_user(session, telegram_id)  # FK fbs_orders.telegram_id → users
    affected = 0
    for dto in orders:
        if dto.get("scheme") not in (None, "FBS"):
            continue
        oid = dto.get("id")
        created = _parse_dt(dto.get("dateCreated"))
        if oid is None or created is None:
            continue
        raw_status = str(dto.get("status") or "").strip().upper()
        status = UZUM_TO_FBS_STATUS.get(raw_status, raw_status or "NEW")
        items = dto.get("orderItems") or []
        sku_title = (
            (items[0].get("skuTitle") or items[0].get("productTitle")) if items else None
        ) or f"Заказ {oid}"

        obj = session.execute(
            select(FBSOrder).filter_by(
                telegram_id=telegram_id, uzum_order_id=str(oid)
            )
        ).scalar_one_or_none()
        if obj is None:
            session.add(FBSOrder(
                telegram_id=telegram_id,
                uzum_order_id=str(oid),
                sku_title=sku_title[:256],
                order_created_at=created,
                status=status,
            ))
        else:
            obj.status = status
            obj.sku_title = sku_title[:256]
            obj.order_created_at = created
        affected += 1
    # autoflush=False: без flush повторный вызов моста / select в ЭТОЙ ЖЕ сессии
    # не увидит pending-вставок и продублирует INSERT (тот же класс бага, что
    # был в ensure_user — пойман регрессионным тестом).
    session.flush()
    return affected


def sync_shipping_acts(
    session: Session, telegram_id: int, invoice_dtos: list[dict[str, Any]]
) -> int:
    """Мост синка: FBS-накладные (/v1/fbs/invoice) → shipping_acts (раздел «Акты»).

    Накладная Uzum и есть акт приёма-передачи поставки. Upsert по
    (telegram_id, act_number). pdf_url не заполняем: печать акта в API — это
    авторизованный поток /v1/fbs/invoice/{id}/print, а не публичная ссылка.
    """
    ensure_user(session, telegram_id)
    affected = 0
    for dto in invoice_dtos:
        number = dto.get("number") or dto.get("id")
        if number is None:
            continue
        act_number = str(number)
        created = _parse_dt(dto.get("dateCreated"))
        total = dto.get("numberOrders") or 0
        act = session.execute(
            select(ShippingAct).filter_by(
                telegram_id=telegram_id, act_number=act_number
            )
        ).scalar_one_or_none()
        if act is None:
            act = ShippingAct(telegram_id=telegram_id, act_number=act_number)
            session.add(act)
        act.total_items = int(total)
        if created is not None:
            act.created_at = created
        affected += 1
    session.flush()  # autoflush=False: видимость pending-вставок (см. sync_fbs_orders)
    return affected


# --------------------------------------------------------------------------- #
#  Аудит платежей за Premium (PaymentLog)
# --------------------------------------------------------------------------- #
def create_payment_log(
    session: Session, telegram_id: int, payload: str, amount: int
) -> PaymentLog:
    """Записать факт выставления инвойса (status='created'). amount — в СУМАХ."""
    entry = PaymentLog(
        telegram_id=telegram_id, payload=payload, amount=amount, status="created"
    )
    session.add(entry)
    return entry


def has_recent_open_payment(
    session: Session, telegram_id: int, payload: str, *, within_minutes: int = 15
) -> bool:
    """True, если у юзера уже есть СВЕЖАЯ открытая 'created'-запись по payload.

    Дебаунс аудита: повторный клик по «купить» в течение окна не плодит
    фантомные created-записи (админ видел их как «зависшие оплаты»). Сравнение
    времени — в Python (naive SQLite ↔ aware Postgres, как в is_user_premium).
    """
    entry = session.execute(
        select(PaymentLog)
        .where(
            PaymentLog.telegram_id == telegram_id,
            PaymentLog.payload == payload,
            PaymentLog.status == "created",
        )
        .order_by(PaymentLog.created_at.desc(), PaymentLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if entry is None or entry.created_at is None:
        return False
    created = entry.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=_UTC)
    return created >= dt.datetime.now(_UTC) - dt.timedelta(minutes=within_minutes)


def complete_payment_log(
    session: Session,
    telegram_id: int,
    payload: str,
    *,
    charge_id: str | None = None,
    amount: int | None = None,
) -> bool:
    """Закрыть последнюю 'created'-запись юзера с этим payload → 'completed'.

    charge_id (telegram_payment_charge_id) уникален на платёж: flush ниже падает
    IntegrityError при повторной обработке того же платежа — вызывать ДО
    activate_premium, чтобы дубль не начислил дни. Если открытой 'created'-записи
    нет (легаси/потерянный инвойс) — создаём сразу 'completed' с amount (в сумах),
    чтобы charge_id всё равно попал под unique-констрейнт.

    True, если нашли и закрыли существующую запись; False — если создали новую.
    """
    entry = session.execute(
        select(PaymentLog)
        .where(
            PaymentLog.telegram_id == telegram_id,
            PaymentLog.payload == payload,
            PaymentLog.status == "created",
        )
        .order_by(PaymentLog.created_at.desc(), PaymentLog.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    found = entry is not None
    if entry is None:
        entry = PaymentLog(
            telegram_id=telegram_id, payload=payload, amount=amount or 0
        )
        session.add(entry)
    entry.status = "completed"
    if charge_id is not None:
        entry.telegram_payment_charge_id = charge_id
        session.flush()  # unique по charge_id: дубль платежа падает здесь
    return found


# --------------------------------------------------------------------------- #
#  Enterprise: настройки системы, кэш, RBAC, бан, глобальный дашборд
# --------------------------------------------------------------------------- #
def get_setting(session: Session, key: str, default: str | None = None) -> str | None:
    """Значение глобальной настройки из system_settings (или default)."""
    obj = session.get(SystemSettings, key)
    return obj.value if obj is not None else default


def set_setting(
    session: Session, key: str, value: str, *, description: str | None = None
) -> None:
    """Upsert глобальной настройки (updated_at обновится через onupdate)."""
    obj = session.get(SystemSettings, key)
    if obj is None:
        session.add(SystemSettings(key=key, value=value, description=description))
    else:
        obj.value = value
        if description is not None:
            obj.description = description


def load_maintenance_cache() -> str:
    """Прогреть config.SYSTEM_CACHE['maintenance_mode'] из БД при старте бота.

    Если записи нет — создаёт её со значением 'false'. Возвращает актуальное значение.
    """
    from config import SYSTEM_CACHE  # локальные импорты против циклов
    from database.connection import session_scope

    with session_scope() as session:
        value = get_setting(session, "maintenance_mode")
        if value is None:
            set_setting(
                session, "maintenance_mode", "false",
                description="Глобальный режим техобслуживания",
            )
            value = "false"
    SYSTEM_CACHE["maintenance_mode"] = value
    return value


def get_user_status(session: Session, telegram_id: int) -> dict[str, Any]:
    """Только is_banned и role одним select'ом (без ленивой загрузки магазинов).

    Юзера нет → фолбэк {is_banned: False, role: UserRole.USER}.
    """
    row = session.execute(
        select(User.is_banned, User.role).where(User.telegram_id == telegram_id)
    ).first()
    if row is None:
        return {"is_banned": False, "role": UserRole.USER}
    return {"is_banned": bool(row.is_banned), "role": row.role}


def set_user_banned(session: Session, telegram_id: int, banned: bool) -> None:
    """Забанить/разбанить юзера (создаёт строку User при отсутствии)."""
    user = ensure_user(session, telegram_id)
    user.is_banned = banned


def set_user_role(session: Session, telegram_id: int, role: UserRole) -> None:
    """Назначить роль юзеру (создаёт строку User при отсутствии)."""
    user = ensure_user(session, telegram_id)
    user.role = role


def get_dashboard_stats(session: Session) -> dict[str, Any]:
    """Агрегаты для /root: юзеры/Premium/магазины/тикеты + выручка по периодам."""
    users = session.scalar(select(func.count()).select_from(User)) or 0
    premium = len(list_premium_telegram_ids(session))
    shops = session.scalar(select(func.count()).select_from(UserShop)) or 0
    tickets = session.scalar(select(func.count()).select_from(SupportTicket)) or 0

    now = dt.datetime.now(_UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _revenue(since: dt.datetime | None) -> int:
        stmt = select(func.coalesce(func.sum(PaymentLog.amount), 0)).where(
            PaymentLog.status == "completed"
        )
        if since is not None:
            # Дата ЗАВЕРШЕНИЯ платежа (updated_at ставится при completed), а не
            # выставления инвойса: «висяк», оплаченный сегодня, попадает в сегодня.
            stmt = stmt.where(
                func.coalesce(PaymentLog.updated_at, PaymentLog.created_at) >= since
            )
        return int(session.scalar(stmt) or 0)

    return {
        "users": users,
        "premium": premium,
        "shops": shops,
        "tickets": tickets,
        "rev_today": _revenue(today),
        "rev_month": _revenue(month),
        "rev_all": _revenue(None),
    }


def list_payment_logs(session: Session, limit: int = 10) -> list[dict[str, Any]]:
    """Последние `limit` платежей с именем юзера (LEFT JOIN users)."""
    rows = session.execute(
        select(
            PaymentLog.telegram_id,
            PaymentLog.payload,
            PaymentLog.amount,
            PaymentLog.status,
            PaymentLog.created_at,
            PaymentLog.updated_at,
            User.username,
            User.first_name,
        )
        .outerjoin(User, User.telegram_id == PaymentLog.telegram_id)
        .order_by(PaymentLog.created_at.desc(), PaymentLog.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "telegram_id": r.telegram_id,
            "payload": r.payload,
            "amount": r.amount,
            "status": r.status,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "username": r.username,
            "first_name": r.first_name,
        }
        for r in rows
    ]


def list_premium_telegram_ids(session: Session) -> set[int]:
    """Множество telegram_id с АКТИВНЫМ Premium (фильтр срока — в Python, надёжно)."""
    rows = session.execute(
        select(User).where(User.subscription_tier == "premium")
    ).scalars()
    return {u.telegram_id for u in rows if is_user_premium(u)}


def get_admin_stats(session: Session) -> dict[str, int]:
    """Быстрые COUNT-метрики для админ-панели (PostgreSQL/SQLite).

    • users               — уникальных пользователей (по telegram_id магазинов);
    • active_shops        — активных магазинов (is_active=True);
    • products            — товаров/SKU в user_products;
    • users_with_purchase — пользователей, у кого хоть у одного SKU задана закупка;
    • premium_users       — пользователей с тарифом 'premium' (по полю subscription_tier).
    """
    def _c(stmt) -> int:
        return session.execute(stmt).scalar_one()

    return {
        "users": _c(select(func.count(func.distinct(UserShop.telegram_id)))),
        "active_shops": _c(
            select(func.count()).select_from(UserShop).where(UserShop.is_active.is_(True))
        ),
        "products": _c(select(func.count()).select_from(UserProduct)),
        "users_with_purchase": _c(
            select(func.count(func.distinct(UserProduct.telegram_id))).where(
                UserProduct.purchase_price.is_not(None)
            )
        ),
        "premium_users": _c(
            select(func.count()).select_from(User).where(
                User.subscription_tier == "premium"
            )
        ),
    }


def list_broadcast_recipients(session: Session) -> list[int]:
    """Уникальные telegram_id ДОСТИЖИМЫХ пользователей (есть активный магазин).

    Заблокировавшие бота помечаются is_active=False (deactivate_user_shops) и сюда
    больше не попадают — рассылки/уведомления их не дёргают.
    """
    return [
        r[0] for r in session.execute(
            select(func.distinct(UserShop.telegram_id)).where(UserShop.is_active.is_(True))
        )
    ]


def deactivate_user_shops(session: Session, telegram_id: int) -> None:
    """Снять is_active со всех магазинов юзера (заблокировал бота / отписался).

    Live-воркер (list_all_active_shops) и рассылки (list_broadcast_recipients)
    берут только is_active=True → перестают слать сообщения этому юзеру.
    """
    _deactivate_all(session, telegram_id)


__all__ = [
    "SyncReport",
    "Tally",
    "save_orders",
    "save_invoice_with_barcodes",
    "link_order_item_barcodes",
    "save_returns",
    "save_sku_catalog",
    "purge_user_data",
    "list_user_shops",
    "get_active_shop",
    "list_all_active_shops",
    "connect_shop",
    "switch_active_shop",
    "touch_active_shop_sync",
    "get_active_status",
    "wipe_user",
    "get_admin_stats",
    "list_broadcast_recipients",
    "deactivate_user_shops",
    "get_user",
    "ensure_user",
    "is_user_premium",
    "activate_premium",
    "activate_welcome_trial",
    "subscription_days_left",
    "list_premium_telegram_ids",
    "add_shop_manager",
    "list_shop_managers",
    "get_detailed_managers",
    "remove_shop_manager",
    "is_shop_manager",
    "get_owner_for_manager",
    "effective_data_owner",
    "can_view_shop_analytics",
    "get_support_ticket",
    "get_ticket_user_by_topic",
    "create_support_ticket",
    "list_shipping_acts",
    "list_active_fbs_orders",
    "ASSEMBLED_FBS_STATUSES",
    "UZUM_TO_FBS_STATUS",
    "sync_fbs_orders",
    "sync_shipping_acts",
    "create_payment_log",
    "has_recent_open_payment",
    "complete_payment_log",
    "list_payment_logs",
    "get_setting",
    "set_setting",
    "load_maintenance_cache",
    "get_user_status",
    "set_user_banned",
    "set_user_role",
    "get_dashboard_stats",
    "save_finance_snapshot",
    "get_finance_snapshot",
    "search_categories",
    "get_category",
    "save_user_products",
    "count_user_products",
    "list_user_products",
    "get_user_product",
    "search_user_products",
    "find_local_competitors",
    "set_product_purchase_price",
    "update_fbs_stocks",
    "needs_product_resync",
    "count_product_groups",
    "list_product_groups",
    "list_products_by_root",
    "get_model_sales_stats",
    "get_shop_profit_by_root",
    "map_order",
    "map_invoice",
    "map_barcode",
    "map_sku_barcode",
]
