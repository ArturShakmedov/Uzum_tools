"""Расчёт дедлайнов FBS-отгрузки (регламент Uzum: 24 часа на сборку).

Чистая математика без I/O и без локализованных строк: функция возвращает
машинный статус-код (safe/urgent/critical), эмодзи и остаток времени, а
человекочитаемые подписи локализует слой хэндлеров (gettext под локаль юзера).
"""

from __future__ import annotations

import datetime as dt

# Регламент Uzum Market: жёсткие 24 часа на сборку и передачу FBS-заказа
# с момента появления заказа в ЛК. Превышение — штраф селлеру.
FBS_DEADLINE_HOURS = 24

# Пороги критичности (в часах ДО дедлайна):
#   > 12 ч  → safe (🟢) · 4–12 ч → urgent (🟡) · < 4 ч / просрочен → critical (🔴)
SAFE_THRESHOLD_HOURS = 12
URGENT_THRESHOLD_HOURS = 4

_UTC = dt.timezone.utc


def calculate_fbs_deadline(created_at: dt.datetime) -> dict:
    """Остаток времени до штрафа по FBS-заказу + статус-код опасности.

    created_at — время появления заказа в ЛК. Naive datetime трактуем как UTC
    (SQLite отдаёт naive, Postgres — aware; та же нормализация, что и в
    repository.is_user_premium).

    Возвращает dict:
      deadline     — момент дедлайна (datetime, UTC);
      seconds_left — секунд до дедлайна (отрицательное значение — просрочен);
      hours / minutes — остаток для вывода «Ч ч. М мин.» (0/0 при просрочке);
      overdue      — True, если дедлайн уже прошёл;
      level        — 'safe' | 'urgent' | 'critical' (машинный код);
      emoji        — 🟢 / 🟡 / 🔴 (статус-бар).
    """
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=_UTC)
    deadline = created_at + dt.timedelta(hours=FBS_DEADLINE_HOURS)
    time_left = deadline - dt.datetime.now(_UTC)
    seconds_left = int(time_left.total_seconds())

    hours_float = seconds_left / 3600
    if hours_float > SAFE_THRESHOLD_HOURS:
        level, emoji = "safe", "🟢"
    elif hours_float >= URGENT_THRESHOLD_HOURS:
        level, emoji = "urgent", "🟡"
    else:
        level, emoji = "critical", "🔴"

    positive = max(0, seconds_left)
    return {
        "deadline": deadline,
        "seconds_left": seconds_left,
        "hours": positive // 3600,
        "minutes": (positive % 3600) // 60,
        "overdue": seconds_left <= 0,
        "level": level,
        "emoji": emoji,
    }


__all__ = [
    "calculate_fbs_deadline",
    "FBS_DEADLINE_HOURS",
    "SAFE_THRESHOLD_HOURS",
    "URGENT_THRESHOLD_HOURS",
]
