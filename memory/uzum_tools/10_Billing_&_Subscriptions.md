# 💎 Биллинг и подписки (Free / Premium)

Назад к [[00_Index]]. Платежи — Telegram Payments (провайдер **Click Terminal**).
Модуль: `handlers/billing.py`, гейт — `middlewares/auth.py`, модель — `database.models.User`.

Тарифы: **Free** (только калькулятор комиссий) и **Premium** (всё остальное:
«Мои товары», аналитика, отчёты, live-уведомления, управление остатками FBS).

---

## Таблица `users` (модель `User`)

Отдельная сущность от `UserShop` (один `telegram_id` = один User, магазинов может
быть несколько).

| Поле | Тип | Назначение |
|------|-----|-----------|
| `telegram_id` | BigInteger, **PK** | пользователь Telegram |
| `subscription_tier` | String(16), default `'free'` | `'free'` / `'premium'` |
| `subscription_expires_at` | DateTime(tz), NULL | конец оплаченного периода |
| `created_at` | DateTime(tz), default `now()` | дата регистрации |

**Авто-создание:** `repository.ensure_user` (get-or-create, tier='free') вызывается
при `/start` (`handlers.start._ensure_user`) и при любом взаимодействии с premium-
фичей (внутри `SubscriptionMiddleware._has_access`). **Бэкфилл существующих:**
`connection._backfill_users()` в `init_db` вставляет `User(free)` для всех
`telegram_id` из `user_shops`, которых ещё нет в `users` (идемпотентно).

**Признак активного Premium** (`repository.is_user_premium`):
`tier=='premium'` И `subscription_expires_at > now(UTC)`. Сравнение устойчиво к
naive/aware datetime (SQLite отдаёт naive → нормализуем к UTC).

---

## Гейт доступа — `SubscriptionMiddleware`

Inner-middleware (`middlewares/auth.py`), навешен в `register_handlers` на
**premium-роутеры**: `products_handlers`, `finance_handlers`, `analytics_handlers`.
Открыты (без гейта): калькулятор, `/start`, биллинг, меню, магазины, админка.

```python
class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None: return await handler(event, data)
        if await asyncio.to_thread(_has_access, user.id):   # админ ИЛИ активный premium
            return await handler(event, data)
        await _send_denied(event)                            # 🔒 оффер Premium
        return None                                          # premium-хэндлер НЕ вызываем

def _has_access(telegram_id) -> bool:
    if telegram_id in set(ADMIN_IDS): return True            # админы — всегда
    with session_scope() as s:
        return is_user_premium(ensure_user(s, telegram_id))  # + авто-создание free
```

Free-юзеру при попытке зайти в премиум-фичу (или по команде `/premium`) показывается
вертикальный список тарифов (для Message и Callback):

> 🔒 <b>Этот функционал доступен только в Premium-тарифе!</b> … [список пакетов ниже]

**Live-уведомления** — тоже Premium: воркер фильтрует получателей через
`list_premium_telegram_ids` (`notification_worker._load_active_shops`), free-юзеров
не дёргает. Клавиатура тарифов `buy_premium_kb` живёт в `keyboards/billing.py`
(вынесена из handlers, чтобы middleware не создавал цикл импорта).

---

## Тарифная сетка `PREMIUM_PACKAGES` (config.py)

Декларативный словарь пакетов; чем длиннее — тем больше скидка (вшита в `price_uzs`):

| Ключ | Дней | Цена, сум | Скидка | Кнопка |
|------|-----:|----------:|:------:|--------|
| `1_month` | 30 | 150 000 | 0% | 💎 1 месяц — 150 000 сум |
| `3_months` | 90 | 380 000 | 15% | 💎 3 месяца — 380 000 сум 🔥 Скидка 15% |
| `6_months` | 180 | 675 000 | 25% | 💎 6 месяцев — 675 000 сум 🚀 Скидка 25% |
| `1_year` | 365 | 1 170 000 | 35% | 👑 1 год — 1 170 000 сум 🌟 Скидка 35%! |

- **Поля пакета:** `days`, `price_uzs`, `label`, `discount`. Визуал кнопок
  (эмодзи 💎/👑, 🔥/🚀/🌟) — в `keyboards/billing.py` (не в данных).
- **Callback кнопки:** `billing:buy_pkg:<key>` (ключ пакета).
- **Скидка** — справочная (в кнопке/доке); фактическая цена уже зашита в `price_uzs`
  (год 1 170 000 vs 12 × 150 000 = 1 800 000 → экономия 35%).

---

## Поток оплаты (Telegram Payments / Click)

```
[💎/👑 пакет] billing:buy_pkg:<key> → on_buy_premium
   pkg = PREMIUM_PACKAGES[key]
   bot.send_invoice(
       title=f"Premium Uzum Tools — {имя_пакета}",      # «… — 1 год»
       payload=f"premium_{key}",                         # «premium_1_year»
       provider_token=CLICK_PROVIDER_TOKEN, currency="UZS",
       prices=[LabeledPrice(pkg["label"], pkg["price_uzs"] * 100)])  # тийины = сум×100
[Telegram] PreCheckoutQuery → on_pre_checkout → query.answer(ok=True)
[Telegram] SUCCESSFUL_PAYMENT → on_successful_payment:
   days = _days_from_payload(successful_payment.invoice_payload)     # premium_<key> → pkg.days
   activate_premium(tg, days=days)   # прибавляет к текущему expires_at, если активна
   «🎉 Спасибо за покупку! Premium-доступ успешно активирован на {days} дней.»
```

- **Payload = `premium_<key>`** (`premium_1_year`, `premium_3_months`, …) — точно
  идентифицирует пакет при успешной оплате. `_days_from_payload` достаёт дни из
  `PREMIUM_PACKAGES[key]["days"]`; понимает и легаси `premium_30_days`, иначе фолбэк
  `PREMIUM_DAYS`.
- **Стекирование:** пакеты продлевают друг друга — `activate_premium` прибавляет дни
  к текущему `expires_at`, если подписка ещё активна (иначе от `now`). Проверено:
  год + месяц поверх ≈ 395 дней.
- **`config.CLICK_PROVIDER_TOKEN`** — из env (тестовый токен @BotFather/Click,
  `390549344:...`). Пусто → алерт «Оплата пока не настроена», инвойс не выставляется.
- **Команда `/premium`** (`cmd_premium`, открыта всем) — показать тарифы вне блокировки.

---

## Админ-метрика

`repository.get_admin_stats` добавляет `premium_users` = `COUNT(users WHERE
subscription_tier='premium')`; в панели [[09_Admin_Panel]] выводится строкой
`💎 Premium-подписчиков: N`.

---

## На будущее (подписки v2)

- Авто-экспирация: ночной джоб переводит истёкшие `premium`→`free` (сейчас доступ
  проверяется по `expires_at` на лету, строка остаётся `premium` до ручного/джоб-сброса
  — метрика `premium_users` считает по полю, не по сроку; при желании добавить
  `AND expires_at > now`).
- Несколько тарифов/длительностей — ✅ реализовано (PREMIUM_PACKAGES, 1/3/6 мес и год).
- Промокоды/рефералка: скидка к `prices`/бонусные дни в `activate_premium`.
