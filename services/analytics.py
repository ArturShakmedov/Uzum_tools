"""Сервис аналитики невозвратов для одного пользователя бота.

Оркестрация: FIFO-расчёт (utils.analytics, строго в рамках telegram_id) →
Excel-файл (utils.excel) → путь к файлу + цифры для подписи.

Два типа отчёта по сроку давности невозврата (порог 30 дней):
  • "transit" — ещё не вернулись (≤30 дней с даты возврата/отмены);
  • "lost"    — утеряны (>30 дней), для подачи претензии.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import DATA_DIR
from database.connection import session_scope
from utils.analytics import ReturnsAnalysis, check_missing_goods
from utils.excel import build_losses_xlsx
from utils.logger import get_logger

log = get_logger(__name__)

_REPORTS_DIR = DATA_DIR / "reports"

# Порог давности для разделения на «не вернулись» / «утеряны».
SPLIT_DAYS = 30

# Параметры по типу отчёта: (имя файла, заголовок внутри Excel).
_REPORT_META: dict[str, tuple[str, str]] = {
    "transit": (
        "losses_transit_{tg}.xlsx",
        "Товары в ожидании возврата на склад (менее 30 дней)",
    ),
    "lost": (
        "lost_goods_{tg}.xlsx",
        "Реестр товаров для подачи претензии по утере (свыше 30 дней)",
    ),
}


@dataclass
class LossReport:
    """Готовый отчёт: путь к .xlsx + цифры для подписи сообщения."""

    path: Path
    report_type: str
    row_count: int          # строк в этом отчёте (после фильтра по сроку)
    total_returns: int      # всего RETURNED/CANCELED позиций (контекст)
    total_received: int
    has_rows: bool


def generate_loss_report(telegram_id: int, report_type: str = "lost") -> LossReport:
    """Построить отчёт по невозвратам выбранного типа и сохранить .xlsx.

    report_type ∈ {"transit", "lost"}. Синхронная функция (в боте — to_thread).
    """
    if report_type not in _REPORT_META:
        report_type = "lost"
    filename_tpl, title = _REPORT_META[report_type]

    # Сессия закрывается сразу после расчёта — сырые выборки из БД не висят в RAM.
    with session_scope() as session:
        analysis: ReturnsAnalysis = check_missing_goods(
            session, telegram_id, threshold_days=SPLIT_DAYS, report_type=report_type
        )

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _REPORTS_DIR / filename_tpl.format(tg=telegram_id)
    build_losses_xlsx(analysis.rows, path, title=title)

    result = LossReport(
        path=path,
        report_type=report_type,
        row_count=len(analysis.rows),
        total_returns=analysis.total_returns,
        total_received=analysis.total_received,
        has_rows=bool(analysis.rows),
    )

    return result


__all__ = ["generate_loss_report", "LossReport", "SPLIT_DAYS"]
