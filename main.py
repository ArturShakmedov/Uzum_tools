"""Админ-CLI Uzum_tools (после перехода на бота).

Работает в мультитенантной схеме — все операции требуют --user <telegram_id>.
Тяжёлую логику делегирует в services/ (та же, что использует бот).

Примеры:
  python main.py --user 12345 --sync            # полная синхронизация (за всё время)
  python main.py --user 12345 --check-losses    # текстовый отчёт по невозвратам
  python main.py --user 12345 --debug-returns    # офлайн-диагностика данных
"""

from __future__ import annotations

import argparse

from database.connection import init_db, session_scope
from database.repository import SyncReport
from services.uzum_sync import SyncError, fetch_everything_from_uzum, persist_to_db
from utils.crypto import ensure_encryption_key
from utils.analytics import (
    RESULT_ACCEPTED,
    RESULT_IN_TRANSIT,
    RESULT_LOST,
    check_missing_goods,
    debug_returns,
    summarize,
)
from utils.logger import get_logger

log = get_logger(__name__)


def run(args: argparse.Namespace) -> int:
    # Fail-fast: без ENCRYPTION_KEY токены в БД нечитаемы — не выполняем команды
    # (та же строгая проверка, что и в bot.py). RuntimeError завершит скрипт.
    ensure_encryption_key()
    init_db()
    telegram_id = args.user

    if args.check_losses:
        return _run_loss_report(telegram_id, args.loss_threshold)
    if args.debug_returns:
        return _run_returns_debug(telegram_id)

    # По умолчанию — синхронизация. CLI синхронный и однопользовательский, поэтому
    # вызываем две фазы напрямую (async-оркестратор с локами нужен только боту).
    log_progress = lambda m: log.info("%s", m)  # noqa: E731
    try:
        bundle = fetch_everything_from_uzum(telegram_id, log_progress)
        report = persist_to_db(telegram_id, bundle, log_progress)
    except SyncError as exc:
        log.error("Ошибка синхронизации: %s", exc)
        return 1
    _print_summary(report)
    return 0


# --------------------------------------------------------------------------- #
#  Отчёт по невозвратам
# --------------------------------------------------------------------------- #
def _run_loss_report(telegram_id: int, threshold: int) -> int:
    with session_scope() as session:
        analysis = check_missing_goods(session, telegram_id, threshold_days=threshold)
    _print_returns_control(analysis)
    _print_losses_table(analysis.rows, threshold=threshold)
    return 0


def _print_returns_control(analysis) -> None:
    print("\n----- Контрольная сводка сопоставления (FIFO по артикулу) -----")
    print(f"  Всего возвратов/отмен в заказах            : {analysis.total_returns}")
    print(f"  Всего единиц принято (возвраты /v1/return) : {analysis.total_received}")
    print(f"  Успешно сопоставлено (вернулось на склад)  : {analysis.matched}")
    print(f"  Реально не возвращено (разница)            : {analysis.unmatched}")


def _truncate(text: str | None, limit: int = 25) -> str:
    s = text or "—"
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _print_losses_table(rows, threshold: int) -> None:
    headers = [
        "ID Заказа", "Дата события", "SKU / Артикул", "Штрихкод", "Название товара",
        "Статус Uzum", "Дней", "Итог проверки",
    ]
    if not rows:
        print("\nНет заказов в статусах RETURNED/CANCELED — проверять нечего.")
        return

    data: list[list[str]] = []
    for r in rows:
        sku_cell = str(r.sku_id) if r.sku_id is not None else (r.article or "—")
        data.append([
            str(r.order_id),
            r.event_date.strftime("%Y-%m-%d") if r.event_date else "—",
            sku_cell[:28],
            r.barcode.ljust(15)[:15],
            _truncate(r.title, 25),
            r.uzum_status,
            str(r.days_elapsed) if r.days_elapsed is not None else "—",
            r.result,
        ])

    widths = [len(h) for h in headers]
    for row in data:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def _fmt(cells: list[str]) -> str:
        return " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    print(f"\nОтчёт по невозвратам (порог {threshold} дн., отсчёт от даты возврата/отмены):\n")
    print(_fmt(headers))
    print("-+-".join("-" * w for w in widths))
    for row in data:
        print(_fmt(row))

    counts = summarize(rows)
    print("\nИтого:")
    for result in (RESULT_LOST, RESULT_IN_TRANSIT, RESULT_ACCEPTED):
        if counts.get(result):
            print(f"  {result}: {counts[result]}")


# --------------------------------------------------------------------------- #
#  Диагностика
# --------------------------------------------------------------------------- #
def _run_returns_debug(telegram_id: int) -> int:
    with session_scope() as session:
        dbg = debug_returns(session, telegram_id)
    print("\n===== ДИАГНОСТИКА НЕВОЗВРАТОВ (offline) =====\n")
    print(f"  Позиций RETURNED/CANCELED            : {dbg.watched_items}")
    print(f"    из них со штрихкодом              : {dbg.linked_watched_items}")
    print(f"  Записей в barcodes                   : {dbg.barcodes_total}")
    print(f"  Номеров накладных в заказах          : {dbg.referenced_numbers}")
    print(f"    есть в БД                          : {dbg.present_numbers}")
    print(f"    отсутствуют                        : {dbg.missing_numbers}")
    print(f"  Накладных в БД                       : {len(dbg.invoices)}")
    return 0


# --------------------------------------------------------------------------- #
#  Сводка синхронизации
# --------------------------------------------------------------------------- #
def _print_summary(report: SyncReport) -> None:
    order = ["orders", "order_items", "sku_catalog", "invoices", "barcodes",
             "returns", "return_items"]
    rows = [report.tallies[k] for k in order if k in report.tallies]
    width = 58
    line = "─" * width
    print("\n┌" + line + "┐")
    print("│ {:^{w}} │".format("ИТОГИ СИНХРОНИЗАЦИИ Uzum_tools", w=width - 2))
    print("├" + line + "┤")
    print("│ {:<20}{:>9}{:>9}{:>9}{:>8} │".format(
        "Сущность", "Новые", "Обнов.", "Ошибки", "Всего"))
    print("├" + line + "┤")
    grand = 0
    for t in rows:
        grand += t.total
        print("│ {:<20}{:>9}{:>9}{:>9}{:>8} │".format(
            t.name, t.created, t.updated, t.failed, t.total))
    print("├" + line + "┤")
    print("│ {:<20}{:>35} │".format("ВСЕГО записано в БД:", grand))
    print("└" + line + "┘")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Uzum_tools admin CLI (мультитенант)")
    p.add_argument("--user", type=int, required=True, help="telegram_id владельца данных")
    p.add_argument("--sync", action="store_true", help="Полная синхронизация (по умолчанию)")
    p.add_argument("--check-losses", action="store_true", help="Текстовый отчёт по невозвратам")
    p.add_argument("--debug-returns", action="store_true", help="Офлайн-диагностика данных")
    p.add_argument("--loss-threshold", type=int, default=7, help="Порог дней (по умолч. 7)")
    return p


if __name__ == "__main__":
    raise SystemExit(run(build_parser().parse_args()))
