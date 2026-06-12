"""Регрессионные тесты критической логики (аудит-закрепление, июнь 2026).

Покрытие: математика FBS-дедлайнов, карта статусов Uzum→БД и мост синка,
идемпотентность платежей (unique charge_id), продление/триал подписки,
дебаунс инвойсов, наивные/aware даты, фикс ensure_user при autoflush=False.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError

_UTC = dt.timezone.utc


def _now() -> dt.datetime:
    return dt.datetime.now(_UTC)


def _ms_ago(hours: float) -> int:
    return int((_now() - dt.timedelta(hours=hours)).timestamp() * 1000)


# --------------------------------------------------------------------------- #
#  utils.fbs_calc — зоны критичности дедлайна
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "hours_ago, level, overdue",
    [
        (2, "safe", False),        # осталось ~22 ч  → 🟢
        (11.5, "safe", False),     # ~12.5 ч — чуть выше порога 12
        (15, "urgent", False),     # ~9 ч            → 🟡
        (21, "critical", False),   # ~3 ч            → 🔴
        (30, "critical", True),    # просрочен       → 🔴 + overdue
    ],
)
def test_fbs_deadline_zones(hours_ago, level, overdue):
    from utils.fbs_calc import calculate_fbs_deadline

    d = calculate_fbs_deadline(_now() - dt.timedelta(hours=hours_ago))
    assert d["level"] == level
    assert d["overdue"] is overdue
    if overdue:
        assert d["hours"] == 0 and d["minutes"] == 0


def test_fbs_deadline_accepts_naive_datetime():
    """SQLite отдаёт naive datetime — трактуем как UTC, не падаем."""
    from utils.fbs_calc import calculate_fbs_deadline

    naive = _now().replace(tzinfo=None) - dt.timedelta(hours=5)
    assert calculate_fbs_deadline(naive)["level"] == "safe"


# --------------------------------------------------------------------------- #
#  Мост синка FBS: карта статусов, upsert, фильтр схем, нормализация регистра
# --------------------------------------------------------------------------- #
def _dto(oid, status, scheme="FBS", hours_ago=2.0, sku="Платье"):
    return {
        "id": oid, "scheme": scheme, "status": status,
        "dateCreated": _ms_ago(hours_ago), "orderItems": [{"skuTitle": sku}],
    }


def test_sync_fbs_orders_mapping_and_filtering(db_session):
    from database.models import FBSOrder
    from database.repository import sync_fbs_orders
    from sqlalchemy import select

    tg = 9001
    n = sync_fbs_orders(db_session, tg, [
        _dto(1, "CREATED"),
        _dto(2, "PENDING_DELIVERY"),       # «В поставке» → DELIVERY
        _dto(3, "delivering"),             # lowercase из API → SHIPPING
        _dto(4, "COMPLETED"),              # терминальный → SHIPPED
        _dto(5, "CREATED", scheme="DBS"),  # не FBS → мимо моста
    ])
    assert n == 4
    rows = {
        r.uzum_order_id: r.status
        for r in db_session.execute(
            select(FBSOrder).where(FBSOrder.telegram_id == tg)
        ).scalars()
    }
    assert rows == {"1": "NEW", "2": "DELIVERY", "3": "SHIPPING", "4": "SHIPPED"}


def test_sync_fbs_orders_upsert_updates_status(db_session):
    from database.models import FBSOrder
    from database.repository import sync_fbs_orders
    from sqlalchemy import select

    tg = 9002
    sync_fbs_orders(db_session, tg, [_dto(10, "CREATED")])
    sync_fbs_orders(db_session, tg, [_dto(10, "PACKING")])  # переход этапа
    rows = db_session.execute(
        select(FBSOrder).where(FBSOrder.telegram_id == tg)
    ).scalars().all()
    assert len(rows) == 1 and rows[0].status == "PACKING"


def test_active_fbs_selection_includes_delivery(db_session):
    """Таймер видит NEW/PACKING/DELIVERY/SHIPPING и не видит терминальные."""
    from database.repository import list_active_fbs_orders, sync_fbs_orders

    tg = 9003
    sync_fbs_orders(db_session, tg, [
        _dto(21, "CREATED"), _dto(22, "PENDING_DELIVERY"),
        _dto(23, "DELIVERING"), _dto(24, "RETURNED"), _dto(25, "CANCELED"),
    ])
    active = {o.uzum_order_id for o in list_active_fbs_orders(db_session, tg)}
    assert active == {"21", "22", "23"}


# --------------------------------------------------------------------------- #
#  Деньги: идемпотентность, продление, триал, дебаунс
# --------------------------------------------------------------------------- #
def test_duplicate_charge_id_raises_integrity_error(db_session):
    """Повторный SUCCESSFUL_PAYMENT (тот же charge_id) падает ДО начисления."""
    from database.repository import complete_payment_log, create_payment_log

    tg = 9101
    create_payment_log(db_session, tg, "premium_1_month", 150_000)
    complete_payment_log(
        db_session, tg, "premium_1_month", charge_id="charge-AAA", amount=150_000
    )
    create_payment_log(db_session, tg, "premium_1_month", 150_000)
    with pytest.raises(IntegrityError):
        complete_payment_log(
            db_session, tg, "premium_1_month", charge_id="charge-AAA", amount=150_000
        )
    db_session.rollback()


def test_activate_premium_extends_active_subscription(db_session):
    """Активная подписка продлевается ОТ expires_at, истёкшая — от now."""
    from database.repository import activate_premium

    tg = 9102
    user = activate_premium(db_session, tg, days=30)
    first_exp = user.subscription_expires_at
    user = activate_premium(db_session, tg, days=30)  # докупка при активной
    base = first_exp if first_exp.tzinfo else first_exp.replace(tzinfo=_UTC)
    exp = user.subscription_expires_at
    exp = exp if exp.tzinfo else exp.replace(tzinfo=_UTC)
    assert abs((exp - base) - dt.timedelta(days=30)) < dt.timedelta(seconds=5)


def test_welcome_trial_granted_once(db_session):
    """Триал один раз на аккаунт; перепривязка магазина не сбрасывает."""
    from database.repository import activate_welcome_trial

    tg = 9103
    assert activate_welcome_trial(db_session, tg) is True
    assert activate_welcome_trial(db_session, tg) is False  # abuse-защита


def test_trial_not_granted_after_paid_subscription(db_session):
    from database.repository import activate_premium, activate_welcome_trial

    tg = 9104
    activate_premium(db_session, tg, days=30)
    assert activate_welcome_trial(db_session, tg) is False


def test_invoice_debounce(db_session):
    """Повторный клик «купить» в течение окна не плодит created-записи."""
    from database.repository import create_payment_log, has_recent_open_payment

    tg = 9105
    assert has_recent_open_payment(db_session, tg, "premium_1_month") is False
    create_payment_log(db_session, tg, "premium_1_month", 150_000)
    db_session.flush()
    assert has_recent_open_payment(db_session, tg, "premium_1_month") is True
    # Другой тариф — отдельное окно дебаунса.
    assert has_recent_open_payment(db_session, tg, "premium_1_year") is False


def test_is_user_premium_naive_and_aware(db_session):
    from database.models import User
    from database.repository import is_user_premium

    user = User(
        telegram_id=9106, subscription_tier="premium",
        subscription_expires_at=_now().replace(tzinfo=None) + dt.timedelta(days=1),
    )
    assert is_user_premium(user) is True
    user.subscription_expires_at = _now() - dt.timedelta(days=1)
    assert is_user_premium(user) is False


def test_ensure_user_twice_in_one_session(db_session):
    """Регрессия: при autoflush=False второй ensure_user не дублирует INSERT."""
    from database.repository import ensure_user

    tg = 9107
    a = ensure_user(db_session, tg)
    b = ensure_user(db_session, tg)
    assert a is b  # вторая выборка видит pending-строку (flush внутри)


# --------------------------------------------------------------------------- #
#  Нормализация дат Uzum
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    [
        1726000000000,              # epoch мс
        1726000000,                 # epoch сек
        "2024-09-26T12:00:00Z",     # ISO с Z
        "2024-09-26 12:00:00",      # человекочитаемый
    ],
)
def test_parse_dt_variants(raw):
    from database.repository import _parse_dt

    parsed = _parse_dt(raw)
    assert parsed is not None and parsed.tzinfo is not None


def test_parse_dt_garbage_returns_none():
    from database.repository import _parse_dt

    assert _parse_dt("не дата") is None and _parse_dt(None) is None
