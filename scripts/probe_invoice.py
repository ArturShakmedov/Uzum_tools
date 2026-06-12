"""Диагностика 400 на /v1/fbs/invoice: перебираем кодировки query-параметров.

Токен берётся из окружения/.env (config.AUTH_TOKEN) и НЕ печатается.
Запуск:  python scripts/probe_invoice.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from config import API, AUTH_HEADER_NAME, AUTH_TOKEN

HEADERS = {
    AUTH_HEADER_NAME: AUTH_TOKEN or "",
    "Accept": "application/json",
    "Accept-Language": "ru",
}

CASES = [
    ("statuses repeated (list)", {"statuses": ["CREATED", "ACCEPTANCE_IN_PROGRESS", "ACCEPTED"], "page": 0, "size": 50}),
    ("statuses comma-joined",    {"statuses": "CREATED,ACCEPTANCE_IN_PROGRESS,ACCEPTED", "page": 0, "size": 50}),
    ("single status CREATED",    {"statuses": "CREATED", "page": 0, "size": 50}),
    ("single status ACCEPTED",   {"statuses": "ACCEPTED", "page": 0, "size": 50}),
    ("no page/size",             {"statuses": ["CREATED"]}),
    ("no statuses at all",       {"page": 0, "size": 50}),
]


def main() -> int:
    if not AUTH_TOKEN:
        print("UZUM_API_TOKEN не задан (.env / env).")
        return 1

    base = API.base_url.rstrip("/")
    with httpx.Client(base_url=base, headers=HEADERS, timeout=30) as c:
        for label, params in CASES:
            try:
                r = c.get("/v1/fbs/invoice", params=params)
                qs = str(r.request.url).split("?", 1)[-1]
                body = r.text[:200].replace("\n", " ")
                print(f"[{r.status_code}] {label}\n      query: {qs}\n      body : {body}\n")
            except httpx.HTTPError as exc:
                print(f"[ERR] {label}: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
