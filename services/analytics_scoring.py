"""Индекс Качества карточки товара (0–100) и нормализация публичной карточки Uzum.

Скоринг — чистая функция (без сети/БД), полностью тестируется. Параметры (макс 100):
  • Рейтинг (40):  >=4.8 → 40 · 4.5–4.7 → 25 · <4.5 → 10
  • Фото    (20):  >=5  → 20 · 2–4 → 10 · 1 → 2 · 0 → 0
  • Описание(20):  >300 → 20 · 100–300 → 10 · <100 → 0
  • Хар-ки  (20):  >5   → 20 · иначе 5
"""

from __future__ import annotations

from typing import Any


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def get_product_quality_score(product_data: dict[str, Any]) -> int:
    """Индекс Качества карточки по нормализованным метрикам (см. модульный docstring).

    Ожидает ключи: rating (float), photos_count (int), description_len (int),
    characteristics_count (int). Любой отсутствующий → трактуется как 0.
    """
    score = 0

    rating = _to_float(product_data.get("rating"))
    if rating >= 4.8:
        score += 40
    elif rating >= 4.5:
        score += 25
    else:                       # <4.5 (в т.ч. нет рейтинга) — по спецификации 10
        score += 10

    photos = _to_int(product_data.get("photos_count"))
    if photos >= 5:
        score += 20
    elif photos >= 2:
        score += 10
    elif photos == 1:
        score += 2              # 0 фото → 0

    desc_len = _to_int(product_data.get("description_len"))
    if desc_len > 300:
        score += 20
    elif desc_len >= 100:
        score += 10             # <100 → 0

    chars = _to_int(product_data.get("characteristics_count"))
    score += 20 if chars > 5 else 5

    return score


# --------------------------------------------------------------------------- #
#  Нормализация «сырой» публичной карточки Uzum → метрики скоринга
# --------------------------------------------------------------------------- #
def _count_list(*candidates: Any) -> int:
    """Длина первого значения-списка среди кандидатов (иначе 0)."""
    for c in candidates:
        if isinstance(c, list):
            return len(c)
    return 0


def _len_str(*candidates: Any) -> int:
    for c in candidates:
        if isinstance(c, str):
            return len(c.strip())
    return 0


def normalize_uzum_card(raw: dict[str, Any]) -> dict[str, Any]:
    """Привести «сырую» публичную карточку Uzum к метрикам скоринга (защитно).

    Публичная схема не зафиксирована — пробуем несколько типичных имён полей;
    чего нет — считаем нулём. Не бросает исключений.
    """
    if not isinstance(raw, dict):
        raw = {}

    rating = raw.get("rating")
    if isinstance(rating, dict):                       # иногда {"rating": 4.8, ...}
        rating = rating.get("rating") or rating.get("value")

    photos = _count_list(
        raw.get("photos"), raw.get("images"), raw.get("gallery"), raw.get("photoList")
    )
    chars = _count_list(
        raw.get("characteristics"), raw.get("attributes"),
        raw.get("characteristicsList"), raw.get("properties"),
    )
    desc_len = _len_str(
        raw.get("description"), raw.get("fullDescription"), raw.get("descriptionShort")
    )

    return {
        "title": raw.get("title") or raw.get("productTitle") or raw.get("name") or "—",
        "rating": _to_float(rating),
        "photos_count": photos,
        "description_len": desc_len,
        "characteristics_count": chars,
    }


__all__ = ["get_product_quality_score", "normalize_uzum_card"]
