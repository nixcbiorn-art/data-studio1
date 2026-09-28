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


_ORDINALS = {
    "первый": "1", "второй": "2", "третий": "3", "четвертый": "4",
    "пятый": "5", "шестой": "6", "седьмой": "7", "восьмой": "8",
    "девятый": "9", "десятый": "10",
}

def normalize(raw) -> str | None:
    """Агрессивная нормализация названия ЖК для сопоставления.

    Приводит к одному виду:
      • "ЖК «Скай Гарден»"    → "скай гарден"
      • "1-й Измайловский"    → "1 измайловский"
      • "Первый Измайловский" → "1 измайловский"
      • "Архитектор (Москва)" → "архитектор"
      • "Sky  Garden"         → "sky garden"
    """
    if raw is None:
        return None
    t = str(raw).lower().strip()

    # Префиксы «ЖК», «мкр», «ЖК.»
    for prefix in ("жк ", "жк. ", "жк: ", "жк-", "жк «", "жк«", "мкр "):
        if t.startswith(prefix):
            t = t[len(prefix):]

    # Скобки — убираем вместе с содержимым: (Москва), [1-я очередь]
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)

    # ё → е, ъ → пусто
    t = t.replace("ё", "е").replace("ъ", "")

    # Корпуса и секции — отсекаем (корпус 3, стр. 1, литер А)
    t = _CORPUS_RE.sub(" ", t)

    # «1-й», «1ый», «1-я», «1ой» → «1»
    t = re.sub(r"\b(\d+)\s*[-–]?\s*(?:й|я|ый|ой|ий|ые|ых)\b", r"\1", t)
    # Числительные: «первый» → «1»
    for word, num in _ORDINALS.items():
        t = re.sub(rf"\b{word}\b", num, t)

    # Всё, что не буква/цифра — в пробел (кавычки, дефисы, запятые, точки)
    t = re.sub(r"[^a-zа-я0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()

    return t or None


# Обратная совместимость — старое имя функции
_normalize = normalize


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
