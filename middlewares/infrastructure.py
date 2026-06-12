"""Инфраструктурный Щит: RBAC-контекст + бан + режим техобслуживания.

OUTER-middleware на dp.message / dp.callback_query (раньше роутеров и фильтров).
Порядок:
  1. event_from_user пуст / бот → пропускаем.
  2. Статус юзера из TTL-кэша (30 с); промах → get_user_status (to_thread).
  3. user.id ∈ ADMIN_IDS → роль переопределяется в UserRole.ROOT.
  4. data["current_role"] = role  (прокидываем в контекст aiogram для фильтров/хэндлеров).
  5. Защита 1 (бан) → стоп.
  6. Защита 2 (техработы, кэш SYSTEM_CACHE) для ролей НЕ admin/root → стоп.

TTL-кэш статусов: без него каждый апдейт = поток (asyncio.to_thread) + соединение
из пула SQLAlchemy; на тысячах юзеров дефолтный executor (min(32, cpu+4) потоков)
становится бутылочным горлышком. Роль/бан меняются редко — 30 с устаревания
допустимы, а /ban, /unban, /set_role инвалидируют запись принудительно
(invalidate_user_status) → решение админа вступает в силу мгновенно.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from config import ADMIN_IDS, SYSTEM_CACHE
from database.connection import session_scope
from database.models import UserRole
from database.repository import get_user_status
from utils.logger import get_logger

log = get_logger(__name__)

# Время жизни записи кэша статусов (сек) и потолок размера, после которого при
# очередной записи выбрасываются протухшие записи (защита RAM от роста словаря).
STATUS_CACHE_TTL = 30.0
_CACHE_PRUNE_SIZE = 10_000

# Кэш статусов общий на процесс: {user_id: {"role", "is_banned", "expires_at"}}.
# Модульный уровень — чтобы root_panel мог инвалидировать без ссылки на инстанс
# middleware. Операции с отдельными ключами dict атомарны под GIL.
_STATUS_CACHE: dict[int, dict[str, Any]] = {}


def invalidate_user_status(user_id: int) -> None:
    """Выбросить юзера из TTL-кэша статусов (после /ban, /unban, /set_role)."""
    _STATUS_CACHE.pop(user_id, None)

_BANNED_TEXT = (
    "❌ <b>Доступ заблокирован.</b> Ваш аккаунт деактивирован администратором."
)
_MAINTENANCE_TEXT = (
    "🛠 <b>Глобальное обновление системы</b>\n\n"
    "Uzum Tools переходит на API V3. Система станет доступна в течение нескольких "
    "минут — спасибо за терпение."
)
_PRIVILEGED = {UserRole.ADMIN, UserRole.ROOT}


def _load_status(telegram_id: int) -> dict[str, Any]:
    with session_scope() as session:
        return get_user_status(session, telegram_id)


async def _block(event: TelegramObject, text: str) -> None:
    """Сообщить о блокировке (Message — ответом, CallbackQuery — алертом)."""
    try:
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery):
            plain = text.replace("<b>", "").replace("</b>", "")
            await event.answer(plain, show_alert=True)
    except Exception as exc:  # noqa: BLE001 — заблокировал бота / устаревший запрос
        log.debug("shield _block не доставлен: %s", exc)


class InfrastructureShieldMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        super().__init__()
        # Алиас на общий кэш процесса (message- и callback-регистрации используют
        # один инстанс, но и при повторном инстанцировании кэш остаётся единым).
        self.user_status_cache = _STATUS_CACHE

    def _cached_status(self, user_id: int) -> dict[str, Any] | None:
        """Статус из TTL-кэша или None (нет записи / запись протухла)."""
        entry = self.user_status_cache.get(user_id)
        if entry is not None and time.time() < entry["expires_at"]:
            return entry
        return None

    def _store_status(self, user_id: int, status: dict[str, Any]) -> None:
        if len(self.user_status_cache) >= _CACHE_PRUNE_SIZE:
            now = time.time()
            for uid in [
                uid for uid, e in self.user_status_cache.items()
                if e["expires_at"] <= now
            ]:
                self.user_status_cache.pop(uid, None)
        self.user_status_cache[user_id] = {
            "role": status["role"],
            "is_banned": status["is_banned"],
            "expires_at": time.time() + STATUS_CACHE_TTL,
        }

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or getattr(user, "is_bot", False):
            return await handler(event, data)

        status = self._cached_status(user.id)
        if status is None:                      # промах кэша → один поход в БД
            status = await asyncio.to_thread(_load_status, user.id)
            self._store_status(user.id, status)
        role: UserRole = status["role"]

        # ADMIN_IDS — жёстко ROOT (анти-локаут: проходят бан/техработы).
        is_root_admin = user.id in set(ADMIN_IDS)
        if is_root_admin:
            role = UserRole.ROOT
        data["current_role"] = role

        if not is_root_admin and status["is_banned"]:
            await _block(event, _BANNED_TEXT)
            return None

        if (
            SYSTEM_CACHE.get("maintenance_mode") == "true"
            and role not in _PRIVILEGED
        ):
            await _block(event, _MAINTENANCE_TEXT)
            return None

        return await handler(event, data)


__all__ = ["InfrastructureShieldMiddleware", "invalidate_user_status", "STATUS_CACHE_TTL"]
