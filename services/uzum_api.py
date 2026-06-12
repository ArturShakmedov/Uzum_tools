"""Финансовый сервис: баланс и выплаты из Uzum Merchant API.

Баланс собираем из /v1/finance/orders (Σ sellerProfit по статусам), выплаты/
удержания — из /v1/finance/expenses. Готового «баланса» одной цифрой Uzum не
отдаёт (проверено по спеке), поэтому суммируем — но СТРОГО отслеживаем полноту
выгрузки: если страницы оборвались (429/timeout), баланс помечается неполным и
снимок НЕ перезаписывается частичным значением (иначе сумма «урезается»).

Финансы развязаны от тяжёлого общего синка: refresh_finance() — отдельный
быстрый путь с коротким timeout, который вызывает кнопка «💰 Выплаты и баланс».
"""

from __future__ import annotations

from dataclasses import dataclass, field

from api.client import UzumAPIError, UzumClient
from api.endpoints import UzumAPI
from database.connection import session_scope
from database.repository import get_active_shop, save_finance_snapshot
from utils.logger import get_logger

log = get_logger(__name__)

_WITHDRAW = "TO_WITHDRAW"
_PROCESSING = "PROCESSING"
_OUTCOME = "OUTCOME"  # исходящий платёж: списание (вывод на р/с ИЛИ удержание)

# Точный код вывода на расчётный счёт (подтверждён на боевых данных Uzum).
_PAYOUT_SOURCE = "WITHDRAWAL"
# Статус успешно проведённой выплаты (вычитается из доступного баланса).
_PAYOUT_CONFIRMED = "CONFIRMED"
# Запасной фолбэк по тексту source/name — на случай иных формулировок.
_PAYOUT_KEYWORDS = (
    "вывод", "выплат", "на расч", "на счет", "на счёт", "расчет", "расчёт",
    "payout", "withdraw", "transfer", "settlement",
)

_ORDERS_PAGE_SIZE = 50
_ORDERS_MAX_PAGES = 200          # верхний предохранитель пагинации заказов
_EXPENSES_MAX_PAGES = 6
_EXPENSES_PAGE_SIZE = 50
_RECENT_PAYMENTS = 5

# Отдельный быстрый клиент для финансов: короткий timeout, мало ретраев —
# чтобы кнопка не висла, а быстро падала в фолбэк на кэш.
_FINANCE_TIMEOUT = 20.0
_FINANCE_RETRIES = 2


@dataclass
class FinanceSummary:
    """Сводка финансов магазина (суммы в сум)."""

    available: int = 0
    pending: int = 0
    commissions: int = 0
    payments: list[dict] = field(default_factory=list)
    has_data: bool = False
    balance_ok: bool = False  # баланс выгружен ПОЛНОСТЬЮ (можно доверять/сохранять)


def _to_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_payout(payment: dict) -> bool:
    """Платёж — это вывод на расчётный счёт (а не удержание)?

    Главный признак — source == 'WITHDRAWAL' (боевой код Uzum). Текстовый поиск
    по source/name оставлен запасным фолбэком на случай иных формулировок.
    """
    if (payment.get("source") or "").upper() == _PAYOUT_SOURCE:
        return True
    text = f"{payment.get('source') or ''} {payment.get('name') or ''}".casefold()
    return any(kw in text for kw in _PAYOUT_KEYWORDS)


def fetch_finance_summary(api: UzumAPI, shop_id: int) -> FinanceSummary:
    """Собрать финансовую сводку. Никогда не бросает наружу.

    balance_ok=True только если ВСЕ страницы finance/orders выгружены без сбоев
    (иначе баланс частичный — доверять нельзя).
    """
    summary = FinanceSummary()

    # 1) Баланс из finance/orders — с контролем полноты выгрузки.
    try:
        page = 0
        while page < _ORDERS_MAX_PAGES:
            batch = api.finance.orders_page(
                shop_id, statuses=[_WITHDRAW, _PROCESSING],
                page=page, size=_ORDERS_PAGE_SIZE,
            )
            if not batch:
                summary.balance_ok = True       # дошли до конца — выгрузка полная
                break
            for item in batch:
                summary.has_data = True
                profit = _to_int(item.get("sellerProfit"))
                status = item.get("status")
                if status == _WITHDRAW:
                    summary.available += profit
                elif status == _PROCESSING:
                    summary.pending += profit
            if len(batch) < _ORDERS_PAGE_SIZE:
                summary.balance_ok = True
                break
            page += 1
        else:
            log.warning("finance/orders shop %s: упёрлись в лимит страниц — неполно", shop_id)
    except (UzumAPIError, Exception) as exc:  # noqa: BLE001
        log.warning("finance/orders shop %s: частичная выгрузка (%s)", shop_id, exc)
        summary.balance_ok = False

    # 2) Расходы: разделяем ВЫВОДЫ на р/с и УДЕРЖАНИЯ (не влияет на balance_ok).
    payouts: list[dict] = []
    try:
        for page in range(_EXPENSES_MAX_PAGES):
            rows = api.finance.expenses(shop_id, page=page, size=_EXPENSES_PAGE_SIZE)
            if not rows:
                break
            for p in rows:
                summary.has_data = True
                if p.get("type") != _OUTCOME:
                    continue
                price = _to_int(p.get("paymentPrice"))
                if _is_payout(p):
                    payouts.append({
                        "date": p.get("dateCreated") or p.get("dateService"),
                        "amount": price,
                        "status": p.get("status"),
                        "name": p.get("name"),
                    })
                else:
                    summary.commissions += price
            if len(rows) < _EXPENSES_PAGE_SIZE:
                break
    except (UzumAPIError, Exception) as exc:  # noqa: BLE001
        log.warning("finance/expenses shop %s недоступен: %s", shop_id, exc)

    # 3) Доступный баланс = прибыль TO_WITHDRAW − уже сделанные выплаты на р/с.
    #    Uzum НЕ уменьшает sellerProfit и не меняет статус заказов после выплаты,
    #    поэтому вычитаем сумму подтверждённых выплат WITHDRAWAL вручную.
    gross_available = summary.available
    total_payouts = sum(
        _to_int(p.get("amount"))
        for p in payouts
        if p.get("status") == _PAYOUT_CONFIRMED
    )
    summary.available = max(0, int(gross_available) - int(total_payouts))
    log.info(
        "finance shop %s: TO_WITHDRAW(gross)=%d − выплаты(CONFIRMED)=%d → доступно=%d; PROCESSING=%d",
        shop_id, gross_available, total_payouts, summary.available, summary.pending,
    )

    payouts.sort(key=lambda r: r["date"] or "", reverse=True)
    summary.payments = payouts[:_RECENT_PAYMENTS]
    return summary


def refresh_finance(telegram_id: int) -> bool:
    """Обновить снимок финансов активного магазина. True — снимок сохранён.

    Быстрый отдельный путь (короткий timeout). Снимок перезаписывается ТОЛЬКО при
    полной выгрузке баланса (balance_ok) — чтобы частичная сумма не «урезала»
    показатели. Синхронно (в боте вызывается через asyncio.to_thread под timeout).
    """
    with session_scope() as session:
        shop = get_active_shop(session, telegram_id)
        token = shop.uzum_token if shop else None
        shop_id = shop.uzum_shop_id if shop else None
    if not token or shop_id is None:
        return False

    try:
        with UzumClient(
            token=token, timeout=_FINANCE_TIMEOUT, max_retries=_FINANCE_RETRIES
        ) as client:
            summary = fetch_finance_summary(UzumAPI(client), shop_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("refresh_finance shop %s: %s", shop_id, exc)
        return False

    if not summary.balance_ok:
        # Не перезаписываем кэш частичным балансом — лучше показать прошлый снимок.
        return False

    with session_scope() as session:
        save_finance_snapshot(
            session,
            telegram_id,
            shop_id=shop_id,
            available=summary.available,
            pending=summary.pending,
            commissions=summary.commissions,
            payments=summary.payments,
            has_data=summary.has_data,
        )
    return True


__all__ = ["FinanceSummary", "fetch_finance_summary", "refresh_finance"]
