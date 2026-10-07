"""web_csv — разбор и конвертация CSV при загрузке."""
from __future__ import annotations

import re


_CSV_UPLOAD_STATE: dict = {}


def _csv_dedupe_idents(names: list) -> list:
    """Превращает произвольные заголовки CSV в безопасные и уникальные
    имена колонок SQLite: только буквы/цифры/подчёркивание, не с цифры,
    без повторов (второй «Цена» станет «Цена_2»)."""
    used = set()
    out = []
    for i, raw in enumerate(names):
        name = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_]+", "_", str(raw or "").strip())
        name = name.strip("_") or f"col_{i + 1}"
        if name[0].isdigit():
            name = f"c_{name}"
        base, n = name, 2
        while name in used:
            name = f"{base}_{n}"
            n += 1
        used.add(name)
        out.append(name)
    return out


def _csv_sniff_type(sample: list) -> str:
    """INTEGER/REAL/TEXT по образцу значений — чтобы загруженная колонка
    вела себя как собранная через API: сортировалась как число, попадала
    в распределения и выбросы, а не оставалась непрозрачным текстом."""
    if not sample:
        return "TEXT"

    def is_int(v):
        try:
            int(str(v).strip())
            return True
        except (TypeError, ValueError):
            return False

    def is_float(v):
        try:
            float(str(v).strip().replace(",", "."))
            return True
        except (TypeError, ValueError):
            return False

    if all(is_int(v) for v in sample):
        return "INTEGER"
    if all(is_float(v) for v in sample):
        return "REAL"
    return "TEXT"


def _csv_convert(value, sql_type):
    if value is None or value == "":
        return None
    if sql_type == "INTEGER":
        try:
            return int(str(value).strip())
        except ValueError:
            return None
    if sql_type == "REAL":
        try:
            return float(str(value).strip().replace(",", "."))
        except ValueError:
            return None
    return value


def _csv_pad_row(row: list, n: int) -> tuple:
    """Достраивает/обрезает строку до числа колонок — реальные CSV не
    всегда идеально прямоугольны (лишняя или недостающая ячейка в конце)."""
    row = list(row) + [None] * (n - len(row))
    return tuple(row[:n])
