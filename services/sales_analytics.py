"""Дашборд продаж по модели (sku_root) + ABC-классификация.

Разделение ответственности:
  • repository.get_model_sales_stats / get_shop_profit_by_root — чистый SQL-агрегат;
  • classify_abc — чистая бизнес-логика ранжирования (тестируется без БД);
  • build_model_analytics — оркестрация: тянет данные, классифицирует, рендерит HTML.
"""

from __future__ import annotations

from html import escape

from database.connection import session_scope
from database.repository import (
    get_model_sales_stats,
    get_shop_profit_by_root,
    list_products_by_root,
)

# Текстовое пояснение класса для селлера.
ABC_HINT = {
    "A": "🅰️ <b>Класс A</b> — флагман. Держите остаток, не допускайте out-of-stock, "
         "осторожнее со скидками: каждая единица приносит максимум прибыли.",
    "B": "🅱️ <b>Класс B</b> — крепкий середняк. Кандидат на рост: тест акций, "
         "расширение размерной сетки, апсейл к товарам A.",
    "C": "🅲 <b>Класс C</b> — низкий вклад в прибыль. Не замораживайте в нём склад; "
         "подумайте о распродаже остатков или выводе из ассортимента.",
}


def classify_abc(target_root: str, profit_by_root: dict[str, int]) -> dict[str, object]:
    """ABC-класс модели по её вкладу в прибыль магазина (кумулятивный метод).

    Правила:
      • доля в прибыли > 20%  ИЛИ кумулятивно входит в топ-80%  → A
      • следующие до 95% кумулятивно (и доля ≥ 5%)              → B
      • остальное / доля < 5% / убыток                          → C
    Учитываются только прибыльные модели (убыточные не искажают базу 100%).
    """
    positives = {r: v for r, v in profit_by_root.items() if v > 0}
    total = sum(positives.values())
    target = profit_by_root.get(target_root, 0)

    if total <= 0 or target <= 0:
        return {"abc": "C", "share_pct": 0.0, "cumulative_pct": None}

    share = target / total * 100.0

    # Кумулятивная доля до целевой модели включительно (сортировка по убыванию).
    ordered = sorted(positives.items(), key=lambda kv: kv[1], reverse=True)
    cumulative = 0.0
    for root, profit in ordered:
        cumulative += profit / total * 100.0
        if root == target_root:
            break

    if share > 20.0 or cumulative <= 80.0:
        abc = "A"
    elif cumulative <= 95.0 and share >= 5.0:
        abc = "B"
    else:
        abc = "C"

    return {"abc": abc, "share_pct": share, "cumulative_pct": cumulative}


def _fmt(value) -> str:
    return f"{int(round(value)):,}".replace(",", " ") if value is not None else "—"


def build_model_analytics(telegram_id: int, sku_root: str, days: int = 30) -> dict[str, object]:
    """Собрать сводку модели + ABC за период. Возвращает {'title','text','abc'}."""
    with session_scope() as session:
        stats = get_model_sales_stats(session, telegram_id, sku_root, days)
        breakdown = get_shop_profit_by_root(session, telegram_id, days)
        # Заголовок модели — по любому SKU группы (через repr берём название).
        title = sku_root
        siblings = list_products_by_root(session, telegram_id, sku_root)
        if siblings and siblings[0].title:
            title = siblings[0].title

    abc = classify_abc(sku_root, breakdown)
    text = _render(title, stats, abc)
    return {"title": title, "text": text, "abc": abc["abc"]}


def _render(title: str, s: dict[str, object], abc: dict[str, object]) -> str:
    head = escape(title or "Без названия")
    period = s["days"]
    margin = s["margin_pct"]
    margin_txt = f"{margin:.1f}%" if margin is not None else "—"
    share = abc["share_pct"]
    share_txt = f"{share:.1f}%" if share else "0%"

    if s["units_sold"] == 0:
        body = (
            f"📊 <b>Аналитика: {head}</b>\n"
            f"<i>за последние {period} дн.</i>\n\n"
            "За выбранный период продаж не было. "
            "Попробуйте больший период или проверьте остатки/цену."
        )
        return body

    profit_warn = (
        ""
        if s["cost_complete"]
        else f"\n⚠️ <i>У {s['units_no_cost']} шт. не задана закупка — "
             "чистая прибыль приблизительная.</i>"
    )

    return (
        f"📊 <b>Аналитика: {head}</b>\n"
        f"<i>за последние {period} дн.</i>\n\n"
        f"💰 Выручка: <b>{_fmt(s['revenue'])}</b> сум\n"
        f"📦 Продано: <b>{s['units_sold']}</b> шт.\n"
        f"↩️ Возвраты: <b>{s['returns_qty']}</b> шт. на {_fmt(s['returns_sum'])} сум\n"
        f"🛒 Себестоимость: {_fmt(s['cogs'])} сум\n"
        f"📈 Чистая прибыль: <b>{_fmt(s['net_profit'])}</b> сум\n"
        f"🎯 Маржинальность: <b>{margin_txt}</b>\n"
        f"🏆 Вклад в прибыль магазина: <b>{share_txt}</b>\n"
        f"{profit_warn}\n\n"
        f"{ABC_HINT[abc['abc']]}"
    )


__all__ = ["classify_abc", "build_model_analytics", "ABC_HINT"]
