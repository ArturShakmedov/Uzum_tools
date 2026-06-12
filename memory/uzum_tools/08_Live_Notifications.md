# 🔔 Live-лента уведомлений (Фича №3)

Назад к [[00_Index]]. Модуль `services/notification_worker.py`. Опирается на синк
из [[05_API_Integration]], расчёт прибыли из [[03_Calculator_&_Economics]] /
[[04_Sales_Analytics_&_ABC]] и стек из [[07_Infrastructure_&_Sizing]].

> Файл назван `08_…`, а не `06_…` (как в ТЗ): номер `06` уже занят
> [[06_Bot_Flow_&_States]] — чтобы не плодить дубль-номера, фича вынесена на 08.

Фоновый воркер раз в **10 минут** обходит все активные магазины, тянет лёгкую
дельту заказов и шлёт в Telegram «живые» уведомления о новых заказах и возвратах.

---

## Архитектура воркера

Под наш стек (Postgres + Redis, sync-сессии + `asyncio.to_thread`):

```
notification_loop(bot)                    # бесконечный while True + asyncio.sleep(600)
  └─ run_notification_cycle(bot)
       ├─ _load_active_shops()            # to_thread → list_all_active_shops() (PostgreSQL)
       └─ gather(_process_shop, …)        # все магазины параллельно
            ├─ fetch_recent_orders(...)   # to_thread ПОД FETCH_SEMAPHORE (сеть, httpx-sync)
            ├─ detect_events(...)         # to_thread (БД: дедуп + прибыль + рендер)
            └─ bot.send_message(...)      # в основном loop, по одному событию
```

Ключевые решения:

| Аспект | Решение |
|--------|---------|
| Планировщик | простой `while True: … ; await asyncio.sleep(POLL_INTERVAL)` (`POLL_INTERVAL=600`) |
| Запуск | `start_notifications(bot)` → `asyncio.create_task(notification_loop(bot))` в `bot.main()` рядом с polling; в `--dry-run` НЕ стартует |
| Потолок RAM/сети | сетевая фаза — `async with FETCH_SEMAPHORE` (общий с полным синком, 10) |
| Параллелизм записи | без write-лока — MVCC PostgreSQL (см. [[07_Infrastructure_&_Sizing]]) |
| Изоляция от падений | каждый магазин/отправка в `try/except`; цикл не падает целиком |
| Снимок магазина | `ActiveShop(telegram_id, shop_id, token)` — plain dataclass, без ORM-привязки между потоками |

### Стоимость (лёгкая, по ТЗ «не создавая лишней нагрузки»)

- **~2 запроса на магазин за цикл**: страница `CREATED` (новые, с `date_from`) +
  страница `RETURNED` (возвраты), `page=0, size=50`.
- 500 магазинов × 2 / 10 мин ≈ **1.7 req/s** суммарно, под `FETCH_SEMAPHORE(10)`.
- Окно «новизны» — `WINDOW_MINUTES=60` (заказы старше часа за новые не считаем).

---

## Дедупликация в PostgreSQL

Состояние дедупа — это сама таблица `orders` (никаких отдельных «seen»-структур).
`detect_events` сверяет id входящих заказов с БД одним запросом:

```python
existing = {row.uzum_id: row.status for row in session.execute(
    select(Order.uzum_id, Order.status).where(
        Order.telegram_id == telegram_id, Order.uzum_id.in_(ids)))}
```

Правила классификации каждого заказа:

| Условие | Действие |
|---------|----------|
| id **не** в БД, статус `CREATED`, создан в окне 60 мин | 💰 **новый заказ** + `save_orders` |
| id **не** в БД, прочее (старый/возврат, попавший в выборку) | молча `save_orders` (фиксируем id, без шума) |
| id в БД, статус стал `RETURNED` (был не-`RETURNED`) | ⚠️ **возврат** + `save_orders` (обновит статус) |
| id в БД, статус не изменился | пропуск |

**Почему повторов нет:** новый заказ после показа сохраняется (его id уже в БД →
в следующем цикле он «знакомый»). Возврат после показа перезаписывает статус на
`RETURNED` → условие `existing[oid] not in _RETURN_STATUSES` больше не сработает.
Идемпотентность гарантирует `_upsert` по `(telegram_id, uzum_id)` (см.
[[01_Database_Schema]]). Проверено тестом: 2-й проход тех же данных → 0 событий.

---

## Расчёт прибыли «на лету»

`_estimate_profit(session, telegram_id, order)` — та же модель выручки, что в
CTE-аналитике ([[04_Sales_Analytics_&_ABC]]): **выручка позиции = цена заказа /
число позиций**. Дальше для каждой позиции:

- SKU находим по `order_items.skuTitle == user_products.article` (канонический мост,
  [[01_Database_Schema#Мост аналитики продаж]]);
- если у SKU задана `purchase_price` **И** известна комиссия категории
  (`uzum_categories`) → полная юнит-экономика через `services.calculator.
  compute_unit_economics` (себестоимость + **комиссия категории** + **логистика
  Uzum** по габариту + налог 4%);
- иначе fallback «выручка − закупка» и пометка «≈ оценка».

Проверено: при заданной категории (комиссия 10%) заказ 120 000 с закупкой 50 000 →
`net = 120000 − 50000 − 12000(комм) − 5000(логистика МГТ) − 4800(налог) = 48200`.

---

## Формат уведомлений (HTML + эмодзи)

`bot.send_message(parse_mode=HTML)`. Название/размер — по первой позиции
(`user_products.title` + `sku_suffix(article)`), всё через `html.escape`.

**💰 Новый заказ:**

```
💰 <b>Новый заказ!</b>
📦 Платье летнее (L)
💵 Цена: <b>120 000</b> сум
📈 Чистая прибыль: <b>+70 000</b> сум <i>(≈ оценка)</i>
```

(`<i>(≈ оценка)</i>` — когда прибыль приблизительная; иначе строка кончается `!`.
Знак прибыли `+`/`−`; для заказа из нескольких позиций к названию добавляется
`+N поз.`)

**⚠️ Возврат товара:**

```
⚠️ <b>Возврат товара!</b>
📦 Платье летнее (L)
↩️ Статус: <b>Возвращено на склад</b>
```

---

## Параметры (модульные константы)

| Константа | Значение | Смысл |
|-----------|----------|-------|
| `POLL_INTERVAL` | `600` | период обхода, сек |
| `WINDOW_MINUTES` | `60` | окно «новизны» заказа |
| `_NEW_STATUS` | `"CREATED"` | статус нового FBS-заказа |
| `_RETURN_STATUSES` | `("RETURNED",)` | статусы возврата |

Репозиторий: добавлена `list_all_active_shops(session)` — активные магазины ВСЕХ
пользователей (один на `telegram_id`), токен расшифровывается на чтении
(`EncryptedToken`).
