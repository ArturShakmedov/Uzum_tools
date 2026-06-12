# 📊 Аналитика продаж и ABC-анализ

Назад к [[00_Index]]. Использует [[01_Database_Schema#Мост аналитики продаж]] и
группировку из [[02_Product_Catalog_&_Group_Logic]].

Дашборд считает продажи **на уровне модели** (`sku_root`) и присваивает ей
ABC-класс по вкладу в прибыль магазина. SQL — в `database/repository.py`, чистая
логика и рендер — в `services/sales_analytics.py`, обвязка — в
`handlers/products_handlers.py` (`prod:stats` / `prod:statp`).

---

## Реальность данных, определившая архитектуру

В `order_items` заполнен только `sku_title` (= полный артикул), а `sku_id`,
`product_title`, `seller_price` — **NULL**, `amount` = 1. Следствия:

1. Привязка продажи к модели — **только** через `p.article = oi.sku_title`
   (совпадение 100%), который даёт `sku_root` и `purchase_price`.
2. Выручку несёт **только** `orders.price` — цена всего заказа. Поскольку в заказе
   бывает несколько позиций, на позицию делим: `o.price / lines_in_order`.

**Подсчёт числа позиций — через CTE (исправление W-1/W-2).** Раньше число позиций
считалось коррелированным подзапросом **на каждую строку** результата (O(n²)).
Теперь `lines_in_order` считается один раз в CTE `WITH lines AS (...)` и
подмешивается обычным `JOIN lines l`. Деление защищено `NULLIF(l.n, 0)` от
рассинхрона данных (в SQLite деление на 0 даёт NULL, не падает):

```sql
WITH lines AS (                       -- число позиций на заказ: ОДИН GROUP BY
    SELECT order_uzum_id, telegram_id, COUNT(*) AS n
    FROM order_items GROUP BY order_uzum_id, telegram_id
)
-- Выручка позиции = цена заказа / число позиций (с защитой от деления на 0)
o.price * 1.0 / NULLIF(l.n, 0)
```

Наборы статусов:

```python
_SOLD_STATUSES     = ("COMPLETED", "DELIVERED",
                      "DELIVERED_TO_CUSTOMER_DELIVERY_POINT", "ACCEPTED_AT_DP")
_RETURNED_STATUSES = ("RETURNED",)
# CANCELED исключён из выручки полностью
```

---

## SQL-агрегация модели — `get_model_sales_stats`

Условная агрегация (`CASE WHEN o.status IN (...)`) за период `days`. Один проход по
позициям модели:

```sql
WITH lines AS (SELECT order_uzum_id, telegram_id, COUNT(*) AS n
               FROM order_items GROUP BY order_uzum_id, telegram_id)
SELECT
  COALESCE(SUM(CASE WHEN o.status IN (<SOLD>)     THEN oi.amount ELSE 0 END), 0)          AS units_sold,
  COALESCE(SUM(CASE WHEN o.status IN (<SOLD>)     THEN <LINE_REVENUE> ELSE 0 END), 0)     AS revenue,
  COALESCE(SUM(CASE WHEN o.status IN (<RETURNED>) THEN oi.amount ELSE 0 END), 0)          AS returns_qty,
  COALESCE(SUM(CASE WHEN o.status IN (<RETURNED>) THEN <LINE_REVENUE> ELSE 0 END), 0)     AS returns_sum,
  COALESCE(SUM(CASE WHEN o.status IN (<SOLD>)
                    THEN COALESCE(p.purchase_price, 0) * oi.amount ELSE 0 END), 0)        AS cogs,
  COALESCE(SUM(CASE WHEN o.status IN (<SOLD>)
                    AND p.purchase_price IS NULL THEN oi.amount ELSE 0 END), 0)           AS units_no_cost
FROM order_items oi
JOIN orders        o ON o.uzum_id = oi.order_uzum_id AND o.telegram_id = oi.telegram_id
JOIN user_products p ON p.article = oi.sku_title     AND p.telegram_id = oi.telegram_id
JOIN lines         l ON l.order_uzum_id = oi.order_uzum_id AND l.telegram_id = oi.telegram_id
WHERE p.telegram_id = :tg AND p.sku_root = :root AND o.date_created >= :cutoff
```

> ⚡ **Индексы JOIN (W-1):** `user_products.article` и `order_items.sku_title`
> проиндексированы (`index=True` + аддитивная миграция `CREATE INDEX IF NOT EXISTS`
> в `connection.py`) — джойн по строковому ключу больше не идёт full-scan'ом. См.
> [[01_Database_Schema#Мост аналитики продаж]].

Python-обёртка считает `cutoff = now(UTC) - timedelta(days)`, выводит производные:

```python
revenue    = int(round(row.revenue))
cogs       = int(round(row.cogs))
net_profit = revenue - cogs
return {
    "units_sold": int(row.units_sold), "revenue": revenue,
    "returns_qty": int(row.returns_qty), "returns_sum": int(round(row.returns_sum)),
    "cogs": cogs, "net_profit": net_profit,
    "units_no_cost": int(row.units_no_cost),
    "cost_complete": int(row.units_no_cost) == 0,
    "margin_pct": (net_profit / revenue * 100) if revenue > 0 else None,
}
```

> `date_created` хранится строкой ISO; сравнение с Python-`datetime` работает —
> `sqlite3` адаптирует его в сопоставимую ISO-строку.

Разрез прибыли по всем моделям для ABC — `get_shop_profit_by_root` (тот же JOIN +
`JOIN lines`, `GROUP BY p.sku_root`, `net = выручка − закупка`), возвращает
`{root: net_profit}`.

---

## Обработка незаданной закупки

`purchase_price` задана лишь у малой части SKU. В SQL `COALESCE(p.purchase_price, 0)`
→ себестоимость ненайденных = 0, а `units_no_cost` считает такие проданные единицы.
Если `cost_complete = False`, карточка показывает предупреждение:

```python
profit_warn = "" if s["cost_complete"] else (
    f"\n⚠️ <i>У {s['units_no_cost']} шт. не задана закупка — "
    "чистая прибыль приблизительная.</i>"
)
```

Как только селлер проставит закупки (через [[03_Calculator_&_Economics]]), цифры
станут точными без изменений кода.

---

## Чистая функция `classify_abc`

ABC по кумулятивному вкладу в прибыль. Базу 100% формируют **только прибыльные**
модели (убыточные не искажают знаменатель):

```python
def classify_abc(target_root, profit_by_root) -> dict:
    positives = {r: v for r, v in profit_by_root.items() if v > 0}
    total  = sum(positives.values())
    target = profit_by_root.get(target_root, 0)
    if total <= 0 or target <= 0:
        return {"abc": "C", "share_pct": 0.0, "cumulative_pct": None}

    share = target / total * 100.0
    cumulative = 0.0
    for root, profit in sorted(positives.items(), key=lambda kv: kv[1], reverse=True):
        cumulative += profit / total * 100.0
        if root == target_root:
            break

    if share > 20.0 or cumulative <= 80.0:      abc = "A"   # флагман / топ-80%
    elif cumulative <= 95.0 and share >= 5.0:   abc = "B"   # следующие до 95%
    else:                                        abc = "C"   # хвост / <5% / убыток
    return {"abc": abc, "share_pct": share, "cumulative_pct": cumulative}
```

Принципы распределения:

| Класс | Условие | Смысл для селлера |
|-------|---------|-------------------|
| **A** | доля `> 20%` **или** кумулятивно в топ-80% | Флагман. Держать остаток, не уходить в OOS, осторожно со скидками |
| **B** | кумулятивно до 95% и доля `≥ 5%` | Середняк. Кандидат на рост: акции, расширение сетки, апсейл к A |
| **C** | хвост / доля `< 5%` / убыток | Низкий вклад. Не морозить склад, распродажа/вывод из ассортимента |

Текстовые пояснения каждого класса — словарь `ABC_HINT` в сервисе.

---

## Оркестрация и UI

`build_model_analytics(telegram_id, sku_root, days)` тянет статистику + разрез
прибыли, классифицирует и рендерит HTML-карточку (`{title, text, abc}`).

Поток в боте:
- `prod:stats:<repr_sku>` → выбор периода (`analytics_period_kb`: 7 / 14 / 30 дней);
- `prod:statp:<repr_sku>:<days>` → `_root_of(repr_sku)` резолвит корень →
  `build_model_analytics` в `to_thread` → сообщение со сводкой.

> В `callback_data` зашит **int `repr_sku`**, а не кириллический `sku_root` —
> иначе данные превысят лимит Telegram 64 байта. Корень резолвится на сервере.

Сводка показывает: Выручку, Продано шт., Возвраты (шт./сумма), Себестоимость,
Чистую прибыль, Маржинальность, Вклад в прибыль магазина и ABC-класс с пояснением.
Подробности сценария — в [[06_Bot_Flow_&_States#Открытие аналитики]].
