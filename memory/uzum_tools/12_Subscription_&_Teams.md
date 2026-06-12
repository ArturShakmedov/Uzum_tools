# 👤 Профиль, подписка и менеджеры (команды/доступ)

Назад к [[00_Index]]. Модули: `handlers/profile.py` (кабинет), `handlers/start.py`
(инвайт), `database/models.py` (`User`, `ShopManager`), `middlewares/auth.py`,
`database/repository.py`. Дополняет биллинг из [[10_Billing_&_Subscriptions]].

---

## Структура БД

### `users` (модель `User`) — добавлено

| Поле | Тип | Назначение |
|------|-----|-----------|
| `telegram_id` | BigInt, PK | пользователь |
| `subscription_tier` | String(16), `free` | логика доступа (free/premium) |
| **`plan_name`** | String(32), `Бесплатный` | **витринное** название плана для кабинета |
| `subscription_expires_at` | DateTime(tz), NULL | конец подписки = «**subscription_ends_at**» из ТЗ |
| **`username`** / **`first_name`** | String, NULL | профиль Telegram — для вывода менеджеров по имени, а не ID |
| `created_at` | DateTime(tz), now | регистрация |

> `username`/`first_name` заполняются при `/start` и приёме инвайта (там есть
> `message.from_user`) через `ensure_user(..., username=, first_name=)`.

> Поле-дубль `subscription_ends_at` НЕ заводили: его роль уже играет
> `subscription_expires_at` (его ставит биллинг `activate_premium`). Один источник
> истины. `plan_name` — новое (по умолчанию «Бесплатный»; `activate_premium` ставит
> «Premium»). Миграция для SQLite-дева — `_ADDITIVE_COLUMNS["users"]`.

### `shop_managers` (модель `ShopManager`) — новая

Связь «Один магазин — много менеджеров».

| Поле | Тип | Назначение |
|------|-----|-----------|
| `id` | PK | — |
| `shop_id` | BigInt, index, NULL | `uzum_shop_id` владельца на момент выдачи доступа |
| `owner_telegram_id` | BigInt, index | владелец |
| `manager_telegram_id` | BigInt, index | кто получил доступ |
| `created_at` | DateTime(tz), now | дата добавления |

Уникальность: `(owner_telegram_id, manager_telegram_id)` — повторный инвайт не дублирует.

---

## «👤 Мой Кабинет» (`handlers/profile.py`, открыт всем)

Кнопка `BTN_PROFILE` в главном меню → `on_profile`:

```
👤 Мой Кабинет
───────────────────
💎 План: Premium            ⏳ Осталось дней: 29       ← если is_user_premium
🆓 План: Бесплатный. Доступ ограничен.                ← если free/истёк
👥 Менеджеров с доступом: N
[💳 Продлить подписку]
[👥 Управление менеджерами] [🔗 Поделиться магазином]
```

- **Дни до конца** — `repository.subscription_days_left(user)` = `max(0, (expires−now).days)`
  (устойчиво к naive/aware datetime). Free/истёк → «Доступ ограничен».
- **💳 Продлить подписку** (`billing:choose_plan`) → меню тарифов (см. ниже).
- **👥 Управление менеджерами** (`profile:managers`) — нумерованный список с именами:
  ```
  1. 👤 Иван (@ivan_seller) — Добавлен: 10.06
  2. 👤 Пётр (ID: 7805206638) — Добавлен: 10.06
  ```
  Данные — `repository.get_detailed_managers` (**LEFT JOIN** `shop_managers` ⨝ `users`
  по `manager_telegram_id` → `username`/`first_name`/`plan_name`; None у не-/start'нувших
  → fallback «ID: …»). Удаление — компактная сетка кнопок `[❌ N]` (по 4 в ряд),
  callback `mgr:delete:<manager_id>` → `on_remove_manager`: `remove_shop_manager` +
  уведомление уволенному в личку + мягкий `edit_text` (`_show_managers`).
- **🔗 Поделиться магазином** (`profile:share`) — генерит инвайт-ссылку.

---

## 💳 Тарифная сетка Premium (UZS) — `handlers/billing.py`

Утверждённая сетка (цены/дни — `config.PREMIUM_PACKAGES`; презентация/payload —
`billing._PLAN_VIEW`). **Сумма инвойса в тийинах = цена × 100.**

| Кнопка меню          | Цена, сум | Тийины (amount) | Дней | payload            | callback                   |
| -------------------- | --------: | --------------: | ---: | ------------------ | -------------------------- |
| 📦 Premium 1 месяц   |   150 000 |  **15 000 000** |   30 | `premium_1_month`  | `sub:buy:premium_1_month`  |
| 🔥 Premium 3 месяца  |   380 000 |  **38 000 000** |   90 | `premium_3_months` | `sub:buy:premium_3_months` |
| 🚀 Premium 6 месяцев |   675 000 |  **67 500 000** |  180 | `premium_6_months` | `sub:buy:premium_6_months` |
| 🌟 Premium 1 год     | 1 170 000 | **117 000 000** |  365 | `premium_1_year`   | `sub:buy:premium_1_year`   |

**Флоу оплаты (Telegram Payments / Click):**
```
[💳 Продлить подписку] / [💎 Купить Premium] / /premium → billing:choose_plan
  → on_choose_plan: меню тарифов (_choose_plan_kb)
[выбор тарифа] sub:buy:<payload> → on_buy:
  callback.message.answer_invoice(title=<тариф>, description="Доступ к аналитике
  невозвратов, лимитам менеджеров и скорингу карточек Uzum Tools.",
  provider_token=CLICK_PROVIDER_TOKEN, currency="UZS",
  start_parameter="premium-subscription", payload=<payload>,
  prices=[LabeledPrice("Uzum Tools Premium", price_uzs*100)])
[Telegram] PreCheckoutQuery → on_pre_checkout → query.answer(ok=True)  (≤10 c)
[Telegram] SUCCESSFUL_PAYMENT → on_successful_payment:
  days,plan_name = _plan_for_payload(invoice_payload)
  activate_premium(tg, days=days, plan_name=plan_name)   # +дни к активной подписке,
      иначе от now; tier='premium'; пишет plan_name
  «🎉 Оплата прошла успешно! Ваш тариф обновлён до {plan_name}. Доступ предоставлен.»
```

- **Продление:** `activate_premium` прибавляет дни к `subscription_expires_at`, если
  она в будущем, иначе считает от `now` — повторная покупка стекуется.
- Пустой `CLICK_PROVIDER_TOKEN` → алерт «Оплата не настроена», инвойс не выставляется.
- Меню тарифов также открывает кнопка «💎 Купить Premium» из оффера
  `SubscriptionMiddleware` (`keyboards.billing.buy_premium_kb` → `billing:choose_plan`).

---

## 🎁 Автоматический Welcome-триал (7 дней Premium)

Маркетинговая воронка: при **первом** успешном подключении магазина юзер получает
7 дней полного Premium **без счёта**. Функция — `repository.activate_welcome_trial`:

```python
def activate_welcome_trial(session, telegram_id) -> bool:
    user = ensure_user(session, telegram_id)
    if user.subscription_expires_at is not None:   # 🛡️ abuse-защита
        return False
    user.subscription_tier = "premium"
    user.plan_name = "Premium (Триал)"
    user.subscription_expires_at = now + timedelta(days=7)
    return True
```

**Интеграция:** `handlers/start.py::connect_chosen_shop` — в той же транзакции, что и
`connect_shop` (атомарно), вызывает `activate_welcome_trial`. Если вернулось `True`,
к «✅ Магазин подключён» добавляется праздничный блок «🎉 Вам начислен Welcome-бонус!
🔥 Активирован полный Premium-доступ на 7 дней…».

**🛡️ Защита от abuse (триал = один раз на аккаунт, не на магазин):**
выдаётся ТОЛЬКО когда `subscription_expires_at IS NULL`. Любое заполненное значение
даты → `False`:
- **перепривязка/смена магазина** — дата уже стоит с первого раза → повторно не дают;
- **истёкший триал** — дата в прошлом, но не NULL → заново не активируется;
- **платящий юзер** — `expires` в будущем → триал не трогает оплаченную подписку.

Проверено всеми 4 кейсами. (Нюанс: сразу после выдачи `subscription_days_left`
показывает 6 — `.days` округляет вниз неполные сутки; реальный срок = ровно 7×24 ч.)

---

## Инвайт-ссылки (deep-linking)

```
🔗 Поделиться → https://t.me/{bot_username}?start=invite_{owner_telegram_id}
```
(`bot_username` = `await callback.bot.me()`.)

**Переход менеджера** обрабатывает `cmd_start` (`/start invite_<owner_id>`):
`_handle_invite` → `_accept_invite` пишет `ShopManager(owner, manager, shop_id=активный
магазин владельца)` и шлёт владельцу: «👥 Пользователь @username добавлен как менеджер
вашего магазина». Менеджеру — «✅ Вы добавлены менеджером… доступна аналитика».
Самоприглашение (`owner==manager`) и повтор — отсекаются.

---

## Права доступа к аналитике (роли)

Аналитику магазина видит **владелец ИЛИ менеджер** (запись в `shop_managers`).

- **`effective_data_owner(session, telegram_id)`** — чьи данные показывать:
  есть свой активный магазин → он сам; иначе он менеджер → `owner_telegram_id`;
  иначе → он сам. Обёртка для хэндлеров — `handlers.common.resolve_owner` (to_thread).
- **`can_view_shop_analytics(session, viewer, owner)`** = `viewer==owner` ИЛИ
  `is_shop_manager`.
- **Гейт Premium** (`SubscriptionMiddleware._has_access`): пропускает админа,
  активный Premium ИЛИ **менеджера, чей владелец на активном Premium**
  (`get_owner_for_manager` + `is_user_premium(owner)`).
- **Применение:** отчёт о невозвратах (`analytics_handlers.on_get_report`) грузит
  данные по `resolve_owner(from_user.id)` → менеджер видит аналитику владельца.
  Тот же одно-строчный паттерн (`from_user.id` → `resolve_owner`) применяется к
  остальным аналитическим экранам (финансы, «Мои товары»/дашборд продаж).

### Репозиторий — новые функции

`add_shop_manager` (идемпотентно), `list_shop_managers`, `remove_shop_manager`,
`is_shop_manager`, `get_owner_for_manager`, `effective_data_owner`,
`can_view_shop_analytics`, `subscription_days_left`; `activate_premium(... , plan_name=)`.
