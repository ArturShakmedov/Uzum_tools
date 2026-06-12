# 🔌 Интеграция с Uzum Seller OpenAPI

Назад к [[00_Index]]. Эндпоинты заданы в `config.py`, HTTP-слой — `api/client.py`,
типизированные обёртки — `api/endpoints.py`.

Базовый URL: `https://api-seller.uzum.uz/api/seller-openapi`.

---

## Авторизация

Схема `TokenAuth` — apiKey в заголовке. **Токен передаётся «как есть», без префикса
`Bearer`**:

```
Authorization: <raw_token>
```

Токен селлера хранится в БД зашифрованным (Fernet, тип `EncryptedToken`) в
`user_shops.uzum_token` — см. [[01_Database_Schema]]. Машиночитаемая спецификация
доступна по `/swagger/api-docs` (пути `/v3/api-docs` и Swagger UI отдают
`403 RBAC: access denied`).

### Rate limiting

Сервер возвращает заголовки `X-RateLimit-Replenish-Rate / -Remaining / -Burst-Capacity`
и легко отдаёт `429`. Клиент (`_RateLimiter` в `api/client.py`) подстраивает
`min_interval` под `1/replenish_rate` из ответа; стартовая скорость ~3 req/s.

---

## 🔄 Архитектура синхронизации — ДВЕ ФАЗЫ (исправление C-1)

`services/uzum_sync.py` выкачивает заказы, каталог, возвраты и накладные. Чтобы
сетевой I/O (минуты) не блокировал event loop бота и не сериализовал всех 500+
пользователей, синк разделён на две независимые фазы:

```python
# Per-user lock: разные юзеры качают из Uzum ПАРАЛЛЕЛЬНО, дубль-синк одного
# юзера сериализуется. Сеть под этим локом — но не под глобальным.
_user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
# 🛡️ Глобальный потолок одновременных СЕТЕВЫХ синков — защита от OOM-killer.
FETCH_SEMAPHORE = asyncio.Semaphore(10)
# Короткий лок ТОЛЬКО вокруг фазы записи (~1–3 с): SQLite-дев — против lock,
# Postgres — страховка поверх MVCC. Сеть его НЕ держит.
DB_WRITE_SEMAPHORE = asyncio.Semaphore(1)

async def run_full_sync(telegram_id, on_progress=None) -> SyncReport:
    async with _user_locks[telegram_id]:                          # фаза 1: сеть
        async with FETCH_SEMAPHORE:                               # потолок RAM
            bundle = await asyncio.to_thread(fetch_everything_from_uzum, telegram_id, on_progress)
    async with DB_WRITE_SEMAPHORE:                                # фаза 2: запись
        report = await asyncio.to_thread(persist_to_db, telegram_id, bundle, on_progress)
    return report
```

### 🛡️ `FETCH_SEMAPHORE = 10` — официальный механизм защиты от OOM

Каждый синк в полёте держит в RAM `SyncBundle` (~15–25 МБ: orders+items, каталог,
накладные). После C-1 сетевая фаза разных юзеров идёт **параллельно**, поэтому без
потолка пик RAM = `N_concurrent × ~20 МБ` — неограничен (100 юзеров → ~2 ГБ → OOM).

`FETCH_SEMAPHORE` (модульный, `services/uzum_sync.py`) жёстко ограничивает число
**одновременных** сетевых синков до **10**, фиксируя пик RAM на **~200 МБ** даже
при одновременном запуске у 100+ пользователей. Лишние синки ждут в очереди на
семафоре (не падают). Проверено: при 100 параллельных `run_full_sync` фактическая
пиковая конкурентность сетевой фазы = ровно 10.

> Семафор ставится в async-оркестраторе `run_full_sync` вокруг
> `await asyncio.to_thread(fetch_everything_from_uzum, …)`, а НЕ внутри самой
> fetch-функции: она синхронна (httpx-sync, исполняется в воркер-потоке), поэтому
> `async with` внутри неё невозможен — а на границе `to_thread` эффект тот же.
> Тюнинг под сервер: `пик_RAM ≈ FETCH_SEMAPHORE × 25 МБ` (см. [[07_Infrastructure_&_Sizing]]).

| Фаза | Функция | Что делает | Под чем |
|------|---------|-----------|---------|
| 1. Сбор | `fetch_everything_from_uzum` | весь httpx-I/O → `SyncBundle` (только dict'ы, без ORM/сессии) | `_user_locks[tg]` (юзеры параллельны) |
| 2. Запись | `persist_to_db` | upsert `SyncBundle` в БД (PostgreSQL) + `link_order_item_barcodes` + `touch_active_shop_sync` | короткий `DB_WRITE_SEMAPHORE` (~1–3 с) |

**Было:** один глобальный `SYNC_SEMAPHORE = Semaphore(1)` оборачивал ВЕСЬ синк,
включая сеть → один пользователь блокировал всех остальных на минуты (самодельный
DoS). Теперь под глобальным локом держится только короткая запись в БД.

- Оркестратор `run_full_sync` — **async** (бот: `handlers/common.ensure_fresh`
  вызывает `await run_full_sync(...)`).
- CLI `main.py` синхронный и однопользовательский — зовёт фазы напрямую:
  `bundle = fetch_everything_from_uzum(...); report = persist_to_db(...)`.
- `SyncBundle` (dataclass) не держит сессию/соединение → безопасно передаётся
  между воркер-потоками.

См. также группировку каталога в [[02_Product_Catalog_&_Group_Logic]].

---

## Каталог товаров — `ProductsAPI.iter_skus`

Источник карточек и SKU (`GET /v1/product/shop/{shopId}`). Разворачивает
`AllProducts.productList[]` → `skuList[]`, листает `page++` до пустой выдачи:

```python
def iter_skus(self, shop_id, *, size=100):
    page = 0
    path = ENDPOINTS.product_shop.format(shop_id=shop_id)
    while True:
        data = self._c.get(path, params={"page": page, "size": size, "filter": "ALL"}) or {}
        cards = data.get("productList") or []
        if not cards:
            return
        for card in cards:
            for sku in card.get("skuList") or []:
                yield card, sku
        if len(cards) < size:
            return
        page += 1
```

Каждый `sku` несёт `article`/`sellerItemCode`, `skuTitle`, цену и остатки — отсюда
`sync_products` берёт данные и считает `sku_root` (см. [[02_Product_Catalog_&_Group_Logic]]).

---

## Остатки FBS — ЧТЕНИЕ на v3 (GET), ЗАПИСЬ на v2 (POST)

> 🔎 **По живой OpenAPI-схеме Uzum** (`/swagger/api-docs`):
> `/v3/fbs/sku/stocks` имеет **только `get`**, а `/v2/fbs/sku/stocks` — `get`+`post`.
> Поэтому: **читаем остатки через v3 GET** (поле `amount`), а **обновляем через
> v2 POST** (другого метода записи Uzum пока не публикует). Анонсированное
> отключение v2 касается GET-чтения (его заменяет v3); POST-запись остаётся на v2.

**`StocksAPI`** (`api/endpoints.py`) — единая точка доступа к остаткам FBS:

```python
class StocksAPI:
    # ЧТЕНИЕ — GET /v3/fbs/sku/stocks (конверт payload.skuAmountList[], поле amount)
    def list_page(self, shop_ids, *, page=0, size=100): ...
    def fbs_stock_map(self, shop_ids) -> dict[int,int|None]: ...   # {skuId: amount}
    # ЗАПИСЬ — POST /v2/fbs/sku/stocks (config.fbs_sku_stocks_update)
    def update_fbs_stock(self, sku_id, amount, *, barcode, sku_title=None, product_title=None):
        item = {"skuId": sku_id, "barcode": barcode, "amount": amount}   # barcode+amount обязательны
        return self._c.post(ENDPOINTS.fbs_sku_stocks_update, json={"skuAmountList": [item]})
```

| Операция | Поле БД | Эндпоинт |
|----------|---------|----------|
| Чтение остатка FBS | `fbs_stock` ← v3 `amount` (фолбэк `quantityFbs`) | **GET** `/v3/fbs/sku/stocks` |
| Остаток FBO | `fbo_stock` ← `quantityActive` | `/v1/product/shop/{id}` (каталог) |
| **Запись остатка FBS** | `fbs_stock` → POST + локально | **POST** `/v2/fbs/sku/stocks` |

**Чтение (v3)** — две точки через `fbs_stock_map`: `sync_products` (on-demand) и
`fetch_everything_from_uzum` (полный синк, под `FETCH_SEMAPHORE` → `update_fbs_stocks`).

**Запись (v2)** — фича «Изменить остаток FBS из Telegram» (см. [[06_Bot_Flow_&_States]]):
`services.products.update_fbs_stock_remote` → `StocksAPI.update_fbs_stock`. Тело —
`SkuStockUpdateApiRequestDto`; **`barcode` обязателен**, поэтому хранится в
`user_products.barcode` (маппинг в `sync_products`, бэкфилл через `needs_product_resync`).
Эндпоинт записи вынесен в `config.fbs_sku_stocks_update` — одна строка для перехода
на v3 POST, когда Uzum его опубликует.

---

## Прочие контуры API

- **OrdersAPI** — `/v2/fbs/orders` (по умолчанию статус `CREATED`, нужно итерировать
  все статусы; дата-фильтр сервера ненадёжен → фильтр по `dateCreated` на клиенте).
- **Возвраты** — `/v1/return` (без фильтров по дате/статусу, `size ≤ 50`,
  пагинация до пустоты) — основной источник возвращённого объёма.
- **FinanceAPI** — `/v1/finance/orders` (баланс по `sellerProfit`) и
  `/v1/finance/expenses` (выплаты/удержания). Готового эндпоинта баланса нет —
  агрегируется из позиций, результат кэшируется в `finance_snapshots`.

Лимиты страниц различаются: orders/return `size ≤ 50`, `/v1/fbs/invoice` `size ≤ 20`
(`config.invoice_max_page_size = 20`).
