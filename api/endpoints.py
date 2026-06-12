"""Высокоуровневые методы по доменам: заказы, накладные, возвраты, штрихкоды.

Каждый класс получает готовый :class:`UzumClient` и инкапсулирует конкретные
эндпоинты + пагинацию. Возвращаются «сырые» dict'ы API — маппинг в ORM-модели
выполняется на уровне пайплайна (main.py), чтобы не смешивать слои.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from api.client import UzumClient
from config import API, ENDPOINTS
from utils.logger import get_logger

log = get_logger(__name__)


def _paginate(
    fetch_page,  # callable(page:int) -> list[dict]
    *,
    size: int,
) -> Iterator[dict[str, Any]]:
    """Унифицированная постраничная выборка (page начинается с 0)."""
    page = 0
    while True:
        batch = fetch_page(page)
        if not batch:
            return
        yield from batch
        if len(batch) < size:
            return
        page += 1


class ShopsAPI:
    """Магазины продавца."""

    def __init__(self, client: UzumClient) -> None:
        self._c = client

    def list(self) -> list[dict[str, Any]]:
        """GET /v1/shops — список организаций/магазинов (id, name)."""
        return self._c.get(ENDPOINTS.shops) or []


class OrdersAPI:
    """FBS/DBS заказы."""

    def __init__(self, client: UzumClient) -> None:
        self._c = client

    def count(
        self,
        shop_ids: list[int] | None = None,
        *,
        status: str | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
    ) -> int:
        """GET /v2/fbs/orders/count — количество заказов по фильтру."""
        params = {
            "shopIds": shop_ids,
            "status": status,
            "dateFrom": date_from,
            "dateTo": date_to,
        }
        return int(self._c.get(ENDPOINTS.fbs_orders_count, params=params) or 0)

    def list_page(
        self,
        shop_ids: list[int],
        *,
        status: str | None = None,
        scheme: str | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        page: int = 0,
        size: int = API.page_size,
    ) -> list[dict[str, Any]]:
        """GET /v2/fbs/orders — одна страница заказов.

        Конверт: payload.orders[] (+ payload.totalAmount).
        Даты dateFrom/dateTo — unix-epoch (мс). size ≤ 50.
        """
        params = {
            "shopIds": shop_ids,
            "status": status,
            "scheme": scheme,
            "dateFrom": date_from,
            "dateTo": date_to,
            "page": page,
            "size": min(size, API.max_page_size),
        }
        payload = self._c.get(ENDPOINTS.fbs_orders, params=params) or {}
        return payload.get("orders", [])

    def iter_all(
        self,
        shop_ids: list[int],
        *,
        status: str | None = None,
        scheme: str | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        size: int = API.page_size,
    ) -> Iterator[dict[str, Any]]:
        """Итератор по всем заказам с автоматической пагинацией."""
        return _paginate(
            lambda p: self.list_page(
                shop_ids, status=status, scheme=scheme,
                date_from=date_from, date_to=date_to, page=p, size=size,
            ),
            size=size,
        )

    def detail(self, order_id: int) -> dict[str, Any]:
        """GET /v1/fbs/order/{orderId} — детали заказа (orderItems со статусами)."""
        path = ENDPOINTS.fbs_order_detail.format(order_id=order_id)
        return self._c.get(path) or {}

    def labels_pdf(self, order_id: int) -> bytes:
        """GET /v1/fbs/order/{orderId}/labels/print — PDF этикеток (штрихкоды)."""
        path = ENDPOINTS.fbs_order_labels_print.format(order_id=order_id)
        return self._c.get(path)

    def return_reasons(self) -> list[dict[str, Any]]:
        """GET /v1/fbs/order/return-reasons — справочник причин возврата."""
        return self._c.get(ENDPOINTS.fbs_order_return_reasons) or []


class InvoicesAPI:
    """FBS-накладные и их состав (источник штрихкодов)."""

    def __init__(self, client: UzumClient) -> None:
        self._c = client

    def list_page(
        self,
        statuses: list[str],
        *,
        page: int = 0,
        size: int = API.invoice_max_page_size,
    ) -> list[dict[str, Any]]:
        """GET /v1/fbs/invoice — страница накладных.

        statuses ∈ {CREATED, ACCEPTANCE_IN_PROGRESS, CANCELLED, ACCEPTED} (REQ).
        ВНИМАНИЕ: size ≤ 20 (иначе API отвечает 400 bad-request).
        """
        params = {
            "statuses": statuses,
            "page": page,
            "size": min(size, API.invoice_max_page_size),
        }
        return self._c.get(ENDPOINTS.fbs_invoice, params=params) or []

    def iter_all(
        self, statuses: list[str], *, size: int = API.invoice_max_page_size
    ) -> Iterator[dict[str, Any]]:
        size = min(size, API.invoice_max_page_size)  # держим size и пагинацию согласованными
        return _paginate(
            lambda p: self.list_page(statuses, page=p, size=size), size=size
        )

    def detail(self, invoice_id: int) -> dict[str, Any]:
        """GET /v1/fbs/invoice/{invoiceId} — детали накладной."""
        path = ENDPOINTS.fbs_invoice_detail.format(invoice_id=invoice_id)
        return self._c.get(path) or {}

    def orders_with_items(self, invoice_id: int) -> list[dict[str, Any]]:
        """GET /v1/fbs/invoice/{invoiceId}/orders.

        Возвращает список FbsOrdersWithItemsDto. Внутри items[] лежит ШТРИХКОД
        (поле `barcode`) на уровне SKU — основной источник для модели Barcode.
        """
        path = ENDPOINTS.fbs_invoice_orders.format(invoice_id=invoice_id)
        return self._c.get(path) or []

    def barcode_items(self, invoice_id: int) -> list[dict[str, Any]]:
        """Плоский список позиций со штрихкодами из накладной.

        Разворачивает FbsOrdersWithItemsDto[] → items[] и оставляет только
        записи с непустым barcode. Каждая несёт чистые пары orderId/skuId/barcode.
        """
        groups = self.orders_with_items(invoice_id)
        return [
            item
            for group in groups
            for item in (group.get("items") or [])
            if item.get("barcode")
        ]

    def print_pdf(self, invoice_id: int) -> bytes:
        """GET /v1/fbs/invoice/{invoiceId}/print — печатная форма накладной (PDF)."""
        path = ENDPOINTS.fbs_invoice_print.format(invoice_id=invoice_id)
        return self._c.get(path)


class ReturnsAPI:
    """Возвраты продавца."""

    def __init__(self, client: UzumClient) -> None:
        self._c = client

    def list_page(
        self,
        *,
        return_id: int | None = None,
        page: int = 0,
        size: int = API.page_size,
    ) -> list[dict[str, Any]]:
        """GET /v1/return — страница возвратов (SellerReturnDto[])."""
        params = {"returnId": return_id, "page": page, "size": min(size, API.max_page_size)}
        return self._c.get(ENDPOINTS.returns, params=params) or []

    def iter_all(self, *, size: int = API.page_size) -> Iterator[dict[str, Any]]:
        return _paginate(lambda p: self.list_page(page=p, size=size), size=size)

    def shop_returns_page(
        self, shop_id: int, *, page: int = 0, size: int = API.page_size
    ) -> dict[str, Any]:
        """GET /v1/shop/{shopId}/return — накладные возврата магазина (SellerReturnLite)."""
        path = ENDPOINTS.shop_returns.format(shop_id=shop_id)
        return self._c.get(path, params={"page": page, "size": size}) or {}

    def shop_return_detail(self, shop_id: int, return_id: int) -> dict[str, Any]:
        """GET /v1/shop/{shopId}/return/{returnId} — состав накладной возврата."""
        path = ENDPOINTS.shop_return_detail.format(shop_id=shop_id, return_id=return_id)
        return self._c.get(path) or {}


class ProductsAPI:
    """Каталог товаров/SKU — источник полного справочника штрихкодов."""

    def __init__(self, client: UzumClient) -> None:
        self._c = client

    def iter_skus(
        self, shop_id: int, *, size: int = 100
    ) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
        """GET /v1/product/shop/{shopId} — итератор пар (карточка, SKU).

        Разворачивает AllProducts.productList[] → skuList[] (SkuForTable, где
        лежит barcode). page/size обязательны — листаем page++ до пустой выдачи.
        """
        page = 0
        path = ENDPOINTS.product_shop.format(shop_id=shop_id)
        while True:
            data = self._c.get(
                path, params={"page": page, "size": size, "filter": "ALL"}
            ) or {}
            cards = data.get("productList") or []
            if not cards:
                return
            for card in cards:
                for sku in card.get("skuList") or []:
                    yield card, sku
            if len(cards) < size:
                return
            page += 1


class StocksAPI:
    """Остатки SKU по схеме FBS/DBS.

    ЧТЕНИЕ — GET `/v3/fbs/sku/stocks` (конверт `payload.skuAmountList[]`, поле
    `amount`): авторитетный оперативный остаток FBS, точнее `quantityFbs` каталога.

    ЗАПИСЬ — POST `/v2/fbs/sku/stocks` (`update_fbs_stock`). По живой OpenAPI-схеме
    Uzum метод POST доступен ТОЛЬКО на v2; на v3 — лишь GET. Тело —
    `SkuStockUpdateApiRequestDto {skuAmountList:[RestSellerSkuFbsAmountDto]}`, где
    `barcode` и `amount` обязательны.

    Единая точка доступа к остаткам FBS: массовый синк (`sync_products`/`uzum_sync`)
    и фича «Управление остатками из Telegram» (точечный апдейт одного SKU).
    """

    def __init__(self, client: UzumClient) -> None:
        self._c = client

    def list_page(
        self, shop_ids: list[int], *, page: int = 0, size: int = 100
    ) -> list[dict[str, Any]]:
        """GET /v3/fbs/sku/stocks — одна страница остатков (skuAmountList[])."""
        payload = self._c.get(
            ENDPOINTS.fbs_sku_stocks,
            params={"shopIds": shop_ids, "page": page, "size": size},
        ) or {}
        return payload.get("skuAmountList") or []

    def iter_all(
        self, shop_ids: list[int], *, size: int = 100
    ) -> Iterator[dict[str, Any]]:
        """Итератор по всем остаткам с автопагинацией."""
        return _paginate(
            lambda p: self.list_page(shop_ids, page=p, size=size), size=size
        )

    def fbs_stock_map(self, shop_ids: list[int]) -> dict[int, int | None]:
        """{skuId: amount} — авторитетные оперативные остатки FBS из v3.

        Готовый словарь для O(1)-обогащения карточек товаров. SKU без skuId
        пропускаются; точечную фичу обслуживает `.get(sku_id)` по этой же карте.
        """
        stocks: dict[int, int | None] = {}
        for row in self.iter_all(shop_ids):
            sku_id = row.get("skuId")
            if sku_id is not None:
                stocks[int(sku_id)] = row.get("amount")
        return stocks

    def update_fbs_stock(
        self,
        sku_id: int,
        amount: int,
        *,
        barcode: str,
        sku_title: str | None = None,
        product_title: str | None = None,
    ) -> Any:
        """Обновить остаток FBS одного SKU — POST /v2/fbs/sku/stocks.

        Магазин определяется ТОКЕНОМ клиента (в теле shopId не передаётся). По
        спецификации `barcode` и `amount` обязательны. Идёт через UzumClient →
        под rate-limiter и ретраями; прикладную ошибку Uzum клиент поднимет как
        UzumAPIError (4xx/5xx или errors[] в теле).

        ⚠️ Эндпоинт v2: POST на /v3/fbs/sku/stocks Uzum пока не предоставляет
        (там только GET). См. config.fbs_sku_stocks_update.
        """
        item: dict[str, Any] = {"skuId": sku_id, "barcode": barcode, "amount": amount}
        if sku_title:
            item["skuTitle"] = sku_title
        if product_title:
            item["productTitle"] = product_title
        return self._c.post(
            ENDPOINTS.fbs_sku_stocks_update, json={"skuAmountList": [item]}
        )


class FinanceAPI:
    """Финансы магазина: баланс по заказам и движения денег (выплаты/удержания)."""

    def __init__(self, client: UzumClient) -> None:
        self._c = client

    def orders_page(
        self,
        shop_id: int,
        *,
        statuses: list[str] | None = None,
        page: int = 0,
        size: int = 50,
    ) -> list[dict[str, Any]]:
        """GET /v1/finance/orders — ОДНА страница позиций заказов для финрасчёта.

        Поля позиции: status, sellerProfit, commission, sellerPrice, amount…
        Одиночная страница нужна, чтобы вызывающий мог отследить полноту выгрузки.
        """
        payload = self._c.get(
            ENDPOINTS.finance_orders,
            params={
                "shopIds": [shop_id],
                "statuses": statuses,
                "page": page,
                "size": size,
                "group": False,
            },
        ) or {}
        return payload.get("orderItems") or []

    def expenses(
        self,
        shop_id: int,
        *,
        page: int = 0,
        size: int = 50,
        date_from: int | None = None,
        date_to: int | None = None,
    ) -> list[dict[str, Any]]:
        """GET /v1/finance/expenses — движения денег (payments[]: выплаты/удержания).

        Шлём и shopId, и shopIds[] — у разных контуров Uzum срабатывает один из них.
        """
        payload = self._c.get(
            ENDPOINTS.finance_expenses,
            params={
                "shopId": shop_id,
                "shopIds": [shop_id],
                "page": page,
                "size": size,
                "dateFrom": date_from,
                "dateTo": date_to,
            },
        ) or {}
        return payload.get("payments") or []


class UzumAPI:
    """Фасад: единая точка доступа ко всем доменным API."""

    def __init__(self, client: UzumClient) -> None:
        self.client = client
        self.shops = ShopsAPI(client)
        self.orders = OrdersAPI(client)
        self.invoices = InvoicesAPI(client)
        self.returns = ReturnsAPI(client)
        self.products = ProductsAPI(client)
        self.stocks = StocksAPI(client)
        self.finance = FinanceAPI(client)


__all__ = [
    "UzumAPI",
    "ShopsAPI",
    "OrdersAPI",
    "InvoicesAPI",
    "ReturnsAPI",
    "ProductsAPI",
    "StocksAPI",
    "FinanceAPI",
]
