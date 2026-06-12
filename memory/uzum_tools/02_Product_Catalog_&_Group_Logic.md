# 📦 Каталог и логика группировки

Назад к [[00_Index]]. Опирается на `sku_root` из [[01_Database_Schema]].

Раздел **«📦 Мои товары»** (`handlers/products_handlers.py`) показывает не каждый
SKU отдельно (у селлера их сотни), а **модели** — группы модификаций с общим
корнем артикула `sku_root`.

---

## Синхронизация каталога

`services/products.sync_products(telegram_id)` тянет карточки активного магазина
через `ProductsAPI.iter_skus` и сохраняет в `user_products`. При сохранении сразу
считаются артикул и корень:

```python
article = sku.get("article") or sku.get("sellerItemCode") or sku.get("skuFullTitle")
# Корень для группировки; фолбэк по продукту/SKU, чтобы root НИКОГДА не был NULL.
root = sku_root(article) or (
    f"pid{card.get('productId')}" if card.get("productId") else f"sku{sku_id}"
)
```

`save_user_products` — идемпотентный upsert по `(telegram_id, uzum_id)`, при
ре-синке **сохраняет вручную заданный `purchase_price`** (не затирает его).

`fbs_stock` берётся из **v3 `/v3/fbs/sku/stocks`** (поле `amount`, через
`api.stocks.fbs_stock_map`), а не из каталожного `quantityFbs` (он — лишь фолбэк).
v2 отключён Uzum с 15.06.2026. Детали — в [[05_API_Integration]].

> Каталог входит и в **полный** синк (`services/uzum_sync.py`), который после
> исправления C-1 разделён на две фазы: сбор из сети (`fetch_everything_from_uzum`,
> параллельно у разных юзеров под per-user lock) и атомарная запись
> (`persist_to_db` под `DB_WRITE_SEMAPHORE`). Сетевую фазу — включая выкачку
> v3-остатков FBS — глобально ограничивает **`FETCH_SEMAPHORE = asyncio.Semaphore(10)`**:
> официальный потолок одновременных синков, фиксирующий пик RAM на ~200 МБ
> (защита от OOM-killer при синке у 100+ юзеров одновременно). Проверено: при 100
> параллельных `run_full_sync` фактическая конкурентность = 10. Архитектура и
> тюнинг — в [[05_API_Integration]] и [[07_Infrastructure_&_Sizing]].

---

## Самолечение каталога (`needs_product_resync`)

Существующие записи, синхронизированные до появления колонок `article`/`sku_root`,
имеют `sku_root IS NULL` и не группируются. Бот лечит это автоматически при первом
заходе в раздел:

```python
def needs_product_resync(session, telegram_id) -> bool:
    return session.execute(
        select(func.count()).select_from(UserProduct).where(
            UserProduct.telegram_id == telegram_id,
            UserProduct.sku_root.is_(None),
        )
    ).scalar_one() > 0
```

Хэндлер `on_products` запускает ре-синк, если каталог либо пуст, либо «протух»:

```python
total, stale = await asyncio.to_thread(_products_state, telegram_id)  # stale = total>0 and needs_product_resync
if total == 0 or stale:
    note = "📦 Загружаю товары…" if total == 0 else "🔄 Обновляю товары для группировки…"
    ...
    await asyncio.to_thread(sync_products, telegram_id)
```

После ре-синка все строки получают `article`/`sku_root`, и `needs_product_resync`
становится `False` — повторный заход идёт мгновенно из кэша.

---

## Группировка и пагинация (по 5)

Список моделей строится через `GROUP BY sku_root`. Представитель группы —
минимальный `uzum_id`, заголовок — минимальный `title`:

```python
def list_product_groups(session, telegram_id, *, offset=0, limit=5):
    stmt = (
        select(
            func.min(UserProduct.uzum_id).label("repr_sku"),   # представитель
            func.min(UserProduct.title).label("title"),
            UserProduct.sku_root,
        )
        .where(UserProduct.telegram_id == telegram_id)
        .group_by(UserProduct.sku_root)
        .order_by(func.min(UserProduct.title).asc())
        .offset(offset).limit(limit)
    )
    return [(r.repr_sku, r.title, r.sku_root) for r in session.execute(stmt)]
```

`count_product_groups` считает `COUNT(*)` по подзапросу с `GROUP BY sku_root`
(корректный счёт уникальных корней). Пагинация в хэндлере — `PAGE_SIZE = 5`,
кнопки `prod:page:<n>` (⬅️/➡️ + счётчик `prod:noop`). Подпись кнопки группы:

```python
def _group_label(title, root):
    name = title or "Без названия"
    return f"📦 {name}" if _is_synthetic_root(root) else f"📦 {name} ({root})"
# синтетический корень (pid…/sku…) в подписи скрывается
```

---

## Агрегация на уровне группы

При открытии модели (`prod:view:<repr_sku>`) `_load_group_card` собирает все её
размеры через `list_products_by_root` и **агрегирует**:

```python
siblings = list_products_by_root(session, telegram_id, root)   # все размеры модели
fbo = sum(p.fbo_stock or 0 for p in siblings)                  # Σ остаток склада Uzum
fbs = sum(p.fbs_stock or 0 for p in siblings)                  # Σ остаток своего склада
prices = [p.current_price for p in siblings if p.current_price]
# Кнопка размера: суффикс артикула → иначе sku_title → иначе «•»
sizes = [(p.uzum_id, sku_suffix(p.article) or p.sku_title or "•") for p in siblings]
return {
    "title": repr_p.title, "root": root, "repr_sku": repr_p.uzum_id,
    "count": len(siblings), "fbo_stock": fbo, "fbs_stock": fbs,
    "price_min": min(prices) if prices else None,
    "price_max": max(prices) if prices else None,
    "sizes": sizes,
}
```

Цены сворачиваются в **диапазон**: если `price_min == price_max` — одна цена,
иначе `«120 000–125 000»` (функция `_price_text`). Остатки FBO/FBS суммируются по
всем размерам — селлер видит общий склад модели. Динамический ряд кнопок-размеров
(`prod:select_sku:<sku_id>`) и кнопка [[04_Sales_Analytics_&_ABC|📊 Аналитика продаж]]
живут в `keyboards.products.group_card_kb`.

Клик по размеру → карточка конкретного SKU (`product_card_kb`) с действиями
🧮 Юнит-экономика и 📉 Симулятор акции (см. [[06_Bot_Flow_&_States]]).

---

## Поиск по товарам (AND-стемминг)

Поиск **категорий** для калькулятора (`repository.search_categories`) устойчив к
словоформам и кириллице. Лёгкий стеммер срезает окончание:

```python
def _stem(word: str) -> str:
    """'блузка'→'блузк' найдёт 'блузки', 'платье'→'плать' найдёт 'платья'.
    Слова короче 5 символов не трогаем."""
    return word[:-1] if len(word) >= 5 else word

def _like_escape(s: str) -> str:
    """Экранировать спецсимволы LIKE (%, _) и слэш. Порядок важен: слэш первым."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def search_categories(session, query, limit=5):
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
```

Каждое слово запроса добавляет свой `LIKE` — то есть условия объединяются по **И**
(все корни должны встретиться): «женская одежда» найдёт строку, где есть и
«женск», и «одежд».

> 🛡️ **Защита от LIKE-инъекции (исправление W-5):** ввод проходит через
> `_like_escape` + модификатор `.like(..., escape="\\")`. Без этого пользователь,
> введя `%`, матчил бы **все** категории, а `_` — любой одиночный символ. Та же
> защита применена в `_resolve_category_id` (резолв категории товара по имени).
> SQL-инъекция тут была невозможна и раньше (значение шло параметризованным
> биндом) — экранируются именно wildcard-символы самого оператора LIKE.

> ⚠️ **Поиск по собственным товарам** (`search_user_products`) фильтрует в **Python**,
> а не в SQL: `LOWER()`/`LIKE` в SQLite корректны только для ASCII и ломают
> регистр кириллицы, а названия товаров хранятся в исходном регистре. Объём на
> магазин небольшой, поэтому `cleaned in f"{title} {sku_title} {article}".lower()`
> дёшев. Результат **сгруппирован** — возвращает кортежи `(repr_sku, title, sku_root)`
> (по одному на корень), а не дубли размеров. Одно совпадение → сразу карточка
> модели; много → список групп.
>
> Поскольку здесь нет SQL-`LIKE` (сопоставление идёт оператором Python `in`),
> LIKE-инъекция тут невозможна и `_like_escape` не требуется — в отличие от
> `search_categories`/`_resolve_category_id`, где LIKE реальный (см. W-5 выше).
