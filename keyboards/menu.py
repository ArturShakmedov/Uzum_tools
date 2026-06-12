"""Клавиатуры бота: главное меню (Reply) и выбор магазина (Inline)."""

from __future__ import annotations

from collections.abc import Iterable

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.i18n import gettext as _


def N_(message: str) -> str:
    """No-op маркер gettext: помечает строку для pybabel extract, не переводя её
    на месте (стандартный gettext-приём для msgid в константах модуля)."""
    return message


# Два отчёта по сроку давности невозврата (порог 30 дней).
BTN_TRANSIT = "⏳ Не вернулись (<30 дней)"
BTN_LOST = "🚨 Утерянные (30+ дней)"
BTN_FINANCE = "💰 Выплаты и баланс"
BTN_CALC = "🧮 Калькулятор"
BTN_PRODUCTS = "📦 Мои товары"
BTN_SHOPS = "🏪 Выбрать магазин"
BTN_PROFILE = "👤 Мой Кабинет"
BTN_SUPPORT = "🆘 Техподдержка"
# msgid кнопки FBS — ЕДИНЫЙ источник для рендера меню (тут, через `_()` в
# рантайме) и текстового фильтра в handlers/fbs_manager (lazy_gettext по нему же).
BTN_FBS_MSGID = N_("🚚 FBS Логистика")

# Тип отчёта по тексту кнопки — единый источник для клавиатуры и хендлера.
REPORT_BUTTONS = {BTN_TRANSIT: "transit", BTN_LOST: "lost"}


def main_menu_kb() -> ReplyKeyboardMarkup:
    """Главное меню: отчёты, финансы, товары/магазин, FBS-логистика, кабинет.

    Кнопка FBS локализуется в момент рендера (`_()` под локаль юзера из
    I18n-middleware) — остальные кнопки пока RU-константы (до полного i18n меню).
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_TRANSIT), KeyboardButton(text=BTN_LOST)],
            [KeyboardButton(text=BTN_FINANCE), KeyboardButton(text=BTN_CALC)],
            [KeyboardButton(text=BTN_PRODUCTS), KeyboardButton(text=BTN_SHOPS)],
            [KeyboardButton(text=_(BTN_FBS_MSGID))],   # 🚚 во всю ширину — сетка ровная
            [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_SUPPORT)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )


def shops_inline_kb(
    items: Iterable[tuple[int, str, bool]], action: str
) -> InlineKeyboardMarkup:
    """Inline-список магазинов.

    items — (uzum_shop_id, name, is_active). action ∈ {"connect", "switch"}.
    callback_data = "shop:<action>:<uzum_shop_id>". Активный отмечается ✅.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for shop_id, name, is_active in items:
        mark = "✅" if is_active else "🏪"
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} {name or shop_id}",
                callback_data=f"shop:{action}:{shop_id}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


__all__ = [
    "main_menu_kb",
    "shops_inline_kb",
    "BTN_TRANSIT",
    "BTN_LOST",
    "BTN_FINANCE",
    "BTN_CALC",
    "BTN_PRODUCTS",
    "BTN_SHOPS",
    "BTN_PROFILE",
    "BTN_SUPPORT",
    "BTN_FBS_MSGID",
    "REPORT_BUTTONS",
]
