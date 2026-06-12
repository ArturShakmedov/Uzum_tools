"""Изолированная диагностика доступности Uzum Seller API и состояния нашего IP.

Запуск (вне архитектуры бота, один лёгкий запрос):
    .venv/bin/python utils/check_api_health.py

Что делает: ОДИН легальный GET /v1/shops (самый дешёвый авторизованный эндпоинт)
с боевыми параметрами авторизации из config.py и обычным браузерным User-Agent.
По ответу выносит вердикт: жив ли API, душит ли нас rate-limit, отозван ли токен
или IP забанен файрволом (Cloudflare/DDoS-protection).

Токен: UZUM_API_TOKEN из окружения → фолбэк: токен активного магазина из БД
(расшифровка EncryptedToken, нужен ENCRYPTION_KEY) → без авторизации (тогда
401 в ответе — это ХОРОШО: значит, до API мы достучались и IP не заблокирован).

Коды выхода: 0 — OK · 1 — авторизация · 2 — бан/недоступность · 3 — rate limit.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

# Запуск напрямую из utils/ → корень проекта в sys.path для импорта config.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import API, AUTH_HEADER_NAME, AUTH_TOKEN, AUTH_TOKEN_PREFIX, ENDPOINTS

# Обычный браузерный UA: «голый» python-httpx/x.y режется анти-ботом на входе.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Заголовки rate-limit'а Uzum (token bucket) — печатаем все, что пришли.
_RATE_HEADERS = (
    "Retry-After",
    "X-RateLimit-Remaining",
    "X-RateLimit-Burst-Capacity",
    "X-RateLimit-Replenish-Rate",
    "X-RateLimit-Requested-Tokens",
)


def _resolve_token() -> tuple[str | None, str]:
    """(токен, источник). Порядок: env UZUM_API_TOKEN → активный магазин из БД."""
    if AUTH_TOKEN:
        return AUTH_TOKEN, "env UZUM_API_TOKEN"
    try:  # фолбэк: расшифрованный токен первого активного магазина бота
        from database.connection import session_scope
        from database.repository import list_all_active_shops

        with session_scope() as session:
            shops = list_all_active_shops(session)
            if shops:
                return shops[0].uzum_token, f"БД (магазин «{shops[0].shop_name}»)"
    except Exception as exc:  # noqa: BLE001 — нет ключа/БД: диагностика без авторизации
        print(f"⚠️ Токен из БД недоступен ({type(exc).__name__}: {exc}).")
    return None, "—"


def main() -> int:
    url = API.base_url + ENDPOINTS.shops
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": API.default_accept_language,
    }

    token, source = _resolve_token()
    if token:
        # По схеме TokenAuth Uzum: голый токен в Authorization, БЕЗ "Bearer".
        headers[AUTH_HEADER_NAME] = f"{AUTH_TOKEN_PREFIX}{token}"
        print(f"🔑 Токен: {token[:4]}…{token[-4:]} (источник: {source})")
    else:
        print("⚠️ Запрос пойдёт БЕЗ авторизации: 401 в ответе будет означать, "
              "что API и IP живы, а проблема только в токене.")

    print(f"🌐 GET {url}")
    started = time.monotonic()
    try:
        resp = httpx.get(url, headers=headers, timeout=10.0)
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ReadTimeout,
            httpx.RemoteProtocolError) as exc:
        print(f"   сеть: {type(exc).__name__}: {exc}")
        print("🚨 КРИТИЧЕСКИЙ БАН: Наш IP-адрес заблокирован файрволом Uzum "
              "(Cloudflare/DDoS-protection) или API полностью лежит.")
        return 2

    latency_ms = (time.monotonic() - started) * 1000
    print(f"📡 HTTP {resp.status_code} за {latency_ms:.0f} мс")
    for name in _RATE_HEADERS:
        if name in resp.headers:
            print(f"   {name}: {resp.headers[name]}")

    code = resp.status_code
    if code == 200:
        print("🟢 API доступно, IP не заблокирован. Проблема в логике циклов воркера.")
        return 0
    if code == 429:
        print("🟡 Превышен лимит частоты запросов (Rate Limit). Uzum просит нас "
              "притормозить.")
        retry_after = resp.headers.get("Retry-After")
        print(f"   Retry-After: {retry_after}" if retry_after
              else "   Заголовок Retry-After не передан — ориентируйтесь на "
                   "X-RateLimit-Replenish-Rate выше.")
        return 3
    if code in (401, 403):
        print("❌ Ошибка авторизации. Возможно, отозван API-токен (App-Token).")
        if not token:
            print("   (Токен не передавался — это ОЖИДАЕМО: сам факт ответа "
                  "значит, что IP НЕ забанен и API живо.)")
        return 1
    if code in (502, 503, 504):
        print("🚨 КРИТИЧЕСКИЙ БАН/АВАРИЯ: шлюз Uzum отвечает 5xx — IP режется "
              "на периметре (Cloudflare) либо API лежит целиком.")
        return 2
    print(f"❓ Неожиданный статус {code}: {resp.text[:300]}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
