"""Калькулятор юнит-экономики Uzum (чистая математика, без aiogram)."""

from __future__ import annotations

from dataclasses import dataclass

# Пороги цены для авто-тарифа логистики.
LOW_PRICE_THRESHOLD = 59_000
HIGH_PRICE_THRESHOLD = 5_000_000
TAX_RATE = 0.04  # 4% для ИП/самозанятых

# Логистический сбор Uzum по типу габарита (сум).
LOGISTICS_FEES = {
    "low": 4_000,   # авто: цена < 59 000
    "mgt": 5_000,   # малогабаритный
    "sgt": 8_000,   # среднегабаритный
    "kgt": 20_000,  # крупногабаритный (авто: is_kgt / цена > 5 млн)
}
# Человекочитаемые подписи тарифа для итогового сообщения.
WEIGHT_LABELS = {
    "low": "до 59 000",
    "mgt": "МГТ",
    "sgt": "СГТ",
    "kgt": "КГТ",
}


@dataclass
class CalcResult:
    sell_price: float
    purchase: float
    extra: float
    comm_pct: float        # доля комиссии (0.1 = 10%)
    commission: float      # комиссия Uzum в сум
    weight_class: str      # low | mgt | sgt | kgt
    logistics_label: str   # подпись тарифа (МГТ/СГТ/КГТ/…)
    logistics_fee: int     # логистический сбор Uzum в сум
    tax: float             # налог в сум
    net_profit: float      # чистая прибыль в сум
    margin: float          # маржинальность, %


def auto_weight_class(sell_price: float, is_kgt: bool) -> str | None:
    """Тариф без опроса пользователя.

    Возвращает 'kgt' (крупногабарит/дорогой) или 'low' (дешёвый) — для них опрос
    габаритов не нужен. Возвращает None, если нужно спросить (МГТ vs СГТ).
    """
    if is_kgt or sell_price > HIGH_PRICE_THRESHOLD:
        return "kgt"
    if sell_price < LOW_PRICE_THRESHOLD:
        return "low"
    return None


def compute_unit_economics(
    *,
    sell_price: float,
    purchase: float,
    extra: float,
    comm_pct: float,
    weight_class: str,
    tax_rate: float = TAX_RATE,
) -> CalcResult:
    """Посчитать юнит-экономику одной единицы товара.

    weight_class ∈ {low, mgt, sgt, kgt} задаёт логистический сбор Uzum.
    Чистая прибыль = Цена − Закупка − РасходыПользователя − КомиссияUzum
                     − ЛогистическийСборUzum − Налог.
    """
    logistics_fee = LOGISTICS_FEES.get(weight_class, LOGISTICS_FEES["mgt"])
    commission = sell_price * comm_pct
    tax = sell_price * tax_rate
    net_profit = sell_price - purchase - extra - commission - logistics_fee - tax
    margin = (net_profit / sell_price * 100.0) if sell_price else 0.0
    return CalcResult(
        sell_price=sell_price,
        purchase=purchase,
        extra=extra,
        comm_pct=comm_pct,
        commission=commission,
        weight_class=weight_class,
        logistics_label=WEIGHT_LABELS.get(weight_class, weight_class.upper()),
        logistics_fee=logistics_fee,
        tax=tax,
        net_profit=net_profit,
        margin=margin,
    )


__all__ = [
    "CalcResult",
    "compute_unit_economics",
    "auto_weight_class",
    "LOGISTICS_FEES",
    "WEIGHT_LABELS",
    "TAX_RATE",
]
