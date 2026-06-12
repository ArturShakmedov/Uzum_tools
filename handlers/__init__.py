"""Регистрация всех роутеров бота + гейт Premium-функционала."""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.utils.i18n import SimpleI18nMiddleware

from config import i18n
from handlers import (
    admin,
    analytics_handlers,
    billing,
    calculator_handlers,
    competitors_analytics,
    fbs_manager,
    finance_handlers,
    menu,
    products_handlers,
    profile,
    root_panel,
    shops,
    start,
    support,
)
from middlewares.auth import SubscriptionMiddleware
from middlewares.infrastructure import InfrastructureShieldMiddleware

# Роутеры, закрытые подпиской (Premium-only): «Мои товары», финансы-отчёты,
# аналитика невозвратов, анализ конкурентов. Калькулятор, /start, биллинг, меню,
# магазины — открыты.
_PREMIUM_ROUTERS = (
    products_handlers.router,
    finance_handlers.router,
    analytics_handlers.router,
    competitors_analytics.router,
)


def register_handlers(dp: Dispatcher) -> None:
    """Подключить роутеры (calc-FSM раньше общего меню, чтобы не перехватывались шаги)."""
    # Инфраструктурный Щит (RBAC-контекст + бан + техобслуживание) — OUTER-middleware
    # на dp: раньше всех роутеров/фильтров, прокидывает data['current_role'].
    infra_mw = InfrastructureShieldMiddleware()
    dp.message.outer_middleware(infra_mw)
    dp.callback_query.outer_middleware(infra_mw)

    # i18n-контекст gettext для `_()` в хэндлерах (ru/uz/en). Без этой мидлвари
    # вызов `_()` падает LookupError («I18n context is not set»). Локаль — из
    # Telegram language_code юзера; при переходе на выбор языка в БД заменить
    # на кастомную I18nMiddleware (см. базу знаний, i18n).
    SimpleI18nMiddleware(i18n).setup(dp)

    # Гейт подписки — inner-middleware на premium-роутерах: срабатывает, когда
    # хэндлер фичи уже сматчился, и блокирует free-юзеров до его вызова.
    sub_mw = SubscriptionMiddleware()
    for router in _PREMIUM_ROUTERS:
        router.message.middleware(sub_mw)
        router.callback_query.middleware(sub_mw)

    dp.include_router(start.router)
    dp.include_router(admin.router)
    dp.include_router(root_panel.router)
    dp.include_router(billing.router)
    dp.include_router(profile.router)
    dp.include_router(support.router)
    dp.include_router(fbs_manager.router)
    dp.include_router(shops.router)
    dp.include_router(products_handlers.router)
    dp.include_router(competitors_analytics.router)
    dp.include_router(finance_handlers.router)
    dp.include_router(calculator_handlers.router)
    dp.include_router(menu.router)
    dp.include_router(analytics_handlers.router)


__all__ = ["register_handlers"]
