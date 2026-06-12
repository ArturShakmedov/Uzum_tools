"""Базовый HTTP-клиент для Uzum Seller OpenAPI.

Отвечает за: авторизацию, троттлинг (rate limit), повторы при 429/5xx,
единообразную обработку ошибок и распаковку конверта GenericResponse.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from config import API, AUTH_HEADER_NAME, AUTH_TOKEN, AUTH_TOKEN_PREFIX
from utils.logger import get_logger

log = get_logger(__name__)


class UzumAPIError(RuntimeError):
    """Ошибка уровня API (4xx/5xx или прикладные errors в теле ответа)."""

    def __init__(self, message: str, *, status: int | None = None,
                 payload: Any | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


class _RateLimiter:
    """Адаптивный потокобезопасный троттлер (token-bucket aware).

    Стартует с консервативного интервала, затем подстраивается под реальный
    серверный лимит из заголовка X-RateLimit-Replenish-Rate: устойчивая частота
    = replenish_rate запр./сек, поэтому min_interval = 1/replenish_rate.
    """

    _MAX_INTERVAL = 10.0  # верхний предел паузы между запросами, сек

    def __init__(self, rps: float) -> None:
        self._min_interval = 1.0 / rps if rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval

    def adapt(self, replenish_rate: float | None) -> None:
        """Подстроить интервал под устойчивую серверную частоту (1/R, +5% запас)."""
        if not replenish_rate or replenish_rate <= 0:
            return
        with self._lock:
            self._min_interval = min(1.05 / replenish_rate, self._MAX_INTERVAL)

    def penalize(self, factor: float = 2.0) -> None:
        """Временно замедлиться после 429."""
        with self._lock:
            base = self._min_interval or 0.25
            self._min_interval = min(base * factor, self._MAX_INTERVAL)


class UzumClient:
    """Тонкая обёртка над httpx с авторизацией, ретраями и троттлингом.

    Используется как контекст-менеджер::

        with UzumClient() as client:
            data = client.get("/v1/shops")
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._token = token or AUTH_TOKEN
        if not self._token:
            raise UzumAPIError(
                "Не задан API-токен. Передайте token= или переменную UZUM_API_TOKEN."
            )

        self._max_retries = max_retries if max_retries is not None else API.max_retries
        self._limiter = _RateLimiter(API.requests_per_second)
        self._client = httpx.Client(
            base_url=(base_url or API.base_url).rstrip("/"),
            timeout=timeout or API.timeout,
            headers=self._default_headers(),
        )

    # ------------------------------------------------------------------ #
    def _default_headers(self) -> dict[str, str]:
        token = f"{AUTH_TOKEN_PREFIX}{self._token}".strip()
        return {
            AUTH_HEADER_NAME: token,  # без префикса Bearer — так требует Uzum
            "Accept": "application/json",
            "Accept-Language": API.default_accept_language,
            "User-Agent": "Uzum_tools/0.1",
        }

    # ------------------------------------------------------------------ #
    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        unwrap: bool = True,
    ) -> Any:
        """Выполнить запрос с ретраями. Возвращает payload (или тело целиком)."""
        params = _clean_params(params)
        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            self._limiter.acquire()
            try:
                resp = self._client.request(
                    method, path, params=params, json=json, headers=headers
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                self._sleep_backoff(attempt, reason=str(exc))
                continue

            # Подстраиваем темп под фактический серверный лимит.
            self._limiter.adapt(_header_float(resp, "X-RateLimit-Replenish-Rate"))

            if resp.status_code == 429 or resp.status_code >= 500:
                if resp.status_code == 429:
                    self._limiter.penalize()
                retry_after = _retry_after_seconds(resp)
                log.warning(
                    "HTTP %s на %s (попытка %d/%d), повтор через %.1fс",
                    resp.status_code, path, attempt, self._max_retries, retry_after,
                )
                time.sleep(retry_after)
                last_exc = UzumAPIError(
                    f"HTTP {resp.status_code}", status=resp.status_code
                )
                continue

            return self._handle_response(resp, unwrap=unwrap)

        raise UzumAPIError(
            f"Превышено число повторов для {method} {path}: {last_exc}"
        ) from last_exc

    # ------------------------------------------------------------------ #
    def _handle_response(self, resp: httpx.Response, *, unwrap: bool) -> Any:
        if resp.status_code >= 400:
            raise UzumAPIError(
                f"HTTP {resp.status_code}: {resp.text[:300]}",
                status=resp.status_code,
            )

        ctype = resp.headers.get("content-type", "")
        if "application/json" not in ctype:
            # Бинарные ответы (PDF этикеток/накладных) возвращаем как bytes.
            return resp.content

        body = resp.json()

        if isinstance(body, dict):
            errors = body.get("errors")
            if errors:
                raise UzumAPIError(
                    f"Прикладная ошибка API: {errors}", payload=body
                )
            if unwrap and "payload" in body:
                return body["payload"]
        return body

    # ------------------------------------------------------------------ #
    def _sleep_backoff(self, attempt: int, *, reason: str) -> None:
        delay = API.backoff_factor * (2 ** (attempt - 1))
        log.warning("Сетевая ошибка (%s), повтор %d через %.1fс", reason, attempt, delay)
        time.sleep(delay)

    # ------------------------------------------------------------------ #
    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw)

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "UzumClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Убрать None-значения — иначе httpx отправит литерал 'None'."""
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _retry_after_seconds(resp: httpx.Response) -> float:
    header = resp.headers.get("Retry-After")
    if header and header.isdigit():
        return float(header)
    # Если сервер сообщил скорость пополнения — ждём ровно один токен.
    replenish = _header_float(resp, "X-RateLimit-Replenish-Rate")
    if replenish and replenish > 0:
        return min(1.0 / replenish + 0.2, 5.0)
    return min(API.backoff_factor * 4, 5.0)


def _header_float(resp: httpx.Response, name: str) -> float | None:
    raw = resp.headers.get(name)
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None


__all__ = ["UzumClient", "UzumAPIError"]
