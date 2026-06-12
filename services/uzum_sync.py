"""Сервис синхронизации Uzum → SQLite для одного пользователя бота.

Логика прежнего `main.py --all-time`, но:
  • параметризована telegram_id (данные изолированы по юзеру);
  • токен Uzum достаётся из таблицы user_shops;
  • стадии шлют человекочитаемый прогресс через колбэк on_progress.

Архитектура синка — ДВЕ ФАЗЫ (исправление блокировки event loop, C-1):

  1) fetch_everything_from_uzum — сетевой сбор (httpx), БЕЗ записи в БД. Долгая
     фаза (минуты I/O). Идёт под per-user asyncio.Lock: разные пользователи
     качают из Uzum ПАРАЛЛЕЛЬНО, не блокируя друг друга; дубль-синк одного юзера
     сериализуется.
  2) persist_to_db — запись собранного в БД. Короткая фаза (~1–3 с) под коротким
     DB_WRITE_SEMAPHORE(1): на SQLite-деве спасает от «database is locked», на
     PostgreSQL (MVCC) — дешёвая страховка. Сеть под этим локом НЕ держится.

Раньше весь синк (включая сеть) держал ОДИН глобальный Semaphore(1) — один юзер
блокировал всех остальных на минуты. Теперь сеть ограничена лишь потолком RAM
(FETCH_SEMAPHORE) и параллельна, а под write-локом — только короткая запись.

Фазы синхронны (sync SQLAlchemy + httpx) и запускаются в воркер-потоках через
asyncio.to_thread из async-оркестратора run_full_sync, поэтому event loop бота
не блокируется.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from api.client import UzumAPIError, UzumClient
from api.endpoints import UzumAPI
from database.connection import session_scope
from database.repository import (
    SyncReport,
    get_active_shop,
    link_order_item_barcodes,
    save_invoice_with_barcodes,
    save_orders,
    save_returns,
    save_sku_catalog,
    sync_fbs_orders,
    sync_shipping_acts,
    touch_active_shop_sync,
    update_fbs_stocks,
)
from utils.logger import get_logger

log = get_logger(__name__)

Progress = Callable[[str], None]

# Полный набор статусов заказа (без status /v2/fbs/orders отдаёт дефолт CREATED).
ORDER_STATUSES = [
    "CREATED", "PACKING", "PENDING_DELIVERY", "DELIVERING", "DELIVERED",
    "ACCEPTED_AT_DP", "DELIVERED_TO_CUSTOMER_DELIVERY_POINT", "COMPLETED",
    "CANCELED", "PENDING_CANCELLATION", "RETURNED",
]
ALL_INVOICE_STATUSES = ["CREATED", "ACCEPTANCE_IN_PROGRESS", "ACCEPTED", "CANCELLED"]

# Тянем историю с 2024-01-01, фильтр по дате — на клиенте (серверный ненадёжен).
HISTORY_FLOOR_MS = int(dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc).timestamp() * 1000)
ALL_TIME_MAX_PAGES = 500
ORDERS_PAGE = 50
INVOICE_PAGE = 20


# Per-user блокировки СЕТЕВОЙ фазы: разные пользователи синкаются параллельно,
# повторный синк одного юзера ждёт завершения предыдущего (защита от дублей/гонок).
_user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

# Глобальный потолок одновременных СЕТЕВЫХ синков (защита от OOM). Каждый синк в
# полёте держит ~15–25 МБ (SyncBundle). Ограничивая до 10, фиксируем пик RAM на
# ~200 МБ даже если синк одновременно запустят 100+ пользователей. Ставится в
# run_full_sync вокруг to_thread(fetch_everything_from_uzum) — сама fetch-функция
# синхронна (httpx-sync в воркер-потоке), поэтому `async with` внутри неё невозможен.
FETCH_SEMAPHORE = asyncio.Semaphore(10)

# КОРОТКИЙ write-семафор только вокруг ФАЗЫ ЗАПИСИ persist_to_db (не сети!).
# Назначение: на SQLite-деве (single-writer) исключает «database is locked»; на
# PostgreSQL (MVCC) строго не обязателен, но дёшев как страховка — держится лишь
# ~1–3 с записи, а не минуты сетевого I/O. Сеть остаётся параллельной (см.
# FETCH_SEMAPHORE / _user_locks). Если нужна максимальная параллельность записи на
# Postgres — этот семафор можно убрать.
DB_WRITE_SEMAPHORE = asyncio.Semaphore(1)


class SyncError(RuntimeError):
    """Ошибка синхронизации, понятная для показа пользователю."""


@dataclass
class SyncBundle:
    """Сырьё, выкачанное из Uzum в фазе 1, для записи в БД в фазе 2.

    Не содержит ORM-объектов — только dict'ы из API, поэтому безопасно
    передаётся между потоками и не держит открытую сессию/соединение с БД.
    """

    uzum_shop_id: int
    shop_name: str | None
    orders: list[dict]
    catalog_pairs: list[tuple[dict, dict]]   # (карточка продукта, SKU)
    returns: list[dict]
    invoices: list[tuple[dict, list[dict]]]  # (накладная, позиции со штрихкодами)
    fbs_stocks: dict[int, int | None]        # {skuId: amount} — оперативные остатки FBS (v3)


def fetch_shops(token: str) -> list[dict] | None:
    """Получить список магазинов по токену через GET /v1/shops.

    Возвращает [{"id": shopId, "name": shopTitle}, …] при валидном токене
    (может быть пустым), либо None — если токен невалиден / API ответил ошибкой.
    Синхронно (вызывается в боте через to_thread).
    """
    try:
        with UzumClient(token=token) as client:
            shops = UzumAPI(client).shops.list()
    except Exception as exc:  # noqa: BLE001
        log.warning("Получение магазинов по токену не удалось: %s", exc)
        return None
    return [{"id": s["id"], "name": s.get("name")} for s in shops if "id" in s]


# --------------------------------------------------------------------------- #
#  Стадии сбора (API-only, без сессии записи)
# --------------------------------------------------------------------------- #
def _collect_orders(
    api: UzumAPI, shop_ids: list[int], since_ms: int, *, max_pages: int
) -> dict[int, dict]:
    """Собрать заказы по всем статусам с клиентским отсечением по dateCreated."""
    collected: dict[int, dict] = {}
    for status in ORDER_STATUSES:
        for page in range(max_pages):
            batch = api.orders.list_page(shop_ids, status=status, page=page, size=ORDERS_PAGE)
            if not batch:
                break
            older_seen = False
            for o in batch:
                created = o.get("dateCreated")
                if created is not None and int(created) < since_ms:
                    older_seen = True
                    continue
                if "id" in o:
                    collected[o["id"]] = o
            if older_seen or len(batch) < ORDERS_PAGE:
                break
    return collected


# --------------------------------------------------------------------------- #
#  Фаза 1: сетевой сбор (без записи в БД)
# --------------------------------------------------------------------------- #
def fetch_everything_from_uzum(
    telegram_id: int, on_progress: Progress | None = None
) -> SyncBundle:
    """Фаза 1 (сеть): выкачать заказы, каталог, возвраты и накладные из Uzum.

    НЕ пишет в БД и не держит блокировку SQLite — поэтому выполняется параллельно
    у разных пользователей (per-user lock в run_full_sync), но число ОДНОВРЕМЕННЫХ
    вызовов ограничено глобальным FETCH_SEMAPHORE (потолок RAM). Единственное
    обращение к БД здесь — быстрый READ токена активного магазина.

    Функция синхронна (httpx-sync) и исполняется в воркер-потоке через to_thread,
    поэтому семафор-потолок ставится снаружи (в run_full_sync), а не здесь.

    Бросает SyncError при отсутствии токена/магазина или ошибке Uzum API.
    """
    progress: Progress = on_progress or (lambda _msg: None)

    with session_scope() as session:
        shop = get_active_shop(session, telegram_id)
        token = shop.uzum_token if shop else None
        uzum_shop_id = shop.uzum_shop_id if shop else None
        shop_name = shop.shop_name if shop else None
    if not token or uzum_shop_id is None:
        raise SyncError("Не выбран магазин. Отправьте /start и подключите магазин.")

    shop_ids = [uzum_shop_id]  # синкаем ТОЛЬКО активный магазин
    try:
        with UzumClient(token=token) as client:
            api = UzumAPI(client)
            progress(f"🏪 Магазин «{shop_name or uzum_shop_id}». Скачиваю заказы за всё время…")
            orders = list(
                _collect_orders(
                    api, shop_ids, HISTORY_FLOOR_MS, max_pages=ALL_TIME_MAX_PAGES
                ).values()
            )
            progress(f"📦 Заказов получено: {len(orders)}. Скачиваю каталог товаров…")

            catalog_pairs: list[tuple[dict, dict]] = []
            for shop_id in shop_ids:
                catalog_pairs.extend(api.products.iter_skus(shop_id))
            progress(f"📚 Каталог: SKU={len(catalog_pairs)}. Скачиваю остатки FBS (v3)…")

            # v3 /v3/fbs/sku/stocks — авторитетные оперативные остатки FBS (поле
            # amount). v2 отключён Uzum с 15.06.2026. Запрос идёт под FETCH_SEMAPHORE
            # (вся фаза в to_thread под семафором — см. run_full_sync).
            fbs_stocks = api.stocks.fbs_stock_map(shop_ids)
            progress(f"📦 Остатки FBS (v3): {len(fbs_stocks)} SKU. Скачиваю возвраты…")

            returns = list(api.returns.iter_all())
            units = sum(
                (it.get("amount") or 0)
                for r in returns for it in (r.get("returnItems") or [])
            )
            progress(f"↩️ Возвратов: {len(returns)} (единиц: {units}). Скачиваю накладные…")

            invoices: list[tuple[dict, list[dict]]] = []
            for inv in api.invoices.iter_all(ALL_INVOICE_STATUSES):
                items = api.invoices.barcode_items(inv["id"])
                invoices.append((inv, items))
                if len(invoices) % 5 == 0:
                    progress(f"🧾 Скачано накладных: {len(invoices)}…")
            progress(f"🧾 Накладных скачано: {len(invoices)}")
    except UzumAPIError as exc:
        raise SyncError(f"Ошибка Uzum API: {exc}") from exc

    return SyncBundle(
        uzum_shop_id=uzum_shop_id,
        shop_name=shop_name,
        orders=orders,
        catalog_pairs=catalog_pairs,
        returns=returns,
        invoices=invoices,
        fbs_stocks=fbs_stocks,
    )


# --------------------------------------------------------------------------- #
#  Фаза 2: запись в БД (single-writer)
# --------------------------------------------------------------------------- #
def persist_to_db(
    telegram_id: int, bundle: SyncBundle, on_progress: Progress | None = None
) -> SyncReport:
    """Фаза 2 (запись): сохранить собранное в БД.

    Сетевых вызовов здесь нет — фаза короткая (~1–3 с). Вызывается под коротким
    DB_WRITE_SEMAPHORE (см. run_full_sync): на SQLite-деве — против «database is
    locked», на PostgreSQL — дешёвая страховка поверх MVCC.
    """
    progress: Progress = on_progress or (lambda _msg: None)
    report = SyncReport()

    with session_scope() as session:
        save_orders(session, telegram_id, bundle.orders, report)
        # Мост FBS-логистики: те же сырые заказы → fbs_orders (таймер дедлайнов
        # /fbs). Статусы нормализуются картой UZUM_TO_FBS_STATUS («В поставке»
        # = PENDING_DELIVERY/DELIVERING → DELIVERY/SHIPPING) — попадают в таймер.
        n_fbs = sync_fbs_orders(session, telegram_id, bundle.orders)
    log.info("FBS-мост: fbs_orders upsert=%d (юзер %s)", n_fbs, telegram_id)
    with session_scope() as session:
        save_sku_catalog(
            session, telegram_id, bundle.catalog_pairs, bundle.uzum_shop_id, report
        )
    with session_scope() as session:
        save_returns(session, telegram_id, bundle.returns, report)
    with session_scope() as session:
        for inv, items in bundle.invoices:
            save_invoice_with_barcodes(session, telegram_id, inv, items, report)
        # Проставить barcode в order_items по совпадению (order_uzum_id, sku_id).
        link_order_item_barcodes(session, telegram_id)
        # Мост актов: накладные FBS и есть акты приёма-передачи (раздел /fbs).
        sync_shipping_acts(session, telegram_id, [inv for inv, _items in bundle.invoices])

    # Освежить оперативные остатки FBS (v3 amount) у уже синканных товаров.
    if bundle.fbs_stocks:
        with session_scope() as session:
            n = update_fbs_stocks(session, telegram_id, bundle.fbs_stocks)
        progress(f"📦 Обновлено остатков FBS: {n}")

    # Финансы НЕ синкаем здесь — у них свой быстрый путь refresh_finance
    # (кнопка «💰 Выплаты и баланс»), чтобы не утяжелять общий синк.

    # Успешный синк — отмечаем время активного магазина (кэш отчёта на 30 минут).
    with session_scope() as session:
        touch_active_shop_sync(session, telegram_id)

    progress("💾 Данные сохранены.")
    return report


# --------------------------------------------------------------------------- #
#  Оркестратор для бота: сеть (параллельно, под потолком RAM) → запись (MVCC)
# --------------------------------------------------------------------------- #
async def run_full_sync(telegram_id: int, on_progress: Progress | None = None) -> SyncReport:
    """Полный синк пользователя в две фазы, не блокируя event loop.

    Фаза 1 (fetch_everything_from_uzum) — СЕТЬ: под per-user lock (разные юзеры
    качают из Uzum параллельно) И под глобальным FETCH_SEMAPHORE (потолок RAM).
    Лок БД здесь НЕ держится — минуты I/O не блокируют чужую запись.
    Фаза 2 (persist_to_db) — ЗАПИСЬ: под коротким DB_WRITE_SEMAPHORE (~1–3 с).
    Обе фазы выполняются в воркер-потоках.

    Бросает SyncError при отсутствии токена/магазина или ошибке API.
    """
    # --- Фаза 1: сетевой сбор (параллельно у разных юзеров, без лока БД) ---
    async with _user_locks[telegram_id]:
        # FETCH_SEMAPHORE ограничивает число ОДНОВРЕМЕННЫХ сетевых синков на весь
        # процесс — жёсткий потолок пиковой RAM (защита от OOM-killer на сервере).
        async with FETCH_SEMAPHORE:
            bundle = await asyncio.to_thread(
                fetch_everything_from_uzum, telegram_id, on_progress
            )
    # --- Фаза 2: запись в БД под коротким write-семафором (НЕ держит сеть) ---
    async with DB_WRITE_SEMAPHORE:
        report = await asyncio.to_thread(persist_to_db, telegram_id, bundle, on_progress)
    return report


__all__ = [
    "run_full_sync",
    "fetch_everything_from_uzum",
    "persist_to_db",
    "fetch_shops",
    "SyncBundle",
    "FETCH_SEMAPHORE",
    "DB_WRITE_SEMAPHORE",
    "SyncError",
    "ORDER_STATUSES",
    "HISTORY_FLOOR_MS",
]
