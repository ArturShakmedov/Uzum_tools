# 🛠 Админ-панель (метрики + рассылка)

Назад к [[00_Index]]. Модуль `handlers/admin.py` (роутер `admin`), метрики —
`database/repository.get_admin_stats`. Доступ — только `config.ADMIN_IDS`.

Панель для мониторинга, управления и рассылок; спроектирована с прицелом на
будущие платные подписки (метрики → конверсия, рассылка → анонсы тарифов).

---

## Защита доступа

Фильтр `IsAdmin(BaseFilter)` навешен на ВСЕ админ-хэндлеры (и команды, и колбэки):

```python
class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return bool(user and user.id in set(ADMIN_IDS))
```

- `ADMIN_IDS` берётся из `.env` (`ADMIN_IDS=111,222`, парсится `_env_int_list`).
- Обычный юзер, набравший `/admin`, **не матчит** хэндлер → бот молчит (нет fallback).
- Колбэки рассылки (`admin:bcast_run` и пр.) тоже под `IsAdmin` — нельзя дёрнуть
  рассылку, подобрав callback_data.

---

## Главное меню `/admin` (или `/stats`)

`cmd_admin_panel` → быстрые COUNT-метрики (`get_admin_stats` в `to_thread`) + inline-меню:

```
🛠 Админ-панель
📊 Метрики системы
• 👥 Пользователей: N
• 🏪 Активных магазинов: N
• 📦 Товаров (SKU): N
• 💰 С заданной закупкой: N
[📢 Создать рассылку]  (admin:broadcast)
[🔄 Обновить метрики]  (admin:refresh)
```

`get_admin_stats` (репозиторий) — 4 быстрых `COUNT`:

```python
{
  "users":               COUNT(DISTINCT user_shops.telegram_id),
  "active_shops":        COUNT(user_shops WHERE is_active),
  "products":            COUNT(user_products),
  "users_with_purchase": COUNT(DISTINCT user_products.telegram_id WHERE purchase_price IS NOT NULL),
}
```

`admin:refresh` → `on_refresh` перерисовывает то же сообщение через
`edit_message_text` (ловит `TelegramBadRequest «message is not modified»`, если
цифры не изменились).

---

## Рассылка (FSM + фоновый цикл)

Состояния `AdminStates`: `waiting_for_broadcast_text` → `waiting_for_broadcast_confirm`.

```
[📢 Создать рассылку] admin:broadcast → waiting_for_broadcast_text
   «📢 Пришлите текст рассылки (HTML):»  [❌ Отмена admin:bcast_cancel]
[Админ присылает текст] on_broadcast_text:
   • берём message.text (СЫРОЙ HTML-тегами; html_text экранировал бы теги);
   • превью «как увидят юзеры» (send). Кривой HTML → TelegramBadRequest → просим
     исправить, остаёмся в waiting_for_broadcast_text;
   • иначе → state.update_data(broadcast_text), waiting_for_broadcast_confirm,
     «Запустить рассылку?» [🚀 Запустить admin:bcast_run][❌ Отменить admin:bcast_cancel]
[🚀 Запустить] on_broadcast_run:
   • edit «🚀 Рассылка запущена…», answer(callback);
   • asyncio.create_task(_run_broadcast(bot, admin_chat_id, text)) — НЕ блокирует
     хэндлер (ссылка на task хранится в _bg_tasks, чтобы её не собрал GC).
```

### Цикл `_run_broadcast` и защита от блокировок

```python
recipients = await asyncio.to_thread(_load_recipients)   # list_broadcast_recipients
sent = blocked = failed = 0
for telegram_id in recipients:
    try:
        await bot.send_message(telegram_id, text); sent += 1
    except TelegramForbiddenError:                        # юзер заблокировал бота
        blocked += 1
        await asyncio.to_thread(_mark_blocked, telegram_id)   # deactivate_user_shops
    except Exception as exc:                              # сетевые/прочие — не валим
        failed += 1; log.warning(...)
    await asyncio.sleep(0.05)                             # анти-флуд Telegram
# отчёт админу:
"📢 Рассылка завершена!\n✅ Успешно доставлено: X\n🚫 Заблокировали бота: Y" (+ ⚠️ Прочие ошибки: Z)
```

**Поведение при блокировке (`TelegramForbiddenError`):** `deactivate_user_shops`
снимает `is_active` со всех магазинов юзера. Дальше его **не дёргают**:
- live-воркер уведомлений (`list_all_active_shops` берёт `is_active=True`),
- будущие рассылки (`list_broadcast_recipients` тоже фильтрует `is_active=True`).

> ⚠️ **Семантика `is_active`.** В схеме это флаг «активного магазина» (см.
> [[01_Database_Schema]]), и он же используется как признак «достижим». Побочный
> эффект: если заблокировавший позже вернётся, активный магазин придётся выбрать
> заново. Получатели рассылки = уникальные `telegram_id` с `is_active=True`.

### Анти-флуд

Пауза `await asyncio.sleep(0.05)` между сообщениями (~20 msg/s — в пределах лимитов
Telegram). При росте базы можно вынести рассылку в отдельный воркер/очередь
(см. [[07_Infrastructure_&_Sizing]] Tier 3).

---

## Сервисное `/revoke`

Отдельно от админки: любой юзер может удалить свои данные и токен
(`wipe_user`, с подтверждением). Не требует `IsAdmin`.

---

## Карта callback'ов

| callback_data | хэндлер | действие |
|---------------|---------|----------|
| `/admin`, `/stats` | `cmd_admin_panel` | метрики + меню (IsAdmin) |
| `admin:refresh` | `on_refresh` | перерисовать метрики (edit_text) |
| `admin:broadcast` | `on_broadcast_start` | → `waiting_for_broadcast_text` |
| `admin:bcast_run` | `on_broadcast_run` | запуск фонового `_run_broadcast` |
| `admin:bcast_cancel` | `on_broadcast_cancel` | отмена, сброс FSM |
| `revoke:confirm` / `revoke:cancel` | `revoke_*` | удаление данных юзера |
