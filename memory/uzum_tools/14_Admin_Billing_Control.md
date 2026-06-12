# 🧾 Платёжный аудит и ручное управление подпиской (админ)

Назад к [[00_Index]]. Модули: `database/models.py` (`PaymentLog`),
`handlers/billing.py` (логирование), `handlers/admin.py` (`/admin_payments`,
`/grant_premium`). Дополняет [[12_Subscription_&_Teams]] и [[09_Admin_Panel]].

Зачем: автоплатёж Click может «зависнуть» (инвойс выставлен, `SUCCESSFUL_PAYMENT`
не пришёл). Лог платежей это показывает, а ручное начисление спасает юзера, который
оплатил и написал в саппорт.

---

## Модель `payment_logs` (`PaymentLog`)

| Поле | Тип | Назначение |
|------|-----|-----------|
| `id` | Integer, PK | — |
| `telegram_id` | BigInt, **index** | кто платит |
| `payload` | String(64) | пакет (`premium_1_month`/…) |
| `amount` | Integer | сумма в **СУМАХ** (не в тийинах!) |
| `status` | String(16), `created` | `created` → `completed` |
| `created_at` | DateTime(tz), now | выставление инвойса |
| `updated_at` | DateTime(tz), `onupdate=now` | момент завершения |

Репозиторий: `create_payment_log(tg, payload, amount)`, `complete_payment_log(tg,
payload)` (закрывает последнюю `created` с этим payload), `list_payment_logs(limit=10)`
(LEFT JOIN `users` → имя/юзернейм).

---

## Жизненный цикл (в `handlers/billing.py`)

```
on_buy (sub:buy:<payload>):
    price_uzs = PREMIUM_PACKAGES[key]["price_uzs"]          # сумы
    _log_created(tg, payload, price_uzs)                    # ← status='created' ДО инвойса
    answer_invoice(... prices=[price_uzs*100 тийинов])

on_successful_payment (SUCCESSFUL_PAYMENT):
    _apply_payment(tg, days, plan_name, payload):
        activate_premium(...)                               # +дни, tier='premium'
        complete_payment_log(tg, payload)                   # ← created → completed
```
Лог пишется **до** инвойса: если Click зависнет, запись остаётся в `created` —
видно в `/admin_payments`. (amount в логе — в сумах; в инвойс уходит ×100 тийинов.)

---

## Админ-команды (`handlers/admin.py`, фильтр `IsAdmin` = ADMIN_IDS)

### `/admin_payments` — последние 10 транзакций
```
📋 Последние транзакции:
1. 👤 Max (@user) — 380 000 сум (3 мес) | 🟢 COMPLETED (10.06 19:15)
2. 👤 Иван (ID: 123) — 150 000 сум (1 мес) | 🟡 CREATED (10.06 19:20)
```
- Имя/юзернейм — из JOIN `users` (нет username → `ID: <tg>`; нет имени → `—`).
- 🟢 `completed` (время = `updated_at`) · 🟡 `created` (время = `created_at`).
- Период — `_PERIOD_LABEL[payload]` (1/3/6 мес, 1 год).

### `/grant_premium <telegram_id> <days>` — ручное начисление
- Парсит 2 аргумента; `days > 0`, иначе подсказка по использованию.
- `activate_premium(tg, days=days, plan_name="Premium (Вручную)")` — прибавляет дни к
  активной подписке либо от now (как при оплате).
- Админу: «✅ Юзеру {id} успешно начислено {days} дней Premium.»
- Юзеру в личку (если достижим): «✨ Администратор активировал вам Premium-доступ на
  {days} дней!» (ошибка доставки → WARNING, команда не падает).

> ⚠️ `/grant_premium` НЕ создаёт `PaymentLog` (это не оплата, а ручная компенсация);
> в аудите остаётся исходная `created`-запись зависшего платежа — это нормально.
