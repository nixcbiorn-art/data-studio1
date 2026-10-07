"""do_core — константы, DataError, подключение только для чтения, схема таблиц и безопасные идентификаторы."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


# Операторы фильтра: код → (шаблон SQL, сколько значений нужно)
OPERATORS = {
    "eq":         ("{col} = ?", 1),
    "ne":         ("{col} <> ?", 1),
    "gt":         ("{num} > ?", 1),
    "gte":        ("{num} >= ?", 1),
    "lt":         ("{num} < ?", 1),
    "lte":        ("{num} <= ?", 1),
    "between":    ("{num} BETWEEN ? AND ?", 2),
    "contains":   ("{col} LIKE '%' || ? || '%'", 1),
    "notcontains": ("({col} IS NULL OR {col} NOT LIKE '%' || ? || '%')", 1),
    "starts":     ("{col} LIKE ? || '%'", 1),
    "ends":       ("{col} LIKE '%' || ?", 1),
    "empty":      ("({col} IS NULL OR TRIM(CAST({col} AS TEXT)) = '')", 0),
    "notempty":   ("({col} IS NOT NULL AND TRIM(CAST({col} AS TEXT)) <> '')", 0),
    "in":         ("{col} IN ({marks})", -1),
    "notin":      ("{col} NOT IN ({marks})", -1),
    "regex":      ("{col} REGEXP ?", 1),
}


OPERATOR_LABELS = {
    "eq": "равно", "ne": "не равно", "gt": "больше", "gte": "больше или равно",
    "lt": "меньше", "lte": "меньше или равно", "between": "в диапазоне",
    "contains": "содержит", "notcontains": "не содержит", "starts": "начинается с",
    "ends": "заканчивается на", "empty": "пусто", "notempty": "не пусто",
    "in": "один из", "notin": "ни один из", "regex": "рег. выражение",
}


AGGREGATIONS = {
    "count": "COUNT({col})", "count_distinct": "COUNT(DISTINCT {col})",
    "sum": "SUM({num})", "avg": "AVG({num})", "min": "MIN({num})",
    "max": "MAX({num})", "median": "MEDIAN({num})",
    "p25": "PCT25({num})", "p75": "PCT75({num})",
}


AGGREGATION_LABELS = {
    "count": "количество", "count_distinct": "уникальных", "sum": "сумма",
    "avg": "среднее", "min": "минимум", "max": "максимум",
    "median": "медиана", "p25": "25-й перцентиль", "p75": "75-й перцентиль",
}


MAX_PAGE_SIZE = 500


SQL_ROW_LIMIT = 5000


# Таблицы, которые созданы сборщиком для себя, а не для просмотра
INTERNAL_TABLES = {"sqlite_sequence"}


class DataError(Exception):
    """Понятная пользователю ошибка запроса."""


# ──────────────────────────────────────────────────────────────
#  ПОДКЛЮЧЕНИЕ
# ──────────────────────────────────────────────────────────────
class _Percentile:
    """Агрегат-перцентиль: SQLite его не умеет, добавляем сами."""
    q = 0.5

    def __init__(self):
        self.values = []

    def step(self, value):
        if value is None:
            return
        try:
            self.values.append(float(value))
        except (TypeError, ValueError):
            pass

    def finalize(self):
        if not self.values:
            return None
        self.values.sort()
        k = (len(self.values) - 1) * self.q
        lo, hi = int(k), min(int(k) + 1, len(self.values) - 1)
        return self.values[lo] + (self.values[hi] - self.values[lo]) * (k - lo)


class _Median(_Percentile):
    q = 0.5


class _P25(_Percentile):
    q = 0.25


class _P75(_Percentile):
    q = 0.75


def _regexp(pattern, value):
    if value is None:
        return False
    try:
        return re.search(pattern, str(value), re.IGNORECASE) is not None
    except re.error:
        return False


def connect_ro(db_path) -> sqlite3.Connection:
    """Соединение ТОЛЬКО ДЛЯ ЧТЕНИЯ. Запись отвергает сам SQLite."""
    path = Path(db_path)
    if not path.exists():
        raise DataError(
            f"База данных не найдена: {path.name}. Сначала соберите данные "
            f"(вкладка «Сбор» → «Запустить сбор») или запустите "
            f"`python -m redcat.tools.demo_data`, чтобы посмотреть интерфейс на примере.")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.create_function("REGEXP", 2, _regexp)
    conn.create_aggregate("MEDIAN", 1, _Median)
    conn.create_aggregate("PCT25", 1, _P25)
    conn.create_aggregate("PCT75", 1, _P75)
    return conn


# ──────────────────────────────────────────────────────────────
#  СХЕМА
# ──────────────────────────────────────────────────────────────
def list_tables(db_path) -> list:
    with connect_ro(db_path) as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            if r[0] not in INTERNAL_TABLES and not r[0].startswith("sqlite_")]
        out = []
        for name in names:
            try:
                count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except sqlite3.Error:
                count = 0
            out.append({"name": name, "rows": count,
                        "columns": len(_columns(conn, name))})
        return out


def _columns(conn, table) -> list:
    rows = conn.execute(f'PRAGMA table_info("{_safe_ident(table)}")').fetchall()
    if not rows:
        raise DataError(f"Таблицы «{table}» нет в базе.")
    return [{"name": r["name"], "type": (r["type"] or "").upper()} for r in rows]


def columns(db_path, table) -> list:
    with connect_ro(db_path) as conn:
        return _columns(conn, table)


def _safe_ident(name) -> str:
    """Пропускает только настоящие идентификаторы. Кавычки внутри запрещены."""
    if not isinstance(name, str) or not name or '"' in name or "\x00" in name:
        raise DataError(f"Недопустимое имя: {name!r}")
    return name


def _check_columns(conn, table, names) -> None:
    known = {c["name"] for c in _columns(conn, table)}
    for n in names:
        if n not in known:
            raise DataError(
                f"В таблице «{table}» нет колонки «{n}». "
                f"Возможно, структура данных изменилась после пересбора.")


def _is_numeric(conn, table, column) -> bool:
    for c in _columns(conn, table):
        if c["name"] == column:
            return any(t in c["type"] for t in ("INT", "REAL", "NUM", "FLOA", "DOUB"))
    return False


def _col_sql(conn, table, column) -> tuple:
    """Возвращает (обычное_выражение, числовое_выражение) для колонки.

    Числовой вариант нужен потому, что API часто отдаёт числа строками
    («12 500 000»): без приведения сравнение «больше» работало бы
    лексикографически и врало.
    """
    ident = f'"{_safe_ident(column)}"'
    if _is_numeric(conn, table, column):
        return ident, ident
    num = (f"CAST(REPLACE(REPLACE(REPLACE(CAST({ident} AS TEXT),"
           f" char(160), ''), ' ', ''), ',', '.') AS REAL)")
    return ident, num


def _flat(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
