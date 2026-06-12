# 🤖 Состояния бота и сценарии

Назад к [[00_Index]]. Бот на `aiogram 3.x`, `parse_mode=HTML`. Роутеры — в
`handlers/`, клавиатуры — в `keyboards/`.

---

## Карта FSM-состояний

### `Calc` — `handlers/calculator_handlers.py`

Машина калькулятора юнит-экономики ([[03_Calculator_&_Economics]]):

```python
class Calc(StatesGroup):
    query    = State()  # поиск категории по слову
    category = State()  # выбор категории из найденных
    purchase = State()  # ввод закупки
    extra    = State()  # ввод доп. расходов
    sell     = State()  # ввод цены продажи
    scheme   = State()  # выбор схемы FBO/FBS
    weight_class    = State()  # габариты (МГТ/СГТ), если не определились авто
    product_purchase = State()  # запрос ТОЛЬКО закупки при запуске из карточки товара
```

### `ProductStates` — `handlers/products_handlers.py`

```python
class ProductStates(StatesGroup):
    search_query = State()  # поиск по своим товарам
    promo_price  = State()  # ввод промо-цены для симулятора акции
```

---

## Сценарий 1. Обычный калькулятор

`🧮 Калькулятор` (`BTN_CALC`) → `calc_start` (`Calc.query`).

```
[Юзер] вводит слово ("платье")
   → search_categories (AND-стемминг)  →  inline-список (calc:cat:<id>)
[Юзер] выбирает категорию  (Calc.category → calc_pick_category)
   → "Закупка?"  (Calc.purchase → число)
   → "Доп. расходы?" (Calc.extra → число)
   → "Цена продажи?" (Calc.sell → число)
   → "Схема?"  [FBO][FBS]  (Calc.scheme → calc:scheme:fbo|fbs)
   → auto_weight_class(sell, is_kgt):
        kgt/low  → сразу расчёт
        None     → [МГТ][СГТ] (Calc.weight_class → calc:size:mgt|sgt) → расчёт
   → _build_result_text: построчная раскладка + ✅/⚠️ + маржа
```

Отмена в любой момент: inline `calc:cancel` или команда `/cancel`. Каждый ввод
числа валидируется (`_parse_number`).

---

## Сценарий 2. Просмотр карточки модели

`📦 Мои товары` (`BTN_PRODUCTS`) → `on_products`.

```
on_products:
   нет товаров / sku_root IS NULL  → sync_products (самолечение, см. [[02_Product_Catalog_&_Group_Logic]])
   иначе                            → список из кэша
   → _render_page: кнопки групп "📦 {title} ({root})", пагинация по 5 (prod:page:<n>)

[Юзер] жмёт группу  → prod:view:<repr_sku>  → on_view → _load_group_card
   → агрегат: Σ FBO, Σ FBS, диапазон цен, число модификаций
   → group_card_kb:
        ряд(ы) [📏 S][📏 M][📏 L][📏 XL]   (prod:select_sku:<sku_id>)
        [📊 Аналитика продаж]              (prod:stats:<repr_sku>)
        [❌ Назад к списку]                (prod:page:0)
```

---

## Сценарий 3. Выбор размера → ФОТО-карточка SKU

Карточка конкретного SKU — **медиа-сообщение с фотографией товара** (а не текст):

```
[Юзер] жмёт [📏 M]  → prod:select_sku:<sku_id>  → on_select_sku
   → 🖼 bot.send_photo(фото SKU, caption=инфо, product_card_kb):
        ряд [✅ M][📏 S][📏 L][📏 XL]  (prod:select_sku:<sku_id>)  ← смена размера
        [🧮 Юнит-экономика]  (prod:calc:<sku_id>)
        [📉 Симулятор акции] (prod:sim:<sku_id>)
        [⬅️ К размерам]      (prod:view:<sku_id>)   ← текстовая карточка модели
```

### Медиа-рендеринг карточки SKU (UX, обновлено)

- **Фото вместо текста.** `on_select_sku` рисует карточку через
  `handlers.common.smart_edit_photo`: первый вход (из текстового списка/модели) —
  `answer_photo` + удаление старого текста; **переключение размера внутри карточки**
  (кнопки `prod:select_sku`) — `edit_media(InputMediaPhoto)`, т.е. фото и подпись
  меняются в ТОМ ЖЕ сообщении, без новых сообщений в чате.
- **Лимит подписи 1024.** У caption под фото лимит Telegram — **ровно 1024 символа**
  (а не 4096, как у текста). Шаблон карточки обрезает поля (название ≤60, SKU/артикул
  ≤64), а `clip_caption()` — финальная страховка ≤1024.
- **Источник фото.** `user_products.image_url` = `previewImage` из каталога Uzum
  (см. [[05_API_Integration]] / [[01_Database_Schema]]); полный URL строится как
  `<base>/t_product_540_high.jpg` (голый `https://images.uzum.uz/<id>` отдаёт 404).
- **Бэкфилл image_url (важно).** Колонка `image_url` появилась позже, поэтому у
  каталогов, синканных раньше нею, она NULL → карточки слепые (текст). Чтобы
  добить фото, `needs_product_resync` теперь форсит ОДИН ре-синк, если у юзера есть
  товары, но **ни у одного нет `image_url`** (проверка «нет ни одного с картинкой»,
  не «есть хоть один без» — иначе магазины с частично-безфотными SKU ресинкались бы
  вечно). После добивки хотя бы одной картинки форс снимается.
- **DEBUG-логи (диагностика «фото текстом»).** `sync_products` логирует, сколько
  карточек пришло с `previewImage` и пример URL; `save_user_products` — сколько
  строк записано с `image_url`; `smart_edit_photo` — `DEBUG PHOTO URL: <url>` перед
  отправкой и `log.error("Telegram photo error: …")` при отказе Telegram (видно, не
  блокирует ли он webp/ссылку).
- **Фолбэк.** Нет URL / битая ссылка / Telegram отверг формат → `smart_edit_photo`
  аккуратно откатывается на текстовый режим (`smart_edit`). Бот НЕ падает из-за
  картинки; у товаров без `image_url` (до ближайшего ре-синка) карточка текстовая.
- **Выходы из фото-карточки** (юнит-экономика, симулятор, «К размерам», «назад»)
  идут через `smart_edit` — он удаляет фото и шлёт текст, т.к. `editMessageText`
  не превращает фото-сообщение в текстовое.

`prod:calc:<sku>` уходит в калькулятор (`calc_from_product`): префилл категории/
схемы/цены, спрашивается лишь закупка (или поиск категории, если `category_id`
пуст) — детали в [[03_Calculator_&_Economics#Обработка ненайденной категории]].

### Ввод закупки (себестоимости) → ROI на карточке

Кнопка **`💰 Задать закупку`** (`product_card_kb`, callback `prod:setbuy:<sku>`):

```
[💰 Задать закупку] → on_set_purchase_start → ProductStates.waiting_for_purchase_price
   prompt: «💰 Введите закупочную стоимость … для SKU «<Название>» в суммах:»  [❌ Отмена]
[Юзер вводит «150 000»] → on_purchase_price_input
   • _parse_purchase: чистит ВСЕ пробелы (вкл. nbsp), требует целое >0 (isdecimal);
     иначе «⚠️ Введите целое положительное число …» (состояние сохраняется);
   • set_product_purchase_price(sku, price) → «✅ Закупка сохранена: N сум»;
   • _send_card_fresh → НОВАЯ фото-карточка SKU с обновлённой закупкой и ROI.
```

- **`❌ Отмена`** = `prod:select_sku:<sku>` → `on_select_sku` (он чистит state и
  заново рисует фото-карточку). Отдельный cancel-хэндлер не нужен.
- **FSM-состояние:** `ProductStates.waiting_for_purchase_price`; в `state.update_data`
  кладётся только `sku_id`.

#### Формула ROI в `_card_text`

`_load_card` теперь отдаёт `comm_pct` (по авто-схеме FBO/FBS) и `is_kgt`. В подписи:

- **Закупка не задана** → старый вид: `🛒 Закупка: — сум (не задана)`.
- **Закупка > 0 и есть цена** → две строки:
  ```
  🛒 Закупка: 100 000 сум
  📈 ROI: +80%
  ```
  где `net_profit` = `_net_profit_for_card` (если известна комиссия категории —
  полная юнит-экономика `compute_unit_economics`: себестоимость + комиссия +
  логистика + налог; иначе грубо «цена − закупка» с пометкой `(≈ без комиссии)`),
  **ROI = net_profit / Закупка × 100**. Знак и эмодзи: `📈 +NN%` при ROI ≥ 0,
  `📉 -NN%` при ROI < 0 (формат `{roi:+.0f}%`).

### Изменение остатка FBS (запись в Uzum API)

Кнопка **`✏️ Изменить остаток FBS`** (`product_card_kb`, под рядом размеров,
callback `prod:edit_stock:<sku>`):

```
[✏️ Изменить остаток FBS] → on_edit_stock_start → ProductStates.waiting_for_fbs_stock
   prompt: «✏️ Введите новый остаток для FBS (свой склад) для выбранного SKU:»  [❌ Отмена]
[Юзер вводит «25»] → on_fbs_stock_input
   • _parse_stock: чистит пробелы, принимает целое ≥0 (isdecimal; 0 допустим);
     иначе «⚠️ Введите целое неотрицательное число …»;
   • to_thread(update_fbs_stock_remote) → POST в Uzum → при успехе локальный fbs_stock;
   • «✅ Остаток успешно обновлён в Uzum!» + _send_card_fresh (новая карточка с цифрой);
   • при ошибке: log.error + «❌ Не удалось обновить остаток в Uzum. Ошибка: …».
```

**Запись идёт на `POST /v2/fbs/sku/stocks`, НЕ на v3.** По живой OpenAPI-схеме Uzum
метод POST есть только на v2; `/v3/fbs/sku/stocks` — read-only (GET). Эндпоинт записи
вынесен в `config.fbs_sku_stocks_update` (одна строка для переключения, когда Uzum
добавит POST на v3). Тело — `SkuStockUpdateApiRequestDto {skuAmountList:[{skuId,
barcode, amount}]}`; **`barcode` обязателен**, поэтому он теперь хранится в
`user_products.barcode` (маппится в `sync_products` из каталога). Существующие
каталоги без штрихкодов добиваются ре-синком — `needs_product_resync` форсит его,
если ни у одного товара нет `barcode` (как и для `image_url`).

- **Поток данных:** `update_fbs_stock_remote` (services/products) читает токен +
  `barcode` → `StocksAPI.update_fbs_stock` (под rate-limiter UzumClient) → при успехе
  `repository.update_fbs_stocks({sku_id: amount})`. Магазин определяется токеном
  (shopId в теле не передаётся).
- **Отказоустойчивость:** `SyncError` (нет магазина/штрихкода) и `UzumAPIError`
  (отказ Uzum: нет прав/SKU не найден/400) ловятся в хендлере, логируются `log.error`
  и показываются юзеру; бот не падает.

### Симулятор акции

`prod:sim:<sku>` → `ProductStates.promo_price`. Требует заданные `category_id` и
`purchase_price` (иначе алерт «откройте 🧮 Юнит-экономика»). Сравнивает
`compute_unit_economics` при текущей и промо-цене (комиссия по авто-схеме, габарит
через `auto_weight_class`). Вердикт:

| Условие | Сигнал |
|---------|--------|
| `net_profit ≤ 0` | 🚨 убыток |
| `margin > 15%` | 🟢 безопасно |
| иначе | 🟡 на грани |

---

## Сценарий 4. Открытие аналитики

Из карточки модели → `[📊 Аналитика продаж]` ([[04_Sales_Analytics_&_ABC]]):

```
prod:stats:<repr_sku>  → on_analytics
   → analytics_period_kb:  [7 дней][14 дней][30 дней]  +  [❌ Назад → prod:view:<repr_sku>]

[Юзер] выбирает период  → prod:statp:<repr_sku>:<days>  → on_analytics_period
   → callback.answer("Считаю…")
   → _root_of(repr_sku)  резолвит sku_root  (в callback — int id, не кириллица)
   → build_model_analytics(tg, root, days)  в asyncio.to_thread
   → HTML-сводка: Выручка / Продано / Возвраты / Себестоимость /
     Чистая прибыль / Маржинальность / Вклад в прибыль / ABC-класс + пояснение
```

Если у части проданных SKU нет закупки — в сводке предупреждение «чистая прибыль
приблизительная».

---

## Навигационная связность callback'ов

| callback | обработчик | результат |
|----------|-----------|-----------|
| `prod:page:<n>` | `on_page` | страница списка моделей |
| `prod:view:<repr_sku>` | `on_view` | карточка модели (агрегат + размеры) |
| `prod:select_sku:<sku_id>` | `on_select_sku` | ФОТО-карточка SKU (edit_media при смене размера) |
| `prod:stats:<repr_sku>` | `on_analytics` | выбор периода аналитики |
| `prod:statp:<repr_sku>:<days>` | `on_analytics_period` | сводка + ABC |
| `prod:setbuy:<sku>` | `on_set_purchase_start` | запрос закупки → `waiting_for_purchase_price` → ROI |
| `prod:edit_stock:<sku>` | `on_edit_stock_start` | запрос остатка FBS → `waiting_for_fbs_stock` → POST v2 в Uzum |
| `prod:calc:<sku>` | `calc_from_product` | калькулятор из товара |
| `prod:sim:<sku>` | `on_simulator` | симулятор акции |
| `prod:search` / `prod:reset` | поиск | вход/выход из `search_query` |

Тонкость HTML: при `parse_mode=HTML` любой динамический текст с литералом `<` или
данными пользователя (имена магазинов, текст исключений) оборачивается в
`html.escape()` перед отправкой — иначе Telegram падает на разборе сущностей.
Текст reply-кнопок НЕ парсится как HTML.
