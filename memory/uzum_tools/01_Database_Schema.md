# 🗄️ Схема данных

Назад к [[00_Index]].

База — **PostgreSQL** (прод, драйвер **`psycopg3`**, синхронный, URL
`postgresql+psycopg://…` из `config.DATABASE_URL`). Слой сессий синхронный, пул
соединений `pool_size=20 / max_overflow=10 / pool_recycle=3600 / pool_pre_ping`;
параллельную запись разных пользователей обслуживает сам Postgres (**MVCC**) —
глобального write-семафора больше нет. **SQLite оставлен только как дефолт для
локалки/CI** (без поднятого Postgres). Все ORM-модели объявлены в
`database/models.py` на стиле SQLAlchemy 2.x (`Mapped[...]` + `mapped_column`).

---

## Абстрактные миксины

Доменные сущности собираются из миксинов — это и даёт ключевое архитектурное
решение мультитенантности:

```python
class TenantMixin:
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)

class PrimaryKeyMixin:
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)   # суррогат
    uzum_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)  # id на стороне Uzum

class TimestampMixin:   # synced_at / updated_at — служебные, НЕ бизнес-даты Uzum
    ...
class RawPayloadMixin:  # raw_payload — сырой ответ API (JSONB на Postgres, см. ниже)
    ...

class MarketplaceEntity(Base, TenantMixin, PrimaryKeyMixin, TimestampMixin, RawPayloadMixin):
    """Базовый класс доменной (тенант-скоупленной) сущности."""
```

### Композитный уникальный ключ

`uzum_id` НЕ уникален глобально — он уникален только в паре с владельцем. Это
позволяет двум разным селлерам иметь записи с одинаковым `uzum_id`, не конфликтуя:

```python
def tenant_unique(table: str) -> UniqueConstraint:
    """Composite-уникальность (telegram_id, uzum_id) для доменной таблицы."""
    return UniqueConstraint("telegram_id", "uzum_id", name=f"uq_{table}_tenant_uzum")
```

Upsert (`repository._upsert`) ищет существующую запись по бизнес-ключу
`(telegram_id, uzum_id)` и обновляет её (переустанавливая `synced_at`), иначе
вставляет новую — защита от `IntegrityError` и дублей при повторном синке.

---

## Таблицы

### `user_shops` (модель `UserShop`)

Подключённые магазины пользователя. **Это замена исторической таблицы `users`** —
у одного `telegram_id` может быть несколько магазинов по одному токену, активен
ровно один (`is_active=True`).

| Поле | Тип | Назначение |
|------|-----|-----------|
| `id` | PK | суррогат |
| `telegram_id` | BigInt, index | владелец |
| `uzum_shop_id` | BigInt | id магазина в Uzum |
| `shop_name`, `username` | str | витринные данные |
| `uzum_token` | `EncryptedToken(2048)` | токен, **шифруется Fernet** прозрачно |
| `is_active` | bool | активный магазин (его данные лежат в доменных таблицах) |
| `last_sync_at` | datetime | метка кэша отчётов (30 мин) |

Уникальность: `UniqueConstraint("telegram_id", "uzum_shop_id")`. Переключение
магазина = **purge доменных данных + ре-синк** (изоляция).

#### 🔐 Шифрование токена и `ENCRYPTION_KEY` (исправление C-2)

Тип `EncryptedToken` (`utils/crypto.py`) прозрачно шифрует/дешифрует `uzum_token`
через Fernet. Поведение `decrypt_token` ужесточено — больше **никакого тихого
fallback**, маскировавшего потерю ключа:

```python
class TokenKeyMismatch(RuntimeError):
    """ENCRYPTION_KEY не соответствует зашифрованным данным в БД."""

def decrypt_token(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:                 # шифртекст есть, но ключ не тот →
        raise TokenKeyMismatch(                 # ЯВНАЯ авария, а не порча данных
            "Ключ шифрования ENCRYPTION_KEY не соответствует данным в БД") from exc
    except ValueError:                          # не Fernet-формат → legacy-plaintext
        return token
```

**Автогенерация ключа полностью вырезана** — и для бота, и для CLI. Раньше при
отсутствии `ENCRYPTION_KEY` генерировался новый ключ и дописывался в `.env`; при
рестарте контейнера без сохранённой переменной ключ менялся и делал ВСЕ токены в
БД нечитаемыми. Теперь ключ обязан быть задан в окружении заранее, иначе fail-fast
на трёх рубежах:

| Рубеж | Где | Поведение при отсутствии ключа |
|-------|-----|-------------------------------|
| Бот | `bot.py:_require_encryption_key()` (старт `main()`/`dry_run()`) | `SystemExit: CRITICAL: ENCRYPTION_KEY не задан! Запуск невозможен.` |
| CLI | `main.py:run()` → `ensure_encryption_key()` в самом начале | `RuntimeError` до выполнения любой команды |
| Крипто-ядро | `utils/crypto.ensure_encryption_key()` (зовётся из `_fernet()`) | `RuntimeError("Критическая ошибка: ENCRYPTION_KEY не обнаружен в окружении!")` |

`ensure_encryption_key()` больше **не** генерирует ключ и **не** пишет в `.env` —
только читает `os.getenv("ENCRYPTION_KEY")` и при пустом значении бросает
`RuntimeError`. Это последний рубеж: даже если какой-то скрипт обойдёт guard'ы
бота/CLI, первая же операция шифрования упадёт явно, а не на «одноразовом» ключе.

### `user_products` (модель `UserProduct` : `MarketplaceEntity`)

Карточки SKU магазина. `uzum_id = skuId`. Чистится при смене магазина.

```python
class UserProduct(MarketplaceEntity):
    shop_id: Mapped[int | None]          = mapped_column(BigInteger, index=True)
    product_id: Mapped[int | None]       = mapped_column(BigInteger, index=True)
    title: Mapped[str | None]            = mapped_column(String(512))
    sku_title: Mapped[str | None]        = mapped_column(String(512))
    current_price: Mapped[int | None]    = mapped_column(BigInteger)   # цена на маркетплейсе
    fbo_stock: Mapped[int | None]        = mapped_column()             # остаток на складе Uzum
    fbs_stock: Mapped[int | None]        = mapped_column()             # остаток на своём складе
    category_id: Mapped[int | None]      = mapped_column(BigInteger, index=True)  # → uzum_categories.id
    purchase_price: Mapped[int | None]   = mapped_column(BigInteger)   # закупка (NULL пока не задана юзером)
    article: Mapped[str | None]          = mapped_column(String(128), index=True) # полный артикул «ТЕМНБОР-L» (ключ JOIN аналитики)
    sku_root: Mapped[str | None]         = mapped_column(String(128), index=True) # корень группировки
    __table_args__ = (tenant_unique("user_products"),)   # ← (telegram_id, uzum_id)
```

#### Поля `sku_root` / `sku_suffix`

Модификации одной модели (размеры/цвета) имеют **общий префикс артикула**, меняется
лишь хвост после последнего дефиса (`ТЕМНБОР-L`, `ТЕМНБОР-M`, `ЛАВАНД-S`). Корень и
суффикс вычисляются на этапе синхронизации в `services/products.py`:

```python
def sku_root(code: str | None) -> str | None:
    """Корень артикула: всё до последнего дефиса. «ТЕМНБОР-L» → «ТЕМНБОР»."""
    if not code:
        return None
    code = code.strip()
    return code.rsplit("-", 1)[0] if "-" in code else code

def sku_suffix(code: str | None) -> str | None:
    """Суффикс (размер/цвет): хвост после последнего дефиса. «…-L» → «L»."""
    if not code or "-" not in code:
        return None
    return code.rsplit("-", 1)[1].strip() or None
```

`rsplit("-", 1)` разбивает по **последнему** дефису (важно для кодов с несколькими
дефисами, напр. `DUALLOK-САРАФАН-СИРЕН-L/XL` → корень `DUALLOK-САРАФАН-СИРЕН`).

`sku_root` **проиндексирован** (`index=True`), потому что по нему идут все
«горячие» операции: `GROUP BY sku_root` для списка моделей, агрегация остатков по
группе и фильтр аналитики `WHERE p.sku_root = :root` — индекс убирает full-scan по
сотням SKU. При синке корень **никогда не NULL**: фолбэк
`sku_root(article) or f"pid{productId}" or f"sku{skuId}"`.

Логика группировки и самолечения старых строк — в [[02_Product_Catalog_&_Group_Logic]].

### `uzum_categories` (модель `UzumCategory`)

**Глобальный** справочник комиссий (НЕ тенант-скоуплен) — заполняется
`scripts/parse_commissions.py` из Excel.

```python
class UzumCategory(Base):
    id: Mapped[int]          = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(512))            # «Одежда -> Женская -> Платья»
    search_text: Mapped[str]  = mapped_column(String(512), index=True) # для поиска LIKE (lower)
    comm_fbo: Mapped[float]   = mapped_column(Float, default=0.0)      # доли: 0.1 = 10%
    comm_fbs: Mapped[float]   = mapped_column(Float, default=0.0)
    is_kgt: Mapped[bool]      = mapped_column(Boolean, default=False)  # крупногабарит → логистика 20000
```

Используется калькулятором — см. [[03_Calculator_&_Economics]].

### `orders` (модель `Order` : `MarketplaceEntity`)

Заголовок заказа FBS.

| Поле | Назначение |
|------|-----------|
| `status` | `CREATED…COMPLETED`, `CANCELED`, `RETURNED` (index) |
| `scheme` | FBS / DBS / FBO |
| `price` | **итоговая цена всего заказа** (единственный источник выручки) |
| `invoice_number` | 12-значный номер накладной |
| `date_created`, `return_date`, `date_cancelled` | бизнес-даты |

### `order_items` (модель `OrderItem` : `MarketplaceEntity`)

Позиции заказа. **Критично для аналитики:** в реальных данных Uzum у позиций
заполнен только `sku_title`, а `sku_id`, `product_title`, `seller_price` — **NULL**,
`amount` всегда 1.

| Поле | Реальное состояние |
|------|--------------------|
| `sku_id` | ❌ всегда NULL |
| `product_title` | ❌ всегда NULL |
| `seller_price` | ❌ всегда NULL |
| `sku_title` | ✅ **полный артикул** («DUALLOK-SARAFAN-СИНИЙ-S/M») |
| `amount` | ✅ = 1 |
| `order_uzum_id` | ✅ связь с `orders.uzum_id` |

---

## Мост аналитики продаж

Поскольку `order_items.sku_id` пуст, продажу нельзя привязать к товару по id.
Единственный рабочий ключ — `order_items.sku_title`, который **на 100% совпадает**
с `user_products.article`. Отсюда канонический JOIN всей аналитики:

```sql
FROM order_items oi
JOIN orders        o ON o.uzum_id = oi.order_uzum_id AND o.telegram_id = oi.telegram_id
JOIN user_products p ON p.article = oi.sku_title     AND p.telegram_id = oi.telegram_id
```

JOIN по `article = sku_title` сразу даёт и `sku_root` (модель), и `purchase_price`
(себестоимость) проданной единицы. Полная математика — в [[04_Sales_Analytics_&_ABC]].

> **Индексы JOIN (исправление W-1):** обе колонки ключа джойна проиндексированы —
> `user_products.article` (`index=True`) и `order_items.sku_title` (`index=True`).
> До этого джойн шёл по двум неиндексированным строковым колонкам (full-scan,
> деградация при росте до 10k+ SKU).

> Выручку несёт **только** `orders.price` (на весь заказ), потому что
> `order_items.seller_price` пуст. На позицию цена делится на число позиций заказа.
> Подсчёт числа позиций вынесен в CTE `WITH lines AS (...)` (один `GROUP BY` на
> заказ) вместо коррелированного подзапроса на каждую строку (было O(n²)), а само
> деление защищено `NULLIF(l.n, 0)` от рассинхрона данных — см. [[04_Sales_Analytics_&_ABC]].

---

## Прочие доменные таблицы

`invoices` / `barcodes` (накладные FBS и штрихкоды для претензий), `returns` /
`return_items` (возвраты `/v1/return`), `sku_barcodes` (каталожный справочник
штрихкодов), `finance_snapshots` (снимок баланса/выплат, PK = `telegram_id`, свой
кэш `finance_synced_at`). Все доменные — `MarketplaceEntity` со скоупом по
`telegram_id` и `tenant_unique(...)`.

---

## Тип `raw_payload`: JSONB на PostgreSQL

Сырьё API хранится в `raw_payload` (миксин `RawPayloadMixin`). Тип объявлен через
вариант диалекта — **`JSONB` на PostgreSQL**, обычный `JSON` на SQLite-деве:

```python
from sqlalchemy.dialects.postgresql import JSONB
_JSON_PAYLOAD = JSON().with_variant(JSONB(), "postgresql")
# RawPayloadMixin:
raw_payload: Mapped[dict[str, Any] | None] = mapped_column(_JSON_PAYLOAD, nullable=True)
```

JSONB = бинарное хранение со сжатием больших JSON-ответов Uzum + возможность
строить GIN-индексы по содержимому (быстрый поиск по полям сырья).

---

## Миграции

**PostgreSQL (прод):** `Base.metadata.create_all` на чистой БД строит все таблицы
**и индексы** из моделей (включая `ix_user_products_article` /
`ix_order_items_sku_title`). Эволюцию схемы в проде ведём через **Alembic**.

**SQLite (дев/CI):** разовые хелперы в `database/connection.py` (под `_IS_SQLITE`,
на Postgres инертны) донакатывают на существующий файл:

```python
_ADDITIVE_COLUMNS = {"finance_snapshots": {"finance_synced_at": "DATETIME"},
                     "user_products": {"article": "VARCHAR(128)", "sku_root": "VARCHAR(128)"}}
_ADDITIVE_INDEXES = {"ix_user_products_article": ("user_products", "article"),
                     "ix_order_items_sku_title": ("order_items", "sku_title")}
# + _rebuild_if_legacy_schema (пересоздать схему, если у orders нет telegram_id)
```

> **SQLite-PRAGMA удалены.** Прежний event-listener `journal_mode=WAL` /
> `busy_timeout` / `synchronous=NORMAL` и пр. вырезан — он был тюнингом
> single-writer SQLite. На PostgreSQL надёжность/конкурентность даёт сама СУБД
> (MVCC + WAL Postgres), а пул соединений (`pool_size=20`, `max_overflow=10`,
> `pool_recycle=3600`, `pool_pre_ping`) настраивается в `create_engine`.
> Для SQLite-дева остался лишь `connect_args={"check_same_thread": False}`
> (доступ из воркер-потоков `asyncio.to_thread`).
