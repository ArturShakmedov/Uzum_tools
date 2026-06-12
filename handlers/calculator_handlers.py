"""Калькулятор юнит-экономики Uzum (FSM, aiogram 3.x).

Сценарий: поиск категории → выбор из inline-списка → закупка → расходы →
цена продажи → схема (FBO/FBS) → расчёт. На каждом шаге — кнопка «Отмена»,
числовые шаги валидируются.
"""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from database.connection import session_scope
from database.repository import (
    get_category,
    get_user_product,
    search_categories,
    set_product_purchase_price,
)
from handlers.common import smart_edit
from keyboards.calculator import CALC_CANCEL, cancel_kb, categories_kb, scheme_kb, size_kb
from keyboards.menu import BTN_CALC, main_menu_kb
from services.calculator import auto_weight_class, compute_unit_economics
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="calculator")

# Эмодзи-цифры для текстового списка результатов (до 5 вариантов).
_NUM_EMOJI = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")


class Calc(StatesGroup):
    query = State()
    category = State()
    purchase = State()
    extra = State()
    sell = State()
    scheme = State()
    weight_class = State()  # выбор габаритов (МГТ/СГТ), если не определился автоматически
    product_purchase = State()  # запрос только закупки при запуске из карточки товара


def _fmt(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


def _parse_number(text: str) -> float | None:
    """Распарсить положительное число (поддержка пробелов/запятой)."""
    t = (text or "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        v = float(t)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


# --------------------------------------------------------------------------- #
#  Запуск + отмена
# --------------------------------------------------------------------------- #
@router.message(F.text == BTN_CALC)
@router.message(Command("calc"))
async def calc_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Calc.query)
    await message.answer(
        "🧮 <b>Калькулятор юнит-экономики</b>\n\n"
        "Введите название товара или категорию для поиска "
        "(например: <i>платье, зарядка, крем</i>):",
        reply_markup=cancel_kb(),
    )


@router.callback_query(F.data == CALC_CANCEL)
async def calc_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("🧮 Калькулятор закрыт.")
    await callback.message.answer("Главное меню:", reply_markup=main_menu_kb())
    await callback.answer()


@router.message(Command("cancel"))
async def calc_cancel_cmd(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())


# --------------------------------------------------------------------------- #
#  Поиск и выбор категории
# --------------------------------------------------------------------------- #
def _search(query: str) -> list[tuple[int, str]]:
    with session_scope() as session:
        return [(c.id, c.display_name) for c in search_categories(session, query, limit=5)]


@router.message(Calc.query, F.text)
async def calc_query(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Введите минимум 2 символа.", reply_markup=cancel_kb())
        return
    items = await asyncio.to_thread(_search, query)  # [(id, display_name), …] до 5
    if not items:
        await message.answer(
            "🔍 Ничего не найдено. Попробуйте ввести корень слова "
            "(например: «плать» вместо «платья»).",
            reply_markup=cancel_kb(),
        )
        return

    # Нумерованный список в тексте + компактные кнопки-цифры под ним.
    lines = [f'🔍 <b>Результаты поиска по запросу «{escape(query)}»:</b>', ""]
    for i, (_cid, name) in enumerate(items):
        lines.append(f"{_NUM_EMOJI[i]} {escape(name)}")
    lines.append("")
    lines.append("Выберите номер вашего варианта ниже:")

    await state.set_state(Calc.category)
    await message.answer(
        "\n".join(lines),
        reply_markup=categories_kb([cid for cid, _ in items]),
    )


def _load_category(category_id: int) -> dict | None:
    with session_scope() as session:
        c = get_category(session, category_id)
        if c is None:
            return None
        return {
            "id": c.id,
            "display_name": c.display_name,
            "comm_fbo": c.comm_fbo,
            "comm_fbs": c.comm_fbs,
            "is_kgt": c.is_kgt,
        }


@router.callback_query(Calc.category, F.data.startswith("calc:cat:"))
async def calc_pick_category(callback: CallbackQuery, state: FSMContext) -> None:
    category_id = int(callback.data.rsplit(":", 1)[-1])
    cat = await asyncio.to_thread(_load_category, category_id)
    if cat is None:
        await callback.answer("Категория не найдена", show_alert=True)
        return

    data = await state.get_data()
    sku_id = data.get("product_sku")

    # --- Режим привязки категории к товару (запуск из карточки) ---
    if sku_id is not None:
        # Навсегда привязываем выбранную категорию к товару в БД.
        await asyncio.to_thread(_link_category, callback.from_user.id, sku_id, category_id)
        sell = data["product_sell"]
        prefill = {
            "cat": cat,
            "scheme": _auto_scheme(data.get("product_fbo"), data.get("product_fbs")),
            "sell": sell,
            "extra": 0,
            "weight_class": auto_weight_class(sell, cat["is_kgt"]) or "mgt",
            "product_sku": sku_id,
        }
        purchase = data.get("product_purchase")
        if purchase is not None:  # закупка уже есть → сразу результат
            prefill["purchase"] = purchase
            await state.clear()
            await callback.message.edit_text(_build_result_text(prefill))
            await callback.message.answer("Готово.", reply_markup=main_menu_kb())
            await callback.answer()
            return
        # иначе спрашиваем только закупку (продолжение префилл-сценария)
        await state.set_state(Calc.product_purchase)
        await state.set_data(prefill)
        await callback.message.edit_text(
            f"✅ Категория привязана: <b>{escape(cat['display_name'])}</b>\n"
            f"Цена: {_fmt(sell)} сум · схема {prefill['scheme'].upper()}\n\n"
            "🛒 Введите <b>закупочную стоимость</b> 1 шт (в сум):"
        )
        await callback.message.answer("Жду число…", reply_markup=cancel_kb())
        await callback.answer()
        return

    # --- Обычный (ручной) сценарий калькулятора ---
    await state.update_data(cat=cat)
    await state.set_state(Calc.purchase)
    await callback.message.edit_text(
        f"📦 Категория: <b>{escape(cat['display_name'])}</b>\n\n"
        "🛒 Введите <b>стоимость закупки</b> 1 шт (в сум):"
    )
    await callback.message.answer("Жду число…", reply_markup=cancel_kb())
    await callback.answer()


# --------------------------------------------------------------------------- #
#  Числовые шаги
# --------------------------------------------------------------------------- #
@router.message(Calc.purchase, F.text)
async def calc_purchase(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await message.answer("⚠️ Введите число, например 45000.", reply_markup=cancel_kb())
        return
    await state.update_data(purchase=value)
    await state.set_state(Calc.extra)
    await message.answer(
        "🚚 Введите <b>расходы на карго/доставку/упаковку/фулфилмент</b> на 1 шт (в сум):",
        reply_markup=cancel_kb(),
    )


@router.message(Calc.extra, F.text)
async def calc_extra(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await message.answer("⚠️ Введите число, например 8000.", reply_markup=cancel_kb())
        return
    await state.update_data(extra=value)
    await state.set_state(Calc.sell)
    await message.answer(
        "💰 Введите <b>планируемую цену продажи</b> на Uzum (в сум):",
        reply_markup=cancel_kb(),
    )


@router.message(Calc.sell, F.text)
async def calc_sell(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None or value <= 0:
        await message.answer("⚠️ Введите цену продажи числом, например 120000.", reply_markup=cancel_kb())
        return
    await state.update_data(sell=value)
    await state.set_state(Calc.scheme)
    await message.answer("⚙️ Выберите схему работы:", reply_markup=scheme_kb())


# --------------------------------------------------------------------------- #
#  Схема → (авто-тариф ИЛИ опрос габаритов) → расчёт
# --------------------------------------------------------------------------- #
@router.callback_query(Calc.scheme, F.data.startswith("calc:scheme:"))
async def calc_scheme(callback: CallbackQuery, state: FSMContext) -> None:
    scheme = callback.data.rsplit(":", 1)[-1]  # fbo | fbs
    await state.update_data(scheme=scheme)
    data = await state.get_data()
    cat = data.get("cat") or {}

    # Авто-тариф: КГТ/дорогой → 20 000, дешёвый → 4 000 (опрос габаритов не нужен).
    auto = auto_weight_class(data["sell"], bool(cat.get("is_kgt")))
    if auto is not None:
        await state.update_data(weight_class=auto)
        await _finish(callback, state)
        return

    # Иначе спрашиваем габариты (МГТ vs СГТ).
    await state.set_state(Calc.weight_class)
    await callback.message.edit_text(
        "📐 <b>Выберите габариты товара в упаковке:</b>", reply_markup=size_kb()
    )
    await callback.answer()


@router.callback_query(Calc.weight_class, F.data.startswith("calc:size:"))
async def calc_weight(callback: CallbackQuery, state: FSMContext) -> None:
    weight_class = callback.data.rsplit(":", 1)[-1]  # mgt | sgt
    await state.update_data(weight_class=weight_class)
    await _finish(callback, state)


async def _finish(callback: CallbackQuery, state: FSMContext) -> None:
    """Посчитать и показать итог; очистить состояние."""
    data = await state.get_data()
    await state.clear()
    await callback.message.edit_text(_build_result_text(data))
    await callback.message.answer(
        "Посчитать ещё? Нажмите 🧮 Калькулятор.", reply_markup=main_menu_kb()
    )
    await callback.answer()


def _build_result_text(data: dict) -> str:
    cat = data.get("cat") or {}
    scheme = data.get("scheme", "fbs")
    weight_class = data.get("weight_class", "mgt")
    comm_pct = cat.get("comm_fbo" if scheme == "fbo" else "comm_fbs") or 0.0

    res = compute_unit_economics(
        sell_price=data["sell"],
        purchase=data["purchase"],
        extra=data["extra"],
        comm_pct=comm_pct,
        weight_class=weight_class,
    )
    verdict = "✅" if res.net_profit > 0 else "⚠️"
    scheme_label = "FBO (склад Uzum)" if scheme == "fbo" else "FBS (свой склад)"
    return (
        "🧮 <b>Юнит-экономика</b>\n"
        f"📦 <b>Категория:</b> {escape(cat.get('display_name', '—'))}\n"
        f"⚙️ <b>Схема:</b> {scheme_label}\n"
        "──────────────\n"
        f"💰 Цена продажи: <b>{_fmt(res.sell_price)}</b> сум\n"
        f"🛒 Закупка: −{_fmt(res.purchase)} сум\n"
        f"🚚 Ваши расходы: −{_fmt(res.extra)} сум\n"
        f"🏪 Комиссия Uzum ({res.comm_pct * 100:.1f}%): −{_fmt(res.commission)} сум\n"
        f"📦 Логистический сбор Uzum ({res.logistics_label}): −{_fmt(res.logistics_fee)} сум\n"
        f"🧾 Налог (4%): −{_fmt(res.tax)} сум\n"
        "──────────────\n"
        f"{verdict} <b>Чистая прибыль: {_fmt(res.net_profit)} сум</b>\n"
        f"📈 <b>Маржинальность: {res.margin:.1f}%</b>"
    )


# --------------------------------------------------------------------------- #
#  Запуск из карточки товара: префилл категории/схемы/цены, спросить лишь закупку
# --------------------------------------------------------------------------- #
def _auto_scheme(fbo_stock, fbs_stock) -> str:
    """Авто-выбор схемы по остаткам: где есть товар, та и схема (иначе FBS)."""
    fbo, fbs = (fbo_stock or 0), (fbs_stock or 0)
    if fbo > 0 and fbs == 0:
        return "fbo"
    if fbs > 0 and fbo == 0:
        return "fbs"
    return "fbs"


def _load_product_for_calc(telegram_id: int, sku_id: int) -> dict | None:
    with session_scope() as session:
        p = get_user_product(session, telegram_id, sku_id)
        if p is None:
            return None
        cat = get_category(session, p.category_id) if p.category_id else None
        return {
            "title": p.title,
            "current_price": p.current_price,
            "fbo_stock": p.fbo_stock,
            "fbs_stock": p.fbs_stock,
            "purchase_price": p.purchase_price,
            "category": None if cat is None else {
                "id": cat.id, "display_name": cat.display_name,
                "comm_fbo": cat.comm_fbo, "comm_fbs": cat.comm_fbs, "is_kgt": cat.is_kgt,
            },
        }


@router.callback_query(F.data.startswith("prod:calc:"))
async def calc_from_product(callback: CallbackQuery, state: FSMContext) -> None:
    sku_id = int(callback.data.rsplit(":", 1)[-1])
    info = await asyncio.to_thread(_load_product_for_calc, callback.from_user.id, sku_id)
    if info is None or not info["current_price"]:
        await callback.answer(
            "Не удалось определить цену товара. Воспользуйтесь 🧮 Калькулятор вручную.",
            show_alert=True,
        )
        return

    # Категория не зарезолвилась автоматически → не блокируем, а просим выбрать
    # вручную (Вариант Б). Сохраняем контекст товара в FSM для последующей привязки.
    if info["category"] is None:
        await state.set_state(Calc.query)
        await state.set_data({
            "product_sku": sku_id,
            "product_sell": info["current_price"],
            "product_fbo": info["fbo_stock"],
            "product_fbs": info["fbs_stock"],
            "product_purchase": info["purchase_price"],
        })
        # Вызывается из фото-карточки SKU → smart_edit (фото→текст без падения).
        await smart_edit(
            callback.message,
            f"⚠️ Не удалось автоматически определить категорию для товара "
            f"«{escape(info['title'] or 'без названия')}».\n\n"
            "Пожалуйста, введите название товара или категорию вручную для привязки "
            "комиссии (например: <i>блузка, одежда</i>):",
        )
        await callback.message.answer("Жду запрос…", reply_markup=cancel_kb())
        await callback.answer()
        return

    cat = info["category"]
    sell = info["current_price"]
    data = {
        "cat": cat,
        "scheme": _auto_scheme(info["fbo_stock"], info["fbs_stock"]),
        "sell": sell,
        "extra": 0,                      # карго/упаковка в быстром расчёте = 0
        "weight_class": auto_weight_class(sell, cat["is_kgt"]) or "mgt",
        "product_sku": sku_id,
    }

    if info["purchase_price"] is not None:  # закупка уже задана → сразу расчёт
        data["purchase"] = info["purchase_price"]
        await state.clear()
        await smart_edit(callback.message, _build_result_text(data))
        await callback.message.answer("Готово.", reply_markup=main_menu_kb())
        await callback.answer()
        return

    # Закупка неизвестна → спрашиваем ТОЛЬКО её.
    await state.set_state(Calc.product_purchase)
    await state.update_data(**data)
    await smart_edit(
        callback.message,
        f"🧮 <b>{escape(cat['display_name'])}</b>\n"
        f"Цена: {_fmt(sell)} сум · схема {data['scheme'].upper()}\n\n"
        "🛒 Введите <b>закупочную стоимость</b> 1 шт (в сум):",
    )
    await callback.message.answer("Жду число…", reply_markup=cancel_kb())
    await callback.answer()


@router.message(Calc.product_purchase, F.text)
async def calc_product_purchase(message: Message, state: FSMContext) -> None:
    value = _parse_number(message.text)
    if value is None:
        await message.answer("⚠️ Введите число, например 45000.", reply_markup=cancel_kb())
        return
    await state.update_data(purchase=value)
    data = await state.get_data()
    sku_id = data.get("product_sku")
    if sku_id:  # сохраняем закупку в карточку товара на будущее
        await asyncio.to_thread(_save_purchase, message.from_user.id, sku_id, int(value))
    await state.clear()
    await message.answer(_build_result_text(data), reply_markup=main_menu_kb())


def _save_purchase(telegram_id: int, sku_id: int, purchase: int) -> None:
    with session_scope() as session:
        set_product_purchase_price(session, telegram_id, sku_id, purchase)


def _link_category(telegram_id: int, sku_id: int, category_id: int) -> None:
    """Навсегда привязать выбранную категорию к товару (UPDATE user_products)."""
    with session_scope() as session:
        product = get_user_product(session, telegram_id, sku_id)
        if product is not None:
            product.category_id = category_id


__all__ = ["router"]
