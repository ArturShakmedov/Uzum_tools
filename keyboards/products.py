"""Inline-клавиатуры раздела «Мои товары»: список с пагинацией + карточка."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

PAGE_SIZE = 5


def products_list_kb(
    items: Sequence[tuple[int, str]], page: int, total: int
) -> InlineKeyboardMarkup:
    """Список товаров (sku_id, label) текущей страницы + поиск + навигация ⬅️/➡️.

    items — товары ЭТОЙ страницы; total — всего товаров (для расчёта страниц).
    """
    rows = [[InlineKeyboardButton(text="🔍 Найти товар", callback_data="prod:search")]]
    rows += [
        [InlineKeyboardButton(text=label[:60], callback_data=f"prod:view:{sku_id}")]
        for sku_id, label in items
    ]
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"prod:page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="prod:noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"prod:page:{page + 1}"))
    rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _reset_search_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="❌ Сбросить поиск / Назад", callback_data="prod:reset")]


def search_prompt_kb() -> InlineKeyboardMarkup:
    """Клавиатура под приглашением ввести поисковый запрос."""
    return InlineKeyboardMarkup(inline_keyboard=[_reset_search_row()])


def search_results_kb(items: Sequence[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Список найденных товаров (sku_id, label) + сброс поиска."""
    rows = [
        [InlineKeyboardButton(text=label[:60], callback_data=f"prod:view:{sku_id}")]
        for sku_id, label in items
    ]
    rows.append(_reset_search_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def group_card_kb(
    sizes: Sequence[tuple[int, str]], repr_sku: int, per_row: int = 4
) -> InlineKeyboardMarkup:
    """Карточка модели: ряд(ы) кнопок-размеров + аналитика + назад к списку.

    sizes — (sku_id, suffix_label); callback каждого = prod:select_sku:<sku_id>.
    repr_sku — id представителя группы (несёт sku_root в аналитику/период).
    """
    rows: list[list[InlineKeyboardButton]] = []
    buttons = [
        InlineKeyboardButton(text=f"📏 {label}", callback_data=f"prod:select_sku:{sku_id}")
        for sku_id, label in sizes
    ]
    for j in range(0, len(buttons), per_row):
        rows.append(buttons[j:j + per_row])
    rows.append([InlineKeyboardButton(
        text="📊 Аналитика продаж", callback_data=f"prod:stats:{repr_sku}")])
    rows.append([InlineKeyboardButton(text="❌ Назад к списку", callback_data="prod:page:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def analytics_period_kb(repr_sku: int) -> InlineKeyboardMarkup:
    """Выбор периода аналитики: 7 / 14 / 30 дней + назад к карточке модели."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="7 дней", callback_data=f"prod:statp:{repr_sku}:7"),
            InlineKeyboardButton(text="14 дней", callback_data=f"prod:statp:{repr_sku}:14"),
            InlineKeyboardButton(text="30 дней", callback_data=f"prod:statp:{repr_sku}:30"),
        ],
        [InlineKeyboardButton(text="❌ Назад", callback_data=f"prod:view:{repr_sku}")],
    ])


def product_card_kb(
    sku_id: int,
    sizes: Sequence[tuple[int, str]] | None = None,
    per_row: int = 4,
) -> InlineKeyboardMarkup:
    """Действия под фото-карточкой КОНКРЕТНОГО SKU (размера).

    sizes — (sku_id, label) всех модификаций модели. Клик по размеру шлёт
    prod:select_sku:<sku_id> → хендлер делает edit_media (смена фото+подписи в
    ТОМ ЖЕ сообщении, без новых). Активный размер помечен ✅.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if sizes:
        buttons = [
            InlineKeyboardButton(
                text=(f"✅ {label}" if sid == sku_id else f"📏 {label}"),
                callback_data=f"prod:select_sku:{sid}",
            )
            for sid, label in sizes
        ]
        for j in range(0, len(buttons), per_row):
            rows.append(buttons[j:j + per_row])
    rows.append([InlineKeyboardButton(text="✏️ Изменить остаток FBS", callback_data=f"prod:edit_stock:{sku_id}")])
    rows.append([InlineKeyboardButton(text="📊 Анализ конкурентов", callback_data=f"prod:analyze_competitors:{sku_id}")])
    rows.append([InlineKeyboardButton(text="💰 Задать закупку", callback_data=f"prod:setbuy:{sku_id}")])
    rows.append([InlineKeyboardButton(text="🧮 Юнит-экономика", callback_data=f"prod:calc:{sku_id}")])
    rows.append([InlineKeyboardButton(text="📉 Симулятор акции", callback_data=f"prod:sim:{sku_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ К размерам", callback_data=f"prod:view:{sku_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def purchase_cancel_kb(sku_id: int) -> InlineKeyboardMarkup:
    """Под приглашением ввести закупку: «❌ Отмена» → вернуть фото-карточку SKU."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data=f"prod:select_sku:{sku_id}")]
    ])


__all__ = [
    "products_list_kb",
    "group_card_kb",
    "analytics_period_kb",
    "product_card_kb",
    "purchase_cancel_kb",
    "search_prompt_kb",
    "search_results_kb",
    "PAGE_SIZE",
]
