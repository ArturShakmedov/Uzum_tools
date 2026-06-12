"""Inline-клавиатуры калькулятора юнит-экономики."""

from __future__ import annotations

from collections.abc import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CALC_CANCEL = "calc:cancel"


def _cancel_row() -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="❌ Отмена", callback_data=CALC_CANCEL)]


def cancel_kb() -> InlineKeyboardMarkup:
    """Только кнопка «Отмена» (для шагов ввода чисел)."""
    return InlineKeyboardMarkup(inline_keyboard=[_cancel_row()])


def categories_kb(category_ids: Sequence[int], per_row: int = 5) -> InlineKeyboardMarkup:
    """Компактные кнопки-цифры [1][2][3]… по номеру в текстовом списке.

    На кнопке — только цифра, а в callback_data зашит исходный id категории
    (calc:cat:<id>). Ниже — «Отмена» отдельной строкой.
    """
    buttons = [
        InlineKeyboardButton(text=str(i), callback_data=f"calc:cat:{cid}")
        for i, cid in enumerate(category_ids, start=1)
    ]
    rows = [buttons[j:j + per_row] for j in range(0, len(buttons), per_row)]
    rows.append(_cancel_row())
    return InlineKeyboardMarkup(inline_keyboard=rows)


def scheme_kb() -> InlineKeyboardMarkup:
    """Выбор схемы работы: FBO / FBS."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📦 FBO (склад Uzum)", callback_data="calc:scheme:fbo"),
            InlineKeyboardButton(text="🏠 FBS (свой склад)", callback_data="calc:scheme:fbs"),
        ],
        _cancel_row(),
    ])


def size_kb() -> InlineKeyboardMarkup:
    """Выбор габаритов товара в упаковке: МГТ / СГТ."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 МГТ (малогабарит · 5 000 сум)", callback_data="calc:size:mgt")],
        [InlineKeyboardButton(text="🟡 СГТ (среднегабарит · 8 000 сум)", callback_data="calc:size:sgt")],
        _cancel_row(),
    ])


__all__ = ["cancel_kb", "categories_kb", "scheme_kb", "size_kb", "CALC_CANCEL"]
