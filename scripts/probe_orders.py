"""Диагностика 'заказов 0': берём реальный orderId из накладных в БД и
сверяем, как он отдаётся детальным эндпоинтом и списком /v2/fbs/orders
при разных фильтрах (даты/статус). Токен не печатается.

Запуск:  python scripts/probe_orders.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from api.client import UzumClient
from api.endpoints import UzumAPI
from database.connection import SessionLocal
from database.models import Barcode


def _shop_ids(api: UzumAPI) -> list[int]:
    return [s["id"] for s in api.shops.list() if "id" in s]


def main() -> int:
    # 1. достаём известный orderId из ранее сохранённых штрихкодов
    with SessionLocal() as s:
        row = s.execute(select(Barcode.order_uzum_id).limit(1)).scalar_one_or_none()
    if row is None:
        print("В БД нет штрихкодов — сначала прогони main.py.")
        return 1
    order_id = int(row)
    print(f"Пробный orderId из накладной: {order_id}\n")

    with UzumClient() as client:
        api = UzumAPI(client)
        shop_ids = _shop_ids(api)
        print(f"shopIds = {shop_ids}\n")

        # 2. детальный заказ — узнаём реальные status / dateCreated / scheme
        detail = api.orders.detail(order_id)
        for key in ("id", "status", "scheme", "shopId", "dateCreated", "invoiceNumber"):
            print(f"  detail.{key} = {detail.get(key)!r}")
        real_status = detail.get("status")
        print()

        now_ms = int(time.time() * 1000)
        year_ms = 365 * 86_400_000
        cases = [
            ("status=real, без дат",        {"status": real_status}),
            ("status=real, окно 365д",      {"status": real_status, "date_from": now_ms - year_ms, "date_to": now_ms}),
            ("без status, без дат",         {}),
            ("без status, окно 365д",       {"date_from": now_ms - year_ms, "date_to": now_ms}),
        ]
        for label, kw in cases:
            page = api.orders.list_page(shop_ids, page=0, size=50, **kw)
            ids = [o.get("id") for o in page]
            hit = "✓ нашёлся" if order_id in ids else "—"
            print(f"[{len(page):>3}] {label}: {hit}  (первые id: {ids[:5]})")

        # 3. счётчик заказов без фильтра
        total = api.orders.count(shop_ids)
        print(f"\n/v2/fbs/orders/count (default CREATED) = {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
