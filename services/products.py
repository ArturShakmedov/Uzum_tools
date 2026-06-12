"""Синхронизация карточек товаров магазина из Uzum API → таблица user_products."""

from __future__ import annotations

from api.client import UzumClient
from api.endpoints import UzumAPI
from database.connection import session_scope
from database.repository import (
    get_active_shop,
    get_user_product,
    save_user_products,
    update_fbs_stocks,
)
from services.uzum_sync import SyncError
from utils.logger import get_logger

log = get_logger(__name__)


def sku_root(code: str | None) -> str | None:
    """Корень артикула: всё до последнего дефиса. «ТЕМНБОР-L» → «ТЕМНБОР»."""
    if not code:
        return None
    code = code.strip()
    return code.rsplit("-", 1)[0] if "-" in code else code


def sku_suffix(code: str | None) -> str | None:
    """Суффикс артикула (размер/цвет): хвост после последнего дефиса. «…-L» → «L»."""
    if not code or "-" not in code:
        return None
    return code.rsplit("-", 1)[1].strip() or None


def sync_products(telegram_id: int) -> int:
    """Выкачать товары активного магазина и сохранить в user_products.

    Возвращает число товаров. Бросает SyncError, если магазин не выбран.
    Синхронно (в боте — через asyncio.to_thread).
    """
    with session_scope() as session:
        shop = get_active_shop(session, telegram_id)
        token = shop.uzum_token if shop else None
        shop_id = shop.uzum_shop_id if shop else None
    if not token or shop_id is None:
        raise SyncError("Не выбран магазин. Отправьте /start и подключите магазин.")

    products: list[dict] = []
    with UzumClient(token=token) as client:
        api = UzumAPI(client)
        # v3 /v3/fbs/sku/stocks — авторитетные оперативные остатки FBS (поле amount).
        # v2 отключён Uzum с 15.06.2026. Берём карту {skuId: amount} один раз и
        # обогащаем ею карточки; quantityFbs из каталога — лишь фолбэк.
        fbs_stocks = api.stocks.fbs_stock_map([shop_id])
        for card, sku in api.products.iter_skus(shop_id):
            sku_id = sku.get("skuId")
            if sku_id is None:
                continue
            article = sku.get("article") or sku.get("sellerItemCode") or sku.get("skuFullTitle")
            # Корень для группировки модификаций; фолбэк — по продукту/SKU (свой «корень»).
            root = sku_root(article) or (
                f"pid{card.get('productId')}" if card.get("productId") else f"sku{sku_id}"
            )
            products.append({
                "sku_id": sku_id,
                "product_id": card.get("productId"),
                "title": card.get("title") or sku.get("productTitle"),
                "sku_title": sku.get("skuTitle") or sku.get("skuFullTitle"),
                "current_price": sku.get("price"),
                "fbo_stock": sku.get("quantityActive"),   # склад Uzum (FBO, из каталога)
                # FBS: приоритет v3 amount, иначе снимок quantityFbs из каталога.
                "fbs_stock": fbs_stocks.get(int(sku_id), sku.get("quantityFbs")),
                "category_name": card.get("category"),
                "article": article,
                "sku_root": root,
                # Базовый URL превью (previewImage). Полный URL строится при рендере.
                "image_url": sku.get("previewImage"),
                # Штрихкод — нужен для POST-обновления остатка FBS (обязателен).
                "barcode": sku.get("barcode"),
                "raw": None,
            })

    # DEBUG image_url: видим, что реально пришло от Uzum в previewImage и сколько
    # карточек получили картинку (диагностика «фото показываются текстом»).
    with_img = sum(1 for p in products if p.get("image_url"))
    sample = next((p["image_url"] for p in products if p.get("image_url")), None)
    log.info(
        "sync_products %s: товаров=%d, с previewImage=%d, пример image_url=%r",
        telegram_id, len(products), with_img, sample,
    )

    with session_scope() as session:
        save_user_products(session, telegram_id, shop_id, products)
    log.info("Товаров синхронизировано для %s: %d", telegram_id, len(products))
    return len(products)


def update_fbs_stock_remote(telegram_id: int, sku_id: int, amount: int) -> int:
    """Обновить остаток FBS одного SKU в Uzum (POST /v2/fbs/sku/stocks) и локально.

    Шаги: читаем токен активного магазина + штрихкод SKU → POST в Uzum →
    при УСПЕХЕ обновляем user_products.fbs_stock. Синхронно (в боте — to_thread).

    Бросает SyncError (нет магазина / нет штрихкода) или UzumAPIError (отказ Uzum)
    — хендлер их ловит и показывает пользователю. Возвращает сохранённый остаток.
    """
    with session_scope() as session:
        shop = get_active_shop(session, telegram_id)
        token = shop.uzum_token if shop else None
        p = get_user_product(session, telegram_id, sku_id)
        barcode = p.barcode if p else None
        sku_title = p.sku_title if p else None
        product_title = p.title if p else None
    if not token:
        raise SyncError("Не выбран магазин. Отправьте /start и подключите магазин.")
    if p is None:
        raise SyncError("Товар не найден в вашем каталоге.")
    if not barcode:
        raise SyncError(
            "У SKU нет штрихкода — обновите каталог (зайдите в «📦 Мои товары»)."
        )

    with UzumClient(token=token) as client:
        UzumAPI(client).stocks.update_fbs_stock(
            sku_id, amount, barcode=barcode,
            sku_title=sku_title, product_title=product_title,
        )

    # Uzum принял запись → синхронизируем локальную БД, чтобы юзер сразу увидел.
    with session_scope() as session:
        update_fbs_stocks(session, telegram_id, {sku_id: amount})
    log.info("FBS-остаток обновлён: tg=%s sku=%s → %s", telegram_id, sku_id, amount)
    return amount


__all__ = ["sync_products", "update_fbs_stock_remote", "sku_root", "sku_suffix"]
