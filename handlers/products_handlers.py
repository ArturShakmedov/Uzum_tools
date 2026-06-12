"""Раздел «📦 Мои товары»: список товаров с пагинацией и карточка товара."""

from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from database.connection import session_scope
from database.repository import (
    count_product_groups,
    count_user_products,
    get_active_shop,
    get_category,
    get_user_product,
    list_product_groups,
    list_products_by_root,
    needs_product_resync,
    search_user_products,
    set_product_purchase_price,
)
from handlers.common import clip_caption, smart_edit, smart_edit_photo
from keyboards.menu import BTN_PRODUCTS, main_menu_kb
from keyboards.products import (
    PAGE_SIZE,
    analytics_period_kb,
    group_card_kb,
    product_card_kb,
    products_list_kb,
    purchase_cancel_kb,
    search_prompt_kb,
    search_results_kb,
)
from services.calculator import auto_weight_class, compute_unit_economics
from services.products import sku_suffix, sync_products, update_fbs_stock_remote
from services.sales_analytics import build_model_analytics
from services.uzum_sync import SyncError
from utils.logger import get_logger

log = get_logger(__name__)
router = Router(name="products")


class ProductStates(StatesGroup):
    search_query = State()              # поиск по своим товарам
    promo_price = State()              # ввод промо-цены для симулятора акции
    waiting_for_purchase_price = State()  # ввод закупки (себестоимости) SKU
    waiting_for_fbs_stock = State()    # ввод нового остатка FBS (запись в Uzum)


def _has_active_shop(telegram_id: int) -> bool:
    with session_scope() as session:
        return get_active_shop(session, telegram_id) is not None


def _is_synthetic_root(root: str | None) -> bool:
    """Корень-фолбэк (нет артикула) — pidXXX / skuXXX: его не показываем юзеру."""
    return not root or root.startswith(("pid", "sku"))


def _group_label(title: str | None, root: str | None) -> str:
    """Подпись кнопки группы: «📦 Блузка с бантиком (ТЕМНБОР)»."""
    name = title or "Без названия"
    if _is_synthetic_root(root):
        return f"📦 {name}"
    return f"📦 {name} ({root})"


def _load_page(telegram_id: int, page: int) -> tuple[int, list[tuple[int, str]]]:
    """Страница СГРУППИРОВАННЫХ моделей: (repr_sku_id, подпись) по корню SKU."""
    with session_scope() as session:
        total = count_product_groups(session, telegram_id)
        groups = list_product_groups(
            session, telegram_id, offset=page * PAGE_SIZE, limit=PAGE_SIZE
        )
        return total, [
            (repr_sku, _group_label(title, root)) for repr_sku, title, root in groups
        ]


def _load_group_card(telegram_id: int, repr_sku_id: int) -> dict | None:
    """Карточка модели по любому её SKU: агрегат остатков + список размеров.

    Берём корень репрезентативного SKU, собираем все его модификации,
    суммируем fbo_stock/fbs_stock, считаем диапазон цен и кнопки размеров.
    """
    with session_scope() as session:
        repr_p = get_user_product(session, telegram_id, repr_sku_id)
        if repr_p is None:
            return None
        root = repr_p.sku_root
        siblings = list_products_by_root(session, telegram_id, root) if root else [repr_p]
        if not siblings:
            siblings = [repr_p]
        fbo = sum(p.fbo_stock or 0 for p in siblings)
        fbs = sum(p.fbs_stock or 0 for p in siblings)
        prices = [p.current_price for p in siblings if p.current_price]
        # Кнопка размера: суффикс артикула → иначе sku_title → иначе «•».
        sizes = [
            (p.uzum_id, sku_suffix(p.article) or p.sku_title or "•")
            for p in siblings
        ]
        return {
            "title": repr_p.title,
            "root": root,
            "repr_sku": repr_p.uzum_id,
            "count": len(siblings),
            "fbo_stock": fbo,
            "fbs_stock": fbs,
            "price_min": min(prices) if prices else None,
            "price_max": max(prices) if prices else None,
            "sizes": sizes,
        }


# Суффикс CDN Uzum: голый previewImage (https://images.uzum.uz/<id>) отдаёт 404,
# а с этим путём — реальная картинка 540px (image/webp). Полный URL строим тут.
_IMG_SIZE_SUFFIX = "/t_product_540_high.jpg"


def _photo_url(base: str | None) -> str | None:
    """Полный URL картинки из базового previewImage (или None, если ссылки нет)."""
    if not base or not isinstance(base, str):
        return None
    base = base.strip()
    if not base.startswith(("http://", "https://")):
        return None
    last = base.rstrip("/").rsplit("/", 1)[-1]
    return base if "." in last else base.rstrip("/") + _IMG_SIZE_SUFFIX


def _load_card(telegram_id: int, sku_id: int) -> dict | None:
    with session_scope() as session:
        p = get_user_product(session, telegram_id, sku_id)
        if p is None:
            return None
        # Все модификации модели — для ряда кнопок переключения размеров (edit_media).
        siblings = list_products_by_root(session, telegram_id, p.sku_root) if p.sku_root else [p]
        if not siblings:
            siblings = [p]
        sizes = [
            (s.uzum_id, sku_suffix(s.article) or s.sku_title or "•") for s in siblings
        ]
        # Комиссия категории + габарит — для расчёта чистой прибыли/ROI на карточке.
        cat = get_category(session, p.category_id) if p.category_id else None
        scheme = "fbo" if (p.fbo_stock or 0) > 0 and not (p.fbs_stock or 0) else "fbs"
        comm_pct = (cat.comm_fbo if scheme == "fbo" else cat.comm_fbs) if cat else None
        return {
            "sku_id": p.uzum_id,
            "title": p.title, "sku_title": p.sku_title, "article": p.article,
            "current_price": p.current_price,
            "fbo_stock": p.fbo_stock, "fbs_stock": p.fbs_stock,
            "purchase_price": p.purchase_price,
            "comm_pct": comm_pct,
            "is_kgt": bool(cat.is_kgt) if cat else False,
            "image_url": p.image_url,
            "sizes": sizes,
        }


def _fmt(value) -> str:
    return f"{int(value):,}".replace(",", " ") if value is not None else "—"


async def _render_page(telegram_id: int, page: int) -> tuple[str, object]:
    total, items = await asyncio.to_thread(_load_page, telegram_id, page)
    text = f"📦 <b>Ваши товары</b> — моделей: {total}. Выберите товар:"
    return text, products_list_kb(items, page, total)


def _price_text(card: dict) -> str:
    lo, hi = card.get("price_min"), card.get("price_max")
    if lo is None:
        return "—"
    return _fmt(lo) if lo == hi else f"{_fmt(lo)}–{_fmt(hi)}"


def _group_card_text(card: dict) -> str:
    head = escape(card["title"] or "Без названия")
    root = "" if _is_synthetic_root(card.get("root")) else f" · {escape(card['root'])}"
    return (
        f"📦 <b>{head}</b>{root}\n"
        f"🎨 Модификаций: {card['count']}\n"
        f"💰 Цена: {_price_text(card)} сум\n"
        f"📦 Остаток FBO (склад Uzum): {_fmt(card['fbo_stock'])}\n"
        f"🏠 Остаток FBS (свой склад): {_fmt(card['fbs_stock'])}\n\n"
        "👇 Выберите размер/модификацию:"
    )


def _trim(value: str | None, limit: int) -> str:
    """Срез длинного значения, чтобы подпись фото гарантированно влезла в 1024."""
    s = value or ""
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def _net_profit_for_card(card: dict) -> tuple[float, bool]:
    """Чистая прибыль 1 шт для карточки. (net, approx).

    Если известна комиссия категории — полная юнит-экономика через калькулятор
    (себестоимость + комиссия + логистика Uzum + налог). Иначе грубо «цена −
    закупка» (approx=True). Та же модель прибыли, что в симуляторе/калькуляторе.
    """
    price = card.get("current_price") or 0
    purchase = card.get("purchase_price") or 0
    comm = card.get("comm_pct")
    if comm is not None and price:
        econ = compute_unit_economics(
            sell_price=price, purchase=purchase, extra=0, comm_pct=comm,
            weight_class=auto_weight_class(price, card.get("is_kgt", False)) or "mgt",
        )
        return econ.net_profit, False
    return price - purchase, True


def _purchase_line(card: dict) -> str:
    """Строки «Закупка» + «ROI» (или старый вид, если закупка не задана)."""
    purchase = card.get("purchase_price")
    price = card.get("current_price")
    if not purchase or purchase <= 0 or not price:
        return f"🛒 Закупка: {_fmt(purchase)} сум" + ("" if purchase else " (не задана)")
    net, approx = _net_profit_for_card(card)
    roi = net / purchase * 100.0
    arrow = "📈" if roi >= 0 else "📉"
    approx_note = " <i>(≈ без комиссии)</i>" if approx else ""
    return (
        f"🛒 Закупка: {_fmt(purchase)} сум\n"
        f"{arrow} ROI: <b>{roi:+.0f}%</b>{approx_note}"
    )


def _card_text(card: dict) -> str:
    # Поля обрезаны (название ≤60, SKU/артикул ≤64) — итог гарантированно < 1024
    # символов (лимит подписи под фото); финальная страховка — clip_caption.
    return (
        f"📦 <b>{escape(_trim(card['title'] or 'Без названия', 60))}</b>\n"
        f"🏷 SKU: {escape(_trim(card.get('sku_title') or '—', 64))}\n"
        f"🔖 Артикул: {escape(_trim(card.get('article') or '—', 64))}\n"
        f"💰 Цена: {_fmt(card['current_price'])} сум\n"
        f"📦 Остаток FBO (склад Uzum): {_fmt(card['fbo_stock'])}\n"
        f"🏠 Остаток FBS (свой склад): {_fmt(card['fbs_stock'])}\n"
        + _purchase_line(card)
    )


def _products_state(telegram_id: int) -> tuple[int, bool]:
    """(всего товаров, нужна ли пересинхронизация для группировки)."""
    with session_scope() as session:
        total = count_user_products(session, telegram_id)
        stale = total > 0 and needs_product_resync(session, telegram_id)
        return total, stale


@router.message(F.text == BTN_PRODUCTS)
async def on_products(message: Message) -> None:
    telegram_id = message.from_user.id
    if not await asyncio.to_thread(_has_active_shop, telegram_id):
        await message.answer("Сначала подключите магазин — команда /start.")
        return

    total, stale = await asyncio.to_thread(_products_state, telegram_id)
    # total==0 — первая загрузка; stale — старая схема: нет sku_root (группировка)
    # ИЛИ ни у одного товара нет image_url (добивка фото из previewImage).
    if total == 0 or stale:
        note = "📦 Загружаю товары из Uzum…" if total == 0 else "🔄 Обновляю товары (группировка и фото)…"
        status = await message.answer(note)
        try:
            n = await asyncio.to_thread(sync_products, telegram_id)
        except SyncError as exc:
            await status.edit_text(escape(f"❌ {exc}"))
            return
        except Exception:  # noqa: BLE001
            log.exception("Products sync failed for %s", telegram_id)
            await status.edit_text("⚠️ Не удалось загрузить товары. Попробуйте позже.")
            return
        if n == 0:
            await status.edit_text("📦 В вашем магазине не найдено товаров.")
            return
        text, kb = await _render_page(telegram_id, 0)
        await status.edit_text(text, reply_markup=kb)
    else:
        text, kb = await _render_page(telegram_id, 0)
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("prod:page:"))
async def on_page(callback: CallbackQuery) -> None:
    page = max(0, int(callback.data.rsplit(":", 1)[-1]))
    text, kb = await _render_page(callback.from_user.id, page)
    await smart_edit(callback.message, text, kb)   # фото-карточка → текстовый список
    await callback.answer()


@router.callback_query(F.data == "prod:noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("prod:view:"))
async def on_view(callback: CallbackQuery, state: FSMContext) -> None:
    """Карточка МОДЕЛИ: агрегат остатков + ряд кнопок-размеров."""
    await state.clear()  # выходим из поиска/симулятора при открытии карточки
    repr_sku = int(callback.data.rsplit(":", 1)[-1])
    card = await asyncio.to_thread(_load_group_card, callback.from_user.id, repr_sku)
    if card is None:
        await callback.answer("Товар не найден", show_alert=True)
        return
    # «К размерам» из фото-карточки SKU → текстовая карточка модели (фото→текст).
    await smart_edit(
        callback.message,
        _group_card_text(card),
        group_card_kb(card["sizes"], card["repr_sku"]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prod:select_sku:"))
async def on_select_sku(callback: CallbackQuery, state: FSMContext) -> None:
    """Фото-карточка КОНКРЕТНОГО размера/SKU: фото + цена/остатки/закупка + действия.

    Отправляется как ФОТО (caption ≤1024). Переключение размера в ряду кнопок →
    smart_edit_photo делает edit_media (меняет фото и подпись в том же сообщении,
    без новых). Нет картинки / битый URL → автооткат на текст (см. smart_edit_photo).
    """
    await state.clear()
    sku_id = int(callback.data.rsplit(":", 1)[-1])
    card = await asyncio.to_thread(_load_card, callback.from_user.id, sku_id)
    if card is None:
        await callback.answer("Товар не найден", show_alert=True)
        return
    await smart_edit_photo(
        callback.message,
        _photo_url(card.get("image_url")),
        _card_text(card),
        product_card_kb(sku_id, card["sizes"]),
    )
    await callback.answer()


# --------------------------------------------------------------------------- #
#  Ввод закупки (себестоимости) SKU → ROI на карточке
# --------------------------------------------------------------------------- #
def _save_purchase(telegram_id: int, sku_id: int, price: int) -> None:
    with session_scope() as session:
        set_product_purchase_price(session, telegram_id, sku_id, price)


def _parse_purchase(text: str) -> int | None:
    """«150 000» / «150000» → 150000. Только целое положительное; иначе None."""
    cleaned = "".join(ch for ch in (text or "") if not ch.isspace())
    if not cleaned.isdecimal():
        return None
    value = int(cleaned)
    return value if value > 0 else None


async def _send_card_fresh(message: Message, telegram_id: int, sku_id: int) -> None:
    """Отправить АКТУАЛЬНУЮ карточку SKU новым сообщением (после смены закупки)."""
    card = await asyncio.to_thread(_load_card, telegram_id, sku_id)
    if card is None:
        await message.answer("Товар не найден.")
        return
    caption = clip_caption(_card_text(card))
    kb = product_card_kb(sku_id, card["sizes"])
    photo = _photo_url(card.get("image_url"))
    if photo:
        try:
            await message.answer_photo(photo, caption=caption, reply_markup=kb)
            return
        except TelegramBadRequest as exc:
            log.error("Telegram photo error: %s | url=%r", exc, photo)
    await message.answer(caption, reply_markup=kb)


@router.callback_query(F.data.startswith("prod:setbuy:"))
async def on_set_purchase_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «💰 Задать закупку» → запрос себестоимости SKU."""
    sku_id = int(callback.data.rsplit(":", 1)[-1])
    card = await asyncio.to_thread(_load_card, callback.from_user.id, sku_id)
    if card is None:
        await callback.answer("Товар не найден", show_alert=True)
        return
    await state.set_state(ProductStates.waiting_for_purchase_price)
    await state.update_data(sku_id=sku_id)
    name = card.get("title") or card.get("sku_title") or "товара"
    await callback.message.answer(
        f"💰 Введите закупочную стоимость (себестоимость) для SKU "
        f"«{escape(_trim(name, 60))}» в суммах:",
        reply_markup=purchase_cancel_kb(sku_id),
    )
    await callback.answer()


@router.message(ProductStates.waiting_for_purchase_price, F.text)
async def on_purchase_price_input(message: Message, state: FSMContext) -> None:
    """Приём себестоимости: валидация → сохранение → обновлённая карточка с ROI."""
    price = _parse_purchase(message.text)
    if price is None:
        await message.answer(
            "⚠️ Введите целое положительное число в сумах, например <b>150000</b>."
        )
        return
    data = await state.get_data()
    sku_id = data.get("sku_id")
    await state.clear()
    if sku_id is None:
        await message.answer("Сессия истекла — откройте товар заново.")
        return
    await asyncio.to_thread(_save_purchase, message.from_user.id, sku_id, price)
    await message.answer(f"✅ Закупка сохранена: <b>{_fmt(price)}</b> сум.")
    await _send_card_fresh(message, message.from_user.id, sku_id)


# --------------------------------------------------------------------------- #
#  Изменение остатка FBS (запись в Uzum API + локально)
# --------------------------------------------------------------------------- #
def _parse_stock(text: str) -> int | None:
    """«10»/«10 шт?»→ нет. Чистим пробелы; принимаем целое НЕотрицательное (0+)."""
    cleaned = "".join(ch for ch in (text or "") if not ch.isspace())
    return int(cleaned) if cleaned.isdecimal() else None  # isdecimal: только 0-9, ≥0


@router.callback_query(F.data.startswith("prod:edit_stock:"))
async def on_edit_stock_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «✏️ Изменить остаток FBS» → запрос нового количества."""
    sku_id = int(callback.data.rsplit(":", 1)[-1])
    card = await asyncio.to_thread(_load_card, callback.from_user.id, sku_id)
    if card is None:
        await callback.answer("Товар не найден", show_alert=True)
        return
    await state.set_state(ProductStates.waiting_for_fbs_stock)
    await state.update_data(sku_id=sku_id)
    await callback.message.answer(
        "✏️ Введите новый остаток для FBS (свой склад) для выбранного SKU:",
        reply_markup=purchase_cancel_kb(sku_id),   # ❌ Отмена → вернуть фото-карточку
    )
    await callback.answer()


@router.message(ProductStates.waiting_for_fbs_stock, F.text)
async def on_fbs_stock_input(message: Message, state: FSMContext) -> None:
    """Приём остатка: валидация → запись в Uzum (v2 POST) → локальное обновление."""
    amount = _parse_stock(message.text)
    if amount is None:
        await message.answer(
            "⚠️ Введите целое неотрицательное число (0 или больше), например <b>10</b>."
        )
        return
    data = await state.get_data()
    sku_id = data.get("sku_id")
    await state.clear()
    if sku_id is None:
        await message.answer("Сессия истекла — откройте товар заново.")
        return

    telegram_id = message.from_user.id
    try:
        # Сетевая запись в Uzum + локальное обновление — в воркер-потоке.
        await asyncio.to_thread(update_fbs_stock_remote, telegram_id, sku_id, amount)
    except Exception as exc:  # noqa: BLE001 — SyncError / UzumAPIError / прочее
        log.error("FBS stock update failed for %s sku=%s: %s", telegram_id, sku_id, exc)
        await message.answer(
            escape(f"❌ Не удалось обновить остаток в Uzum. Ошибка: {exc}")
        )
        return

    await message.answer("✅ Остаток успешно обновлён в Uzum!")
    await _send_card_fresh(message, telegram_id, sku_id)


# --------------------------------------------------------------------------- #
#  Дашборд аналитики продаж + ABC по модели (sku_root)
# --------------------------------------------------------------------------- #
def _root_of(telegram_id: int, repr_sku: int) -> str | None:
    with session_scope() as session:
        p = get_user_product(session, telegram_id, repr_sku)
        return p.sku_root if p else None


@router.callback_query(F.data.startswith("prod:stats:"))
async def on_analytics(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка «📊 Аналитика продаж» → выбор периода."""
    await state.clear()
    repr_sku = int(callback.data.rsplit(":", 1)[-1])
    await smart_edit(
        callback.message,
        "📊 <b>Аналитика продаж</b>\nВыберите период:",
        analytics_period_kb(repr_sku),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("prod:statp:"))
async def on_analytics_period(callback: CallbackQuery) -> None:
    """Выбран период → сводка продаж + ABC-класс модели."""
    _, _, repr_sku_s, days_s = callback.data.split(":")
    repr_sku, days = int(repr_sku_s), int(days_s)
    telegram_id = callback.from_user.id
    root = await asyncio.to_thread(_root_of, telegram_id, repr_sku)
    if root is None:
        await callback.answer("Товар не найден", show_alert=True)
        return
    await callback.answer("Считаю…")
    try:
        result = await asyncio.to_thread(build_model_analytics, telegram_id, root, days)
    except Exception:  # noqa: BLE001
        log.exception("Analytics failed for %s root=%s", telegram_id, root)
        await smart_edit(
            callback.message,
            "⚠️ Не удалось посчитать аналитику. Попробуйте позже.",
            analytics_period_kb(repr_sku),
        )
        return
    await smart_edit(callback.message, result["text"], analytics_period_kb(repr_sku))


# --------------------------------------------------------------------------- #
#  Поиск по своим товарам
# --------------------------------------------------------------------------- #
def _search(telegram_id: int, query: str) -> list[tuple[int, str]]:
    """Поиск, сгруппированный по корню SKU: (repr_sku_id, подпись группы)."""
    with session_scope() as session:
        shop = get_active_shop(session, telegram_id)
        shop_id = shop.uzum_shop_id if shop else None
        found = search_user_products(session, telegram_id, query, shop_id=shop_id, limit=10)
        return [(repr_sku, _group_label(title, root)) for repr_sku, title, root in found]


@router.callback_query(F.data == "prod:search")
async def on_search_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProductStates.search_query)
    await callback.message.edit_text(
        "🔍 Введите название товара или артикул (SKU) для поиска по вашему магазину:",
        reply_markup=search_prompt_kb(),
    )
    await callback.answer()


@router.message(ProductStates.search_query, F.text)
async def on_search_query(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Введите минимум 2 символа.", reply_markup=search_prompt_kb())
        return
    items = await asyncio.to_thread(_search, message.from_user.id, query)
    if not items:
        await message.answer(
            "🔍 Ничего не найдено. Попробуйте другое слово или артикул.",
            reply_markup=search_prompt_kb(),
        )
        return
    if len(items) == 1:  # единственное совпадение → сразу карточка модели
        await state.clear()
        repr_sku, _ = items[0]
        card = await asyncio.to_thread(_load_group_card, message.from_user.id, repr_sku)
        if card:
            await message.answer(
                _group_card_text(card),
                reply_markup=group_card_kb(card["sizes"], card["repr_sku"]),
            )
        return
    await message.answer(
        f"🔍 Найдено моделей: {len(items)}. Выберите товар:",
        reply_markup=search_results_kb(items),
    )


@router.callback_query(F.data == "prod:reset")
async def on_reset(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _render_page(callback.from_user.id, 0)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# --------------------------------------------------------------------------- #
#  Симулятор акции
# --------------------------------------------------------------------------- #
def _load_for_sim(telegram_id: int, sku_id: int) -> dict | None:
    with session_scope() as session:
        p = get_user_product(session, telegram_id, sku_id)
        if p is None:
            return None
        cat = get_category(session, p.category_id) if p.category_id else None
        scheme = "fbo" if (p.fbo_stock or 0) > 0 and not (p.fbs_stock or 0) else "fbs"
        comm_pct = None
        if cat is not None:
            comm_pct = cat.comm_fbo if scheme == "fbo" else cat.comm_fbs
        return {
            "title": p.title,
            "current_price": p.current_price,
            "purchase_price": p.purchase_price,
            "category_id": p.category_id,
            "comm_pct": comm_pct,
            "is_kgt": bool(cat.is_kgt) if cat else False,
        }


@router.callback_query(F.data.startswith("prod:sim:"))
async def on_simulator(callback: CallbackQuery, state: FSMContext) -> None:
    sku_id = int(callback.data.rsplit(":", 1)[-1])
    info = await asyncio.to_thread(_load_for_sim, callback.from_user.id, sku_id)
    if info is None or not info["current_price"]:
        await callback.answer("Нет данных по товару.", show_alert=True)
        return
    if info["comm_pct"] is None:
        await callback.answer(
            "Сначала привяжите категорию: откройте 🧮 Юнит-экономика.", show_alert=True
        )
        return
    if info["purchase_price"] is None:
        await callback.answer(
            "Сначала задайте закупку: откройте 🧮 Юнит-экономика.", show_alert=True
        )
        return

    await state.set_state(ProductStates.promo_price)
    await state.update_data(
        sku_id=sku_id, title=info["title"], current_price=info["current_price"],
        purchase=info["purchase_price"], comm_pct=info["comm_pct"], is_kgt=info["is_kgt"],
    )
    # Вызывается из фото-карточки SKU → smart_edit (фото→текст без падения).
    await smart_edit(
        callback.message,
        f"💰 Текущая цена товара: {_fmt(info['current_price'])} сум.\n"
        "Введите цену, которую предлагает Uzum для участия в акции:",
    )
    await callback.message.answer("Жду промо-цену…")
    await callback.answer()


def _parse_number(text: str) -> float | None:
    t = (text or "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        v = float(t)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


@router.message(ProductStates.promo_price, F.text)
async def on_promo_price(message: Message, state: FSMContext) -> None:
    promo = _parse_number(message.text)
    if promo is None:
        await message.answer("⚠️ Введите цену числом, например 99000.")
        return
    data = await state.get_data()
    await state.clear()

    comm_pct = data["comm_pct"]
    purchase = data["purchase"]
    is_kgt = data["is_kgt"]
    current = data["current_price"]

    def _calc(price):
        return compute_unit_economics(
            sell_price=price, purchase=purchase, extra=0, comm_pct=comm_pct,
            weight_class=auto_weight_class(price, is_kgt) or "mgt",
        )

    old, new = _calc(current), _calc(promo)

    if new.net_profit <= 0:
        verdict = "🚨 <b>Внимание! Вы работаете на грани минуса или в убыток!</b>"
    elif new.margin > 15:
        verdict = "🟢 <b>Участие выгодно!</b>"
    else:
        verdict = "🟡 <b>Маржа ниже 15% — участвуйте осторожно.</b>"

    await message.answer(
        "📉 <b>Расчёт для участия в Акции:</b>\n"
        f"📦 Товар: {escape(data.get('title') or '—')}\n"
        "──────────────\n"
        f"Было (обычная цена): {_fmt(current)} сум → Прибыль: {_fmt(old.net_profit)} сум "
        f"(маржа {old.margin:.1f}%)\n"
        f"Стало (промо-цена): {_fmt(promo)} сум → Прибыль: {_fmt(new.net_profit)} сум "
        f"(маржа {new.margin:.1f}%)\n"
        "──────────────\n"
        f"{verdict}",
        reply_markup=main_menu_kb(),
    )


__all__ = ["router"]
