"""
Словарь соответствий названий ЖК между внешними источниками и Redcat.
Читает hc_aliases.json из корня проекта. Кэширует по mtime.

Сама нормализация живёт в name_normalizer.py — здесь только словарь.
Ключи и значения файла при загрузке ПЕРЕНОРМАЛИЗУЮТСЯ, поэтому в JSON
можно писать как удобно: "ЖК Скай Гарден": "Sky Garden".
Ручной словарь нужен только для исключений — обычные пары
(кириллица↔латиница, дефисы, «корпус», «очередь») находятся автоматически,
см. name_normalizer.auto_aliases.
"""

from __future__ import annotations

import json
from pathlib import Path

import name_normalizer as nn

_FILE = Path(__file__).resolve().parent / "hc_aliases.json"
_cache: dict | None = None
_mtime: float = 0.0

normalize = nn.normalize
_normalize = normalize          # обратная совместимость


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
            raw = data.get("aliases") or {}
        except (OSError, json.JSONDecodeError):
            raw = {}
        _cache = {}
        for k, v in raw.items():
            nk, nv = nn.normalize(k), nn.normalize(v)
            if nk and nv and nk != nv:
                _cache[nk] = nv
        _mtime = cur
    return _cache


def apply(raw) -> str | None:
    """Нормализует имя и подставляет канон из словаря, если есть."""
    return nn.canon(raw, _load())


def count() -> int:
    return len(_load())
