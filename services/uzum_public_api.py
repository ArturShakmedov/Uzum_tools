"""Лёгкий парсер публичной WEB-версии Uzum Market (без браузера) для анализа конкурентов.

Почему web, а не app-API: мобильный API (`api.uzum.uz`, `graphql.uzum.uz`) требует
app-токен `x-iid`/подпись (401). Поэтому читаем публичные HTML-страницы
`https://uzum.uz/ru/...` и достаём из них данные: сперва пытаемся вытащить
JSON-стейт инициализации (`window.__INITIAL_STATE__` / `__NUXT__` / `__NEXT_DATA__`
— там в чистом JSON и рейтинг, и фото, и характеристики), иначе — bs4 по тегам.

Масштаб/устойчивость под нагрузкой 30+ юзеров:
  • `CONCURRENCY_LIMITER = asyncio.Semaphore(5)` — не больше 5 одновременных
    запросов к Uzum на весь процесс (стабильность сервера + не злим анти-бот);
  • httpx.AsyncClient(http2=True) + мобильные заголовки + опциональный ротируемый
    прокси (config.PROXY_URL) + жёсткий таймаут 5 с.

Функции ЗАЩИТНЫЕ: при любой ошибке/блокировке — []/None, бот не падает, фича
деградирует в «Экспресс-аудит» (см. handlers/competitors_analytics).
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from config import PROXY_URL, UZUM_PUBLIC_API_BASE, UZUM_WEB_BASE
from utils.logger import get_logger

log = get_logger(__name__)

_TIMEOUT = 5.0

# 🚦 Глобальный потолок одновременных запросов к Uzum: 30 юзеров → пачками по 5.
CONCURRENCY_LIMITER = asyncio.Semaphore(5)

# Мимикрия под официальное мобильное приложение Uzum Market (Android).
MOBILE_HEADERS: dict[str, str] = {
    "User-Agent": "Uzum/3.4.0 (Android 13; Scale/3.0; Build/4567; Xiaomi 2201116PG)",
    "Accept": "text/html, application/json, text/plain, */*",
    "Accept-Language": "ru-RU, ru;q=0.9, uz-UZ;q=0.8, uz;q=0.7, en-US;q=0.6, en;q=0.5",
    "X-Platform": "ANDROID",
    "X-App-Version": "3.4.0",
    "Connection": "keep-alive",
}

# h2 нужен для http2=True; нет пакета — fallback на HTTP/1.1 (не падаем).
try:
    import h2  # noqa: F401

    _HTTP2 = True
except ImportError:  # pragma: no cover
    _HTTP2 = False
    log.warning("Пакет h2 не установлен — HTTP/2 недоступен, fallback на HTTP/1.1.")


def _make_client() -> httpx.AsyncClient:
    """AsyncClient: HTTP/2 + мобильные заголовки + ротируемый прокси (если задан).

    httpx 0.28+ принимает `proxy=` (одиночный, на все схемы); параметр `proxies=`
    из старых версий удалён. Пустой PROXY_URL → без прокси (локальные тесты).
    """
    proxy = PROXY_URL or None
    return httpx.AsyncClient(
        http2=_HTTP2,
        proxy=proxy,
        follow_redirects=True,
        headers=MOBILE_HEADERS,
        timeout=_TIMEOUT,
    )


async def _get_html(url: str) -> str | None:
    """GET страницы под семафором (≤5 одновременно). None при не-200/ошибке.

    Дебаг-лог при статусе ≠ 200: видно 403 (блок) vs 404 (нет пути) vs пустота.
    """
    async with CONCURRENCY_LIMITER:                 # 🚦 пачками по 5
        try:
            async with _make_client() as client:
                resp = await client.get(url)
        except Exception as exc:  # noqa: BLE001 — сеть/прокси/таймаут
            log.warning("GET %s сеть упала: %s", url, exc)
            return None
    if resp.status_code != 200:
        log.warning("GET %s: HTTP %s | %s", url, resp.status_code, resp.text[:200])
        return None
    return resp.text


async def _get_json(url: str) -> dict | None:
    """GET чистого JSON под семафором (для мобильного API каталога).

    Возвращает распарсенный JSON или None. На 401/403 (нужен app-токен/подпись) —
    None: вызывающий код тогда уходит в фолбэк на локальные метрики БД («Экспресс-
    аудит»), без сетевой ошибки. Редирект-цикл (web-WAF) ловится в except → None.
    """
    async with CONCURRENCY_LIMITER:                 # 🚦 пачками по 5
        try:
            async with _make_client() as client:
                resp = await client.get(url)
        except Exception as exc:  # noqa: BLE001 — сеть/прокси/таймаут/редирект-цикл
            log.warning("GET %s сеть упала: %s", url, exc)
            return None
    if resp.status_code in (401, 403):
        log.warning("GET %s: HTTP %s — мобильный API требует подпись, фолбэк на "
                    "локальные метрики БД.", url, resp.status_code)
        return None
    if resp.status_code != 200:
        log.warning("GET %s: HTTP %s | %s", url, resp.status_code, resp.text[:200])
        return None
    try:
        return resp.json()
    except (ValueError, TypeError) as exc:
        log.warning("GET %s не-JSON: %s | %s", url, exc, resp.text[:200])
        return None


# --------------------------------------------------------------------------- #
#  Извлечение JSON-стейта инициализации из HTML (балансировка скобок)
# --------------------------------------------------------------------------- #
_STATE_MARKERS = (
    "window.__INITIAL_STATE__",
    "window.__NUXT__",
    "window.__PRELOADED_STATE__",
    "__NEXT_DATA__",
)


def _json_after(html: str, marker: str) -> dict | None:
    """JSON-объект сразу после маркера (учёт вложенных скобок, не жадная регулярка)."""
    i = html.find(marker)
    if i < 0:
        return None
    start = html.find("{", i)
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for j in range(start, len(html)):
        c = html[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : j + 1])
                except (ValueError, TypeError):
                    return None
    return None


def _extract_state_json(html: str) -> dict | None:
    for marker in _STATE_MARKERS:
        state = _json_after(html, marker)
        if state:
            return state
    return None


# --------------------------------------------------------------------------- #
#  Поиск конкурентов (web-страница поиска)
# --------------------------------------------------------------------------- #
async def search_competitors_on_uzum(query_text: str, limit: int = 3) -> list[int]:
    """ID топ-`limit` конкурентов по тексту запроса со страницы поиска Uzum.

    GET {web}/search?query=...; productId достаём из JSON-стейта, иначе — из ссылок
    /product/<id> в HTML. [] при любой недоступности.
    """
    query_text = (query_text or "").strip()
    if not query_text:
        return []
    html = await _get_html(f"{UZUM_WEB_BASE}/search?query={quote(query_text)}")
    if not html:
        return []

    ids: list[int] = []
    state = _extract_state_json(html)
    if state:
        _collect_ids_from_state(state, ids, limit)
    if len(ids) < limit:                            # добор из ссылок на карточки
        for m in re.finditer(r"/product/(\d{3,})", html):
            pid = int(m.group(1))
            if pid not in ids:
                ids.append(pid)
            if len(ids) >= limit:
                break
    return ids[:limit]


def _collect_ids_from_state(obj: Any, into: list[int], limit: int, depth: int = 0) -> None:
    if len(into) >= limit or depth > 9:
        return
    if isinstance(obj, dict):
        for key in ("productId", "id"):
            v = obj.get(key)
            if isinstance(v, int) and v not in into:
                into.append(v)
            elif isinstance(v, str) and v.isdigit() and int(v) not in into:
                into.append(int(v))
        for v in obj.values():
            _collect_ids_from_state(v, into, limit, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _collect_ids_from_state(v, into, limit, depth + 1)


# --------------------------------------------------------------------------- #
#  Карточка товара — мобильный API каталога (чистый JSON, без браузерных редиректов)
# --------------------------------------------------------------------------- #
def _photo_link(p: Any) -> str | None:
    """Ссылка на изображение из элемента photos[] (строка или вложенный объект)."""
    if isinstance(p, str):
        return p
    if isinstance(p, dict):
        for k in ("link", "url", "src"):
            if isinstance(p.get(k), str):
                return p[k]
        nested = p.get("photo")
        if isinstance(nested, dict):
            for v in nested.values():
                if isinstance(v, str):
                    return v
                if isinstance(v, dict):
                    for vv in v.values():
                        if isinstance(vv, str):
                            return vv
    return None


async def fetch_product_card(product_id: int) -> dict[str, Any] | None:
    """Карточка товара из мобильного API: GET api.uzum.uz/api/v2/product/{id}.

    Эндпоинт отдаёт ЧИСТЫЙ JSON и не использует браузерные редиректы (в отличие от
    web-страницы uzum.uz/ru/product/{id}, которую WAF гонял в цикл редиректов).
    Запрос идёт через _make_client() с MOBILE_HEADERS (X-Platform: ANDROID и т.д.).

    Данные тянем безопасно через .get(); возвращаем «сырой» dict в формате, понятном
    normalize_uzum_card (списки photos/attributes считаются по длине). None — при
    401/403 (нужна подпись) или любой ошибке → хэндлер уйдёт на локальные метрики БД
    («Экспресс-аудит»), без сетевой ошибки редиректа.
    """
    data = await _get_json(f"{UZUM_PUBLIC_API_BASE}/product/{product_id}")
    if not isinstance(data, dict):
        return None
    payload = data.get("payload", {}) or {}
    photos = [_photo_link(p) for p in (payload.get("photos", []) or [])]
    return {
        "title": payload.get("title"),
        "rating": payload.get("rating", 0.0),
        "photos": photos,                            # список ссылок → длина = число фото
        "description": payload.get("description", "") or "",
        # Кол-во характеристик считаем по длине attributes (normalize_uzum_card).
        "attributes": payload.get("attributes", []) or [],
    }


async def fetch_product_cards(product_ids: list[int]) -> list[dict[str, Any]]:
    """Параллельно выкачать несколько карточек (конкуренция ограничена семафором)."""
    if not product_ids:
        return []
    results = await asyncio.gather(*(fetch_product_card(pid) for pid in product_ids))
    return [card for card in results if card]


__all__ = [
    "MOBILE_HEADERS",
    "CONCURRENCY_LIMITER",
    "search_competitors_on_uzum",
    "fetch_product_card",
    "fetch_product_cards",
]
