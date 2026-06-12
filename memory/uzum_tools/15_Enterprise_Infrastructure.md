# 🏛️ Enterprise Infrastructure & Root Core (RBAC, кэши, идемпотентные деньги)

Назад к [[00_Index]]. Модули: `database/models.py` (`UserRole`, `User.role/is_banned`,
`SystemSettings`, `PaymentLog`), `database/connection.py` (миграции/сид),
`database/repository.py` (`activate_premium`, `complete_payment_log`),
`config.SYSTEM_CACHE`, `middlewares/infrastructure.py` (Shield + TTL-кэш),
`handlers/root_panel.py` (`/root`), `handlers/billing.py` (идемпотентная оплата).
Дополняет [[09_Admin_Panel]] и [[14_Admin_Billing_Control]].

---

## 1. Схема данных

### RBAC: `UserRole(str, enum.Enum)`
`USER="user"` · `MANAGER="manager"` · `ADMIN="admin"` · `ROOT="root"`.
В БД persist'ится **ИМЯ** члена (`USER`/`MANAGER`/…), значение (lowercase) удобно
для команд и сравнений (`str`-enum: `UserRole.ROOT == "root"`).

### `users` — добавлено
| Поле | Тип | Default |
|------|-----|---------|
| `role` | `Enum(UserRole)` | `UserRole.USER` / `server_default="USER"` |
| `is_banned` | Boolean | `False` / `server_default=false()` |

### `system_settings` (`SystemSettings`)
| Поле | Тип |
|------|-----|
| `key` | String(64), PK |
| `value` | String(256), NOT NULL |
| `description` | String(512), nullable |
| `updated_at` | DateTime(tz), `server_default=now()`, `onupdate=now()` |

### `payment_logs` (`PaymentLog`) — НОВАЯ структура (рефакторинг 2026-06)
| Поле | Тип | Назначение |
|------|-----|-----------|
| `id` | Integer, PK | — |
| `telegram_id` | BigInteger, index | плательщик |
| `payload` | String(64) | `premium_1_month` / … |
| `amount` | Integer | сумма в **СУМАХ** (не тийинах) |
| **`telegram_payment_charge_id`** | **String(128), `unique=True`, nullable** | **ключ идемпотентности платежа** |
| `status` | String(16) | `created` → `completed` |
| `created_at` / `updated_at` | DateTime(tz) | аудит |

**Зачем unique по `telegram_payment_charge_id`:** Telegram присваивает каждому
платежу уникальный charge_id. Если апдейт `SUCCESSFUL_PAYMENT` передоставлен
(рестарт процесса до коммита offset'а polling), повторная запись упирается в
unique-констрейнт → `IntegrityError` → дни **не** начисляются второй раз.

### Контур миграций (Alembic) — внедрён 2026-06

Самописные SQLite-хаки (`_ADDITIVE_COLUMNS`, `_ADDITIVE_UNIQUE_INDEXES`,
`_migrate_roles_to_enum_names`, `_backfill_users`, `_seed_system_settings`,
`_rebuild_if_legacy_schema`) **полностью удалены** из `database/connection.py`.
Эволюция схемы и на проде (PostgreSQL), и на деве (SQLite) — только Alembic.

**Устройство контура:**
- `alembic.ini` — `script_location = alembic`, `sqlalchemy.url` пуст: env.py
  динамически делает `config.set_main_option("sqlalchemy.url", DATABASE_URL)`
  из `config.py` проекта (env `DATABASE_URL`/`UZUM_DB_URL` → фолбэк SQLite).
- `alembic/env.py` — `target_metadata = Base.metadata` (`database.models`);
  `render_item` рендерит `EncryptedToken` как `sa.String(2048)` (DDL-слою
  шифрование не нужно, миграции не тянут `utils.crypto`/ENCRYPTION_KEY);
  `render_as_batch=True` на SQLite (переносимые ALTER через batch-режим);
  `compare_type=True`.
- Базовая ревизия **`2af3e24a23f6_init_enterprise_schema`** — полная схема
  (16 таблиц): native ENUM `userrole ('USER','MANAGER','ADMIN','ROOT')`,
  JSONB для `raw_payload` на PostgreSQL, `uq_payment_logs_charge_id`,
  `is_banned DEFAULT false` (диалектный `sa.false()`, не `'0'`).

**Команды:**
```bash
alembic upgrade head                       # накат миграций на проде/деве
alembic revision --autogenerate -m "..."   # новая миграция после правки моделей
alembic stamp head                         # разово: пометить БД, созданную ДО Alembic
alembic current / alembic history          # диагностика ревизий
```

- `init_db()` в `database/connection.py` сам выполняет `upgrade head` при старте
  бота + **разовый bridge**: если таблицы есть, а `alembic_version` нет (база
  времён create_all) → `stamp head` без повторного наката DDL.
- ⚠️ Автогенерацию НОВЫХ миграций запускать против ПУСТОЙ временной БД нельзя —
  только против актуальной (autogenerate сравнивает модели с живой схемой);
  baseline генерировался против пустой БД намеренно, чтобы захватить всё.
- ⚠️ После автогенерации всегда ревьюить файл: автоген может отрендерить
  SQLite-специфику (`sa.text('0')` для Boolean, отсутствующий импорт `Text`
  у JSONB-варианта) — чинить на диалектно-нейтральные конструкции.

---

## 2. Защита денег от Race Condition (`repository.activate_premium`)

Начисление подписки — паттерн *check-then-act* (прочитать `expires_at` → прибавить
дни). Без блокировки два параллельных начисления (двойной `SUCCESSFUL_PAYMENT`,
`/grant_premium` во время оплаты) читали одинаковую базу и **теряли дни** друг
друга (lost update на Postgres READ COMMITTED).

**Решение — пессимистическая блокировка строки:**

```python
user = session.execute(
    select(User).where(User.telegram_id == telegram_id).with_for_update()
).scalar_one_or_none()
if user is None:
    user = User(telegram_id=telegram_id, subscription_tier="free")
    session.add(user)
    session.flush()
```

- `SELECT … FOR UPDATE` выстраивает конкурирующие транзакции **в очередь на
  уровне СУБД**: вторая ждёт коммита первой и читает уже обновлённый `expires_at`.
- На SQLite (дев) `FOR UPDATE` — no-op: запись там и так сериализована.
- Продление: подписка активна → дни прибавляются к `expires_at`, иначе от `now()`.

### Идемпотентный пайплайн оплаты (`handlers/billing.py`)
Порядок внутри ОДНОЙ транзакции `session_scope` принципиален:

```
SUCCESSFUL_PAYMENT
 └─ _apply_payment (to_thread):
     1. complete_payment_log(..., charge_id=..., amount=...)   # created → completed
        └─ session.flush()  ← unique по charge_id: ДУБЛЬ ПАДАЕТ ЗДЕСЬ,
                              до какого-либо начисления
     2. activate_premium(...)                                  # FOR UPDATE
 └─ except IntegrityError: log.warning + мягкий return (платёж уже учтён)
```

Если открытой `created`-записи нет (легаси-инвойс), `complete_payment_log`
создаёт `completed`-запись с фактической суммой — charge_id всё равно попадает
под unique. Откат по `IntegrityError` отменяет всю транзакцию целиком.

---

## 3. Кэши горячего пути

### 3.1 `config.SYSTEM_CACHE` (глобальные настройки)
`SYSTEM_CACHE: dict = {"maintenance_mode": "false"}` — обычный dict (операции
set/get отдельных ключей в CPython атомарны под GIL). Shield читает флаг
техработ **из памяти**, не ходя в БД на каждый апдейт.

**Прогрев при старте** — `repository.load_maintenance_cache()` (вызывается в
`bot.main()` после `init_db`). **Инвалидация:** toggle из `/root` пишет в БД
(`set_setting`) И обновляет `SYSTEM_CACHE` в том же действии.

### 3.2 TTL-кэш статусов юзеров (НОВОЕ, в `InfrastructureShieldMiddleware`)
**Проблема:** каждый message/callback = `asyncio.to_thread(get_user_status)` =
поток из дефолтного executor'а (`min(32, cpu+4)`) + соединение из пула SQLAlchemy.
На тысячах юзеров executor становился бутылочным горлышком раньше, чем БД.

**Решение:** локальный кэш процесса (модульный `_STATUS_CACHE`, алиас
`self.user_status_cache` в инстансе):

```python
{user_id: {"role": UserRole, "is_banned": bool, "expires_at": float}}
```

- **TTL = 30 секунд** (`STATUS_CACHE_TTL`): hit → статус из памяти, ноль потоков
  и ноль запросов к БД; miss/протухло → один `to_thread` и перезапись записи.
- **Прунинг:** при размере ≥ 10 000 записей (`_CACHE_PRUNE_SIZE`) перед вставкой
  выбрасываются все протухшие — словарь не растёт бесконечно.
- **Принудительная инвалидация** — `invalidate_user_status(user_id)`: вызывается
  из `/ban`, `/unban`, `/set_role` (`handlers/root_panel.py`) сразу после записи
  в БД → решение админа действует **мгновенно**, не дожидаясь истечения TTL.
- Override `ADMIN_IDS → ROOT` применяется ПОСЛЕ кэша (дёшев, в кэш не пишется).

---

## 4. Инфраструктурный Щит (`InfrastructureShieldMiddleware`)

OUTER-middleware на `dp.message`/`dp.callback_query` (регистрируется в
`register_handlers` раньше всех роутеров). На каждый апдейт:

```
user = data["event_from_user"]
if user is None or user.is_bot: → handler          # сервис/боты пропускаем
status = TTL-кэш ИЛИ to_thread(get_user_status)    # {is_banned, role}
if user.id ∈ ADMIN_IDS: role = UserRole.ROOT        # жёсткий root (анти-локаут)
data["current_role"] = role                        # ← в контекст aiogram
# Защита 1: is_banned (кроме ADMIN_IDS) → «❌ Доступ заблокирован…», СТОП
# Защита 2: SYSTEM_CACHE["maintenance_mode"]=="true" И role ∉ {ADMIN,ROOT}
#           → «🛠 Глобальное обновление системы…», СТОП
→ handler(event, data)
```

- `get_user_status` — оптимизированный `select(User.is_banned, User.role)` без
  ленивой загрузки магазинов; нет юзера → фолбэк `{False, UserRole.USER}`.
- Ответ: Message → `answer`; CallbackQuery → `answer(show_alert=True)`. Ошибки гасятся.

---

## 5. Root Control Core (`handlers/root_panel.py`, `/root`)

Доступ — фильтр **`IsRoot`** (читает `current_role` из Shield: только `UserRole.ROOT`;
ADMIN_IDS получают ROOT в middleware).

### Stateful-дашборд `/root`
`get_dashboard_stats` (to_thread): пользователи, активный Premium (`expires > now`),
магазины, тикеты `SupportTicket`; финансовый аудит из `PaymentLog completed` —
выручка за **сегодня / месяц / весь оборот** (UZS). Inline-кнопки:
- **🔄 Обновить показатели** (`root:refresh`) — `edit_text`; `TelegramBadRequest`
  («message is not modified») → «Данные не изменились».
- **🛑 Переключить тех-работы** (`root:toggle_maint`) — `_toggle_maintenance`:
  инвертирует, пишет в `SystemSettings` (`set_setting`) И в `SYSTEM_CACHE`, перерисовывает.

### Команды (строгая валидация, regex `^-?\d{1,15}$`)
| Команда | Действие | Кэш |
|---------|----------|-----|
| `/set_role <id> <user\|manager\|admin\|root>` | `set_user_role` | `invalidate_user_status(id)` |
| `/ban <id>` | `is_banned=True` | `invalidate_user_status(id)` — бан мгновенный |
| `/unban <id>` | `is_banned=False` | `invalidate_user_status(id)` |

---

## 6. Саппорт: фидбэк о недоставке

Ответ админа из топика юзеру (`handlers/support.py`, `on_admin_reply`): если
`copy_to` в личку падает (юзер заблокировал бота / чат недоступен), бот, кроме
`log.warning`, отвечает админу **прямо в топик**:
«⚠️ Не доставлено: пользователь заблокировал бота или его чат недоступен» —
админы не общаются «в пустоту». Подробнее о мосте — [[13_Support_System]].

---

## 7. Модуль FBS Логистики и Контроля Дедлайнов

Модули: `handlers/fbs_manager.py` (`/fbs`), `utils/fbs_calc.py` (математика),
ревизия Alembic **`042127317f47_add_fbs_and_acts_tables`**. Цель — предотвратить
штрафы селлеров за просрочку сборки/передачи FBS-заказов.

### Таблица `fbs_orders` (`FBSOrder`)
| Поле | Тип | Назначение |
|------|-----|-----------|
| `id` | Integer, PK | — |
| `telegram_id` | BigInteger, **FK → users.telegram_id**, index | владелец |
| `uzum_order_id` | String(64), NOT NULL | ID заказа в Uzum; **unique в паре** с telegram_id (`uq_fbs_orders_tenant_order`) |
| `sku_title` | String(256), NOT NULL | название товара |
| `order_created_at` | DateTime(tz), NOT NULL | момент появления в ЛК — **точка отсчёта дедлайна** |
| `status` | String(32), default `NEW` | активные для таймера: `NEW`, `PACKING` (`_ACTIVE_FBS_STATUSES`) |

### Таблица `shipping_acts` (`ShippingAct`)
| Поле | Тип | Назначение |
|------|-----|-----------|
| `id` | Integer, PK | — |
| `telegram_id` | BigInteger, FK → users.telegram_id, index | владелец |
| `act_number` | String(64), NOT NULL | номер акта приёма-передачи |
| `total_items` | Integer, default 0 | штук в отгрузке |
| `created_at` | DateTime(tz), `server_default=now()` | дата акта |
| `pdf_url` | String(512), nullable | ссылка на документ в ЛК Uzum |

### Математика штрафного таймера (`utils/fbs_calc.py`)
Регламент Uzum: **жёсткие 24 часа** (`FBS_DEADLINE_HOURS`) на сборку и передачу
с момента появления заказа в ЛК.

```
deadline     = order_created_at + 24h          (naive → UTC, как везде в проекте)
time_left    = deadline - now(UTC)
seconds_left = int(time_left.total_seconds())  (< 0 → overdue)
```

Статус-коды критичности (по часам до дедлайна):
| Остаток | level | Эмодзи / смысл |
|---------|-------|----------------|
| > 12 ч. | `safe` | 🟢 Безопасно |
| 4–12 ч. | `urgent` | 🟡 Срочно |
| < 4 ч. / просрочен | `critical` | 🔴 Горишь (риск штрафа) |

Функция возвращает **машинный код** (`level`/`emoji`/`hours`/`minutes`/`overdue`),
локализованные подписи рендерит хэндлер через gettext.

#### Слой синхронизации данных (Uzum API V2/V3)

Таблицы `fbs_orders`/`shipping_acts` наполняются **мостами синка**
(`repository.sync_fbs_orders` / `sync_shipping_acts`) из тех же сырых DTO, что
и основной пайплайн. Точки вызова:
- `services/uzum_sync.persist_to_db` — полный синк (заказы → fbs_orders,
  накладные `/v1/fbs/invoice` → shipping_acts: накладная = акт приёма-передачи);
- `services/notification_worker` — дельта воркера каждые 10 мин (переходы
  статусов между полными синками).

⚠️ Главный урок (баг «таймер пуст на живом боте»): хэндлеры читали таблицу, в
которую **никто не писал** — модуль без моста синка мёртв. И: Uzum API **не
отдаёт** литералы `DELIVERY`/`SHIPPING` — вкладка «В поставке» ЛК приходит как
`PENDING_DELIVERY`/`DELIVERING`. Точная карта (`UZUM_TO_FBS_STATUS`,
`repository.py`; вход нормализуется `.upper()`):

| Сырой статус Uzum API (/v2/fbs/orders) | Внутренний `fbs_orders.status` | В таймере? |
|---|---|---|
| `CREATED` | `NEW` | ✅ (📦 собери) |
| `PACKING` | `PACKING` | ✅ (📦 собери) |
| `PENDING_DELIVERY` («В поставке») | `DELIVERY` | ✅ (🚐 довези) |
| `DELIVERING` | `SHIPPING` | ✅ (🚐 довези) |
| `DELIVERED` / `ACCEPTED_AT_DP` / `DELIVERED_TO_CUSTOMER_DELIVERY_POINT` / `COMPLETED` | `SHIPPED` | ❌ терминальный |
| `CANCELED` / `PENDING_CANCELLATION` | `CANCELLED` | ❌ |
| `RETURNED` | `RETURNED` | ❌ |
| неизвестный | как есть (UPPER) | ❌ — рассинхрон виден в БД, не маскируется |

Активные статусы таймера: `_ACTIVE_FBS_STATUSES = (NEW, PACKING, DELIVERY,
SHIPPING)`; подмножество «собран, довези» — `ASSEMBLED_FBS_STATUSES = (DELIVERY,
SHIPPING)`. DBS/FBO-заказы мост пропускает (дедлайн сборки — только FBS).
Синк опрашивает ВСЕ статусы словаря `ORDER_STATUSES` (uzum_sync), не только
CREATED — заказы не теряются при смене этапа.

### UI (`/fbs`, роутер `fbs`)
- **📄 Акты отправки** (`fbs:acts`) — последние 5 `shipping_acts`: «Акт №… от
  [дата] — Количество: X шт. [Посмотреть акт→pdf_url]».
- **⏰ Таймер дедлайнов** (`fbs:timer`) — активные `fbs_orders` через
  `calculate_fbs_deadline`, сортировка по `seconds_left` ASC (горящие первыми):
  «Заказ #ID (товар) — Оставшееся время: Ч ч. М мин. 🔴/🟡/🟢»; просроченные —
  «⚠️ ПРОСРОЧЕН, риск штрафа».
- **i18n:** все строки модуля — через `_()` (gettext, ru/uz/en); каталог
  `locales/*/LC_MESSAGES/messages.po`. Контекст локали даёт
  `SimpleI18nMiddleware(i18n).setup(dp)` в `register_handlers` (локаль из
  Telegram `language_code`; без мидлвари `_()` падает LookupError).

---

## 8. Контур сетевой безопасности и Rate Limiting

### Диагностика: `utils/check_api_health.py`
Изолированный скрипт (вне архитектуры бота): ОДИН легальный `GET /v1/shops`
с боевой авторизацией из `config.py` и браузерным User-Agent (анти-бот режет
«голые» python-UA). Запуск: `.venv/bin/python utils/check_api_health.py`.

```python
# Суть вердикта (полный код — utils/check_api_health.py):
token, source = _resolve_token()        # env UZUM_API_TOKEN → БД (EncryptedToken) → без авторизации
headers = {"User-Agent": <Chrome UA>, AUTH_HEADER_NAME: token}  # голый токен, БЕЗ Bearer
try:
    resp = httpx.get(API.base_url + ENDPOINTS.shops, headers=headers, timeout=10.0)
except (ConnectTimeout, ConnectError, ...):
    "🚨 КРИТИЧЕСКИЙ БАН: IP заблокирован файрволом (Cloudflare) или API лежит"  # exit 2
# 200 → "🟢 API доступно, IP не заблокирован" (exit 0)
# 429 → "🟡 Rate Limit" + Retry-After / X-RateLimit-* (exit 3)
# 401/403 → "❌ Авторизация: токен отозван" (exit 1);  5xx → "🚨 периметр/авария" (exit 2)
```

### Замер живых лимитов (12.06.2026, /v1/shops, HTTP 200 за 478 мс)
| Заголовок | Значение | Смысл |
|---|---|---|
| `X-RateLimit-Burst-Capacity` | **2** | ёмкость bucket — всего 2 запроса залпом! |
| `X-RateLimit-Replenish-Rate` | **2** | пополнение 2 токена/сек |
| `X-RateLimit-Remaining` | 1 | остаток после одного запроса |

Вывод: лимит ЖЁСТКИЙ. `config.API.requests_per_second` снижен 3.0 → **2.0**
(старт выше Replenish-Rate ловил 429 до того, как адаптивный лимитер успевал
подстроиться).

### Регламент при 429/банах
1. **Уже встроено** (`api/client.py`): адаптивный `_RateLimiter` подстраивается
   под `X-RateLimit-Replenish-Rate` из ответов; экспоненциальный backoff на
   429/5xx/сетевых (`max_retries=4`, `backoff_factor=0.8`).
2. **429 разово** — норма: лимитер сам притормозит. Системно → проверить, не
   крутится ли несколько инстансов бота с одним токеном (общий bucket!).
3. **Бан IP (таймауты/обрывы)**: остановить воркер (`POLL_INTERVAL` ≥ 600 c),
   прогнать `check_api_health.py` с другого IP/прокси (`PROXY_URL` в .env для
   публичного парсера); вернуться с пониженной частотой.
4. **Не делать**: параллельные полные синки без `FETCH_SEMAPHORE`; запросы без
   User-Agent; ретраи без задержки (`asyncio.sleep`/backoff обязательны).
5. Перед любым «у нас бан!» — СНАЧАЛА `check_api_health.py`: один запрос
   отличает бан IP (нет ответа) от rate-limit (429) и мёртвого токена (401).

---

## Иерархия и реконсиляция

- Фактический «root» — **ADMIN_IDS** (Shield повышает их до ROOT безусловно): не
  блокируются баном/техработами, проходят `IsRoot`. Роли `admin`/`manager` в БД —
  для будущего тонкого RBAC; `admin` уже обходит техработы.
- Заменило прежний `InfrastructureMiddleware` и admin-команды `/admin_dashboard`,
  `/sys_maintenance`, `/ban`, `/unban` (turn-15) — теперь всё в Root Core. В
  `handlers/admin.py` остались `/admin_payments` и `/grant_premium` (см. [[14_Admin_Billing_Control]]).

## 🔭 След. шаг

- ✅ ~~Alembic~~ — контур внедрён (см. «Контур миграций» выше), блокер
  масштабирования снят; прод-накат: `alembic upgrade head`.
- **Перенос `maintenance_mode` и кэша банов в Redis при переходе на
  мульти-инстанс.** `SYSTEM_CACHE` и `_STATUS_CACHE` — process-local: второй
  инстанс бота не увидит ни toggle техработ, ни инвалидацию бана с первого.
  Redis уже в стеке (FSM RedisStorage) → план: флаг техработ читать из Redis
  (`GET maintenance_mode` с локальным кэшем 5–10 с), инвалидацию статусов
  рассылать через Redis Pub/Sub (канал `system_events`), БД остаётся
  источником истины для холодного старта.
- После перевода всех окружений на Alembic — удалить stamp-bridge из `init_db()`.
