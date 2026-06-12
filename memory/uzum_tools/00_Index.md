# 🗺️ Uzum Tools — Карта знаний

> Мультитенантный Telegram-бот для селлеров узбекского маркетплейса **Uzum Market**.
> Синхронизация заказов/возвратов/каталога, FIFO-аналитика потерь, финансы,
> калькулятор юнит-экономики, карточки товаров с группировкой по моделям и
> дашборд продаж с ABC-анализом.

---

## Архитектурный стек

| Слой | Технология | Назначение |
|------|-----------|------------|
| **Бот / UI** | `aiogram 3.x` | Роутеры, FSM (**RedisStorage**), inline/reply-клавиатуры, `parse_mode=HTML` |
| **Доступ к данным** | `SQLAlchemy 2.0` (ORM 2.x style, `Mapped[...]`) | Декларативные модели, миксины, upsert |
| **Хранилище** | **PostgreSQL** (`psycopg3`, пул 20+10) | Прод; `raw_payload`=JSONB. SQLite — только дефолт дев/CI |
| **FSM-стейт** | **Redis** (`RedisStorage`) | Бот stateless → несколько инстансов за балансировщиком |
| **HTTP-клиент** | `httpx` + адаптивный rate-limiter | Запросы к Uzum Seller OpenAPI |
| **Отчёты** | `openpyxl` | Excel-реестры потерь |
| **Шифрование** | `cryptography` (Fernet) | Токены магазинов в БД хранятся зашифрованными |

Принципиальные решения:

- **Мультитенантность.** Все доменные таблицы скоупятся по `telegram_id`
  (`TenantMixin`), бизнес-ключ — композитный `(telegram_id, uzum_id)`, а не
  глобальный `uzum_id`. Подробнее — [[01_Database_Schema]].
- **Sync — две фазы** (C-1): сбор из сети (`fetch_everything_from_uzum`,
  параллельно у разных юзеров под per-user lock + `FETCH_SEMAPHORE`) → запись
  (`persist_to_db` под коротким `DB_WRITE_SEMAPHORE` — ~1–3 с, сеть его не держит).
  Обе — в `asyncio.to_thread`.
- **Изоляция магазинов = purge + ре-синк** при переключении активного магазина.
- **Кэш 30 минут**: отчёты по `user_shops.last_sync_at`, финансы — по отдельному
  `finance_snapshots.finance_synced_at`.

---

## Структура проекта

```
Uzum_tools/
├── bot.py                  # точка входа (polling), стартовые проверки
├── main.py                 # CLI (--user, --check-losses, --debug-returns)
├── config.py               # endpoints, DATABASE, лимиты страниц
├── api/
│   ├── client.py           # UzumClient: httpx + _RateLimiter
│   └── endpoints.py        # OrdersAPI / ProductsAPI / FinanceAPI / ...
├── database/
│   ├── models.py           # ORM-модели + миксины
│   ├── connection.py       # engine PostgreSQL (psycopg), пул 20+10, session_scope
│   └── repository.py       # маппинг dict→ORM, upsert, выборки, аналитика
├── services/
│   ├── uzum_sync.py        # run_full_sync (2 фазы), FETCH_SEMAPHORE
│   ├── products.py         # sync_products, sku_root / sku_suffix
│   ├── calculator.py       # чистая математика юнит-экономики
│   ├── sales_analytics.py  # classify_abc, build_model_analytics
│   ├── analytics.py        # generate_loss_report → xlsx
│   └── notification_worker.py  # Live-лента заказов/возвратов (Фича №3)
├── handlers/               # aiogram-роутеры (бот-логика)
├── keyboards/              # сборка inline/reply-клавиатур
├── utils/                  # analytics (FIFO), excel, crypto, logger
└── scripts/parse_commissions.py  # Excel → uzum_categories
```

---

## 🧭 Навигация по базе знаний

- [[01_Database_Schema]] — таблицы, композитные ключи, `sku_root`, мост аналитики.
- [[02_Product_Catalog_&_Group_Logic]] — группировка по моделям, самолечение
  каталога, пагинация и поиск со стеммингом.
- [[03_Calculator_&_Economics]] — авто-тариф логистики, формула прибыли,
  обработка ненайденной категории.
- [[04_Sales_Analytics_&_ABC]] — SQL-агрегация продаж и алгоритм ABC.
- [[05_API_Integration]] — авторизация, каталог, двухфазный синк, `/v3/fbs/sku/stocks`.
- [[06_Bot_Flow_&_States]] — карта FSM-состояний и пользовательские сценарии.
- [[07_Infrastructure_&_Sizing]] — capacity planning: замеры ресурсов, 3 тира
  серверов, матрица «юзеры → конфиг», мост на PostgreSQL.
- [[08_Live_Notifications]] — Фича №3: фоновый воркер Live-ленты заказов/возвратов,
  дедуп в PostgreSQL, расчёт прибыли на лету, формат уведомлений.
- [[09_Admin_Panel]] — админ-панель (ADMIN_IDS): метрики `get_admin_stats`,
  фоновая рассылка, деактивация при блокировке бота.
- [[10_Billing_&_Subscriptions]] — Free/Premium, модель `User`, SubscriptionMiddleware,
  оплата Click (Telegram Payments), активация подписки.
- [[11_Competitor_Analytics]] — анализ конкурентов из карточки: публичный парсер Uzum,
  Индекс Качества (скоринг), отчёт с рекомендациями.
- [[12_Subscription_&_Teams]] — «Мой Кабинет», план/дни подписки, менеджеры через
  инвайт-ссылки (`ShopManager`), права доступа к аналитике (владелец/менеджер).
- [[13_Support_System]] — техподдержка: тикеты `SupportTicket`, персональные
  форум-топики супергруппы, двусторонний мост «личка ↔ топик».
- [[14_Admin_Billing_Control]] — платёжный аудит `PaymentLog` (created→completed),
  админ-команды `/admin_payments` и `/grant_premium` (ручное начисление Premium).
- [[15_Enterprise_Infrastructure]] — RBAC (`UserRole`), `InfrastructureShieldMiddleware`
  (бан + техработы из `SYSTEM_CACHE`, TTL-кэш статусов 30 с + инвалидация),
  Root Core `/root` (дашборд, toggle, `/set_role`/`/ban`/`/unban`), идемпотентные
  платежи (`payment_logs.telegram_payment_charge_id` UNIQUE, `with_for_update()`).

---

## Связанные домены

Продажи связываются с товарами через [[01_Database_Schema#Мост аналитики продаж]],
группировка моделей строится на `sku_root` из [[02_Product_Catalog_&_Group_Logic]],
а дашборд [[04_Sales_Analytics_&_ABC]] переиспользует ту же группировку.
