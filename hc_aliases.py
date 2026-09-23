"""
Словарь соответствий названий ЖК между внешними источниками и Redcat.
Читает hc_aliases.json из корня проекта. Кэширует по mtime.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_FILE = Path(__file__).resolve().parent / "hc_aliases.json"
_cache: dict | None = None
_mtime: float = 0.0

_CORPUS_RE = re.compile(
    r"\b(корпус|корп\.?|к\.?|строение|стр\.?|литер[аы]?|секц(ия|\.)?)\s*\d+\b",
    re.IGNORECASE)


def _normalize(raw) -> str | None:
    if raw is None:
        return None
    t = str(raw).lower().strip()
    for prefix in ("жк ", "жк. ", "жк: ", "жк-", "мкр "):
        if t.startswith(prefix):
            t = t[len(prefix):]
    t = t.replace("ё", "е")
    t = _CORPUS_RE.sub(" ", t)
    t = re.sub(r"[^a-zа-я0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or None


def _load() -> dict:
    """Читает hc_aliases.json. Кэш сбрасывается по mtime файла."""
    global _cache, _mtime
    try:
        cur = _FILE.stat().st_mtime
    except OSError:
        cur = 0.0
    if _cache is None or cur != _mtime:
        try:
            data = json.loads(_FILE.read_text(encoding="utf-8"))
            _cache = data.get("aliases") or {}
        except (OSError, json.JSONDecodeError):
            _cache = {}
        _mtime = cur
    return _cache


def apply(raw) -> str | None:
    """Нормализует имя и подставляет канон из словаря, если есть."""
    n = _normalize(raw)
    if n is None:
        return None
    return _load().get(n, n)


def count() -> int:
    return len(_load())
