"""
Словарь соответствий названий ЖК между внешними источниками и Redcat.
Читает hc_aliases.json из корня проекта. Кэширует по mtime.

Сама нормализация живёт в name_normalizer.py — здесь только словарь.
Ключи и значения файла при загрузке ПЕРЕНОРМАЛИЗУЮТСЯ, поэтому в JSON
можно писать как удобно: "ЖК Скай Гарден": "Sky Garden".
Ручной словарь нужен только для исключений — обычные пары
(кириллица↔латиница, дефисы, «корпус», «очередь») находятся автоматически,
см. name_normalizer.auto_aliases.

Если файл есть, но не читается как JSON, об этом пишется предупреждение
(раз на каждое изменение файла), а не молча возвращается пустой словарь.
"""

from __future__ import annotations

from redcat.core import paths
import json
import logging
import sys
from pathlib import Path

from redcat.sources import name_normalizer as nn

_FILE = paths.CONFIG_DIR / "hc_aliases.json"
_cache: dict | None = None
_mtime: float = 0.0

normalize = nn.normalize
_normalize = normalize          # обратная совместимость


def _warn(msg: str) -> None:
    text = f"⚠️ hc_aliases.json: {msg} — синонимы ЖК не применяются."
    logging.warning(text)
    print(text, file=sys.stderr)


def _read_raw() -> dict:
    """Читает словарь из файла. Отсутствие файла — норма, порча — предупреждение."""
    try:
        text = _FILE.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except OSError as e:
        _warn(f"не удалось прочитать ({e})")
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        _warn(f"это не JSON (строка {e.lineno}, колонка {e.colno}: {e.msg})")
        return {}
    raw = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        _warn("в файле нет объекта \"aliases\"")
        return {}
    return raw


def _load() -> dict:
    """Читает hc_aliases.json. Кэш сбрасывается по mtime файла."""
    global _cache, _mtime
    try:
        cur = _FILE.stat().st_mtime
    except OSError:
        cur = 0.0
    if _cache is None or cur != _mtime:
        raw = _read_raw()
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
