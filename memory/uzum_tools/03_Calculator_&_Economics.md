# 🧮 Калькулятор юнит-экономики

Назад к [[00_Index]]. Источник комиссий — `uzum_categories` из [[01_Database_Schema]].

Чистая математика лежит в `services/calculator.py` (без aiogram, легко тестируется).
Бот-обвязка — `handlers/calculator_handlers.py`, FSM `Calc` (см. [[06_Bot_Flow_&_States]]).

---

## Справочник тарифов логистики

```python
LOW_PRICE_THRESHOLD  = 59_000
HIGH_PRICE_THRESHOLD = 5_000_000
TAX_RATE = 0.04   # 4% для ИП/самозанятых

LOGISTICS_FEES = {
    "low": 4_000,   # авто: цена < 59 000
    "mgt": 5_000,   # малогабаритный
    "sgt": 8_000,   # среднегабаритный
    "kgt": 20_000,  # крупногабаритный (is_kgt / цена > 5 млн)
}
WEIGHT_LABELS = {"low": "до 59 000", "mgt": "МГТ", "sgt": "СГТ", "kgt": "КГТ"}
```

---

## Авто-пропуск шага габаритов

Чтобы не спрашивать селлера о габаритах там, где тариф очевиден, `auto_weight_class`
возвращает класс сразу — либо `None`, если нужно уточнить МГТ vs СГТ:

```python
def auto_weight_class(sell_price: float, is_kgt: bool) -> str | None:
    if is_kgt or sell_price > HIGH_PRICE_THRESHOLD:   # крупногабарит / дорогой
        return "kgt"
    if sell_price < LOW_PRICE_THRESHOLD:              # дешёвый товар
        return "low"
    return None                                       # спросить: МГТ или СГТ?
```

Правила:
- `is_kgt` (флаг категории) **или** цена `> 5 000 000` → `kgt` (20 000 сум);
- цена `< 59 000` → `low` (4 000 сум);
- иначе → `None` → бот показывает кнопки выбора `МГТ (5000)` / `СГТ (8000)`
  (FSM-шаг `Calc.weight_class`, callback `calc:size:mgt|sgt`).

---

## Формула чистой прибыли

```python
def compute_unit_economics(*, sell_price, purchase, extra, comm_pct, weight_class,
                           tax_rate=TAX_RATE) -> CalcResult:
    logistics_fee = LOGISTICS_FEES.get(weight_class, LOGISTICS_FEES["mgt"])
    commission = sell_price * comm_pct          # comm_pct — доля: 0.1 = 10%
    tax        = sell_price * tax_rate
    net_profit = sell_price - purchase - extra - commission - logistics_fee - tax
    margin     = (net_profit / sell_price * 100.0) if sell_price else 0.0
    return CalcResult(...)
```

$$
\text{Чистая прибыль} = \text{Цена} - \text{Закупка} - \text{Расходы} - \text{Комиссия Uzum} - \text{Логистика Uzum} - \text{Налог}
$$

- **Комиссия** = `sell_price × comm_pct`, где `comm_pct` берётся из категории по
  схеме: `comm_fbo` для FBO, `comm_fbs` для FBS.
- **Налог** = `sell_price × 0.04` (4%).
- **Логистика** — фиксированный сбор по `weight_class` из `LOGISTICS_FEES`.
- **Маржинальность** = `net_profit / sell_price × 100`.

Итоговое сообщение (`_build_result_text`) показывает построчную раскладку и вердикт
`✅`/`⚠️` (по знаку `net_profit`). Схема (FBO/FBS) выбирает, какую комиссию брать:

```python
comm_pct = cat.get("comm_fbo" if scheme == "fbo" else "comm_fbs") or 0.0
```

---

## Обработка ненайденной категории

У товара `category_id` может быть `NULL` (Uzum не отдаёт categoryId — только имя
категории, которое матчится best-effort). Тогда комиссию считать не из чего.

Запуск калькулятора из карточки товара (`prod:calc:<sku>` → `calc_from_product`)
блокирует расчёт **только если не задана цена**. Если `category_id` пуст — бот
переводит пользователя в поиск категории (FSM `Calc.query`), сохранив в state
контекст товара (`product_sku`, `product_sell`, `product_fbo`, `product_fbs`,
`product_purchase`). После выбора категории `calc_pick_category` ветвится по
`product_sku` и **привязывает категорию к товару навсегда** (UPDATE):

```python
def _link_category(telegram_id, sku_id, category_id) -> None:
    """Навсегда привязать выбранную категорию к товару (UPDATE user_products)."""
    with session_scope() as session:
        product = get_user_product(session, telegram_id, sku_id)
        if product is not None:
            product.category_id = category_id   # сохранится при commit session_scope
```

Дальше: если `purchase_price` уже известна → сразу `_build_result_text`, иначе
спрашивается только закупка (`Calc.product_purchase`), и введённое значение
сохраняется обратно в товар через `set_product_purchase_price`. Так каждый расчёт
из карточки **обогащает** товар: один раз указанные категория и закупка остаются.

Схема при запуске из карточки определяется автоматически по остаткам:

```python
def _auto_scheme(fbo_stock, fbs_stock) -> str:
    fbo, fbs = (fbo_stock or 0), (fbs_stock or 0)
    if fbo > 0 and fbs == 0: return "fbo"
    if fbs > 0 and fbo == 0: return "fbs"
    return "fbs"   # по умолчанию
```

> Калькулятор не работает, пока пуст справочник `uzum_categories` — это главная
> причина «ничего не найдено». Заполняется `scripts/parse_commissions.py`
> (~5043 строки), `bot.py` на старте логирует громкое предупреждение, если таблица
> пуста.

Симулятор акции переиспользует `compute_unit_economics` для сравнения текущей и
промо-цены — см. [[06_Bot_Flow_&_States#Симулятор акции]].
