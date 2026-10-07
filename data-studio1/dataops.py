"""
ДВИЖОК РАБОТЫ С ДАННЫМИ
=======================
Всё чтение собранных данных идёт через этот модуль. Он ничего не знает про
недвижимость и работает с любой таблицей, которую создал сборщик.

Три принципа
------------
1. **Только чтение.** Соединение с redcat_data.db открывается в режиме
   `mode=ro` — SQLite физически не даст ничего записать, даже если в коде
   окажется опечатка. Пользовательские правки уходят в отдельную базу
   (studio_store.py) и накладываются поверх при выдаче.

2. **Имена колонок не склеиваются в SQL вслепую.** Любое имя таблицы и
   колонки сверяется с настоящей схемой через PRAGMA, значения уходят
   параметрами. Это закрывает SQL-инъекции при том, что фильтры приходят
   из браузера.

3. **Не тянуть всё в память.** Страница данных — это LIMIT/OFFSET, агрегаты
   считает сам SQLite. Приложение одинаково отзывчиво на 500 и на 500 000
   строк.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import statistics
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
            f"`python demo_data.py`, чтобы посмотреть интерфейс на примере.")
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


# ──────────────────────────────────────────────────────────────
#  ФИЛЬТРЫ
# ──────────────────────────────────────────────────────────────
def build_where(conn, table, filters, match="AND") -> tuple:
    """Собирает WHERE из списка фильтров. Возвращает (sql, параметры)."""
    if not filters:
        return "", []
    clauses, args = [], []
    for f in filters:
        field = f.get("field")
        op = f.get("op", "eq")
        if op not in OPERATORS:
            raise DataError(f"Неизвестный оператор фильтра: {op}")
        _check_columns(conn, table, [field])
        col, num = _col_sql(conn, table, field)
        template, arity = OPERATORS[op]
        values = f.get("values")
        if values is None:
            values = [] if arity == 0 else [f.get("value")]
        if not isinstance(values, list):
            values = [values]

        if arity == -1:  # IN / NOT IN
            if not values:
                continue
            marks = ",".join("?" for _ in values)
            clauses.append(template.format(col=col, marks=marks))
            args.extend(str(v) for v in values)
        else:
            if len(values) < arity:
                raise DataError(
                    f"Фильтру «{field} {OPERATOR_LABELS.get(op, op)}» "
                    f"не хватает значения.")
            clauses.append(template.format(col=col, num=num))
            args.extend(values[:arity])

    if not clauses:
        return "", []
    joiner = " OR " if str(match).upper() == "OR" else " AND "
    return "WHERE " + joiner.join(f"({c})" for c in clauses), args


def query(db_path, table, filters=None, match="AND", sort=None, desc=False,
          page=1, page_size=100, select=None, exclude_ids=None, id_field=None,
          only_ids=None) -> dict:
    """Страница данных с фильтрами и сортировкой."""
    page_size = max(1, min(int(page_size or 100), MAX_PAGE_SIZE))
    page = max(1, int(page or 1))

    with connect_ro(db_path) as conn:
        all_cols = [c["name"] for c in _columns(conn, table)]
        if select:
            _check_columns(conn, table, select)
            cols = list(select)
        else:
            cols = all_cols

        where, args = build_where(conn, table, filters or [], match)

        extra = []
        if exclude_ids and id_field:
            _check_columns(conn, table, [id_field])
            for chunk in _chunks(list(exclude_ids), 400):
                marks = ",".join("?" for _ in chunk)
                extra.append((f'CAST("{_safe_ident(id_field)}" AS TEXT) '
                              f'NOT IN ({marks})', [str(v) for v in chunk]))
        if only_ids is not None and id_field:
            _check_columns(conn, table, [id_field])
            ids = [str(v) for v in only_ids]
            if not ids:
                return {"table": table, "columns": cols, "rows": [], "total": 0,
                        "page": page, "page_size": page_size, "pages": 0}
            marks = ",".join("?" for _ in ids)
            extra.append((f'CAST("{_safe_ident(id_field)}" AS TEXT) IN ({marks})', ids))

        for clause, clause_args in extra:
            where = f"{where} AND ({clause})" if where else f"WHERE ({clause})"
            args = args + clause_args

        total = conn.execute(
            f'SELECT COUNT(*) FROM "{_safe_ident(table)}" {where}', args).fetchone()[0]

        order = ""
        if sort:
            _check_columns(conn, table, [sort])
            col, num = _col_sql(conn, table, sort)
            expr = num if _is_numeric(conn, table, sort) else col
            order = f'ORDER BY {expr} {"DESC" if desc else "ASC"}'

        col_sql = ", ".join(f'"{_safe_ident(c)}"' for c in cols)
        rows = conn.execute(
            f'SELECT {col_sql} FROM "{_safe_ident(table)}" {where} {order} '
            f'LIMIT ? OFFSET ?', args + [page_size, (page - 1) * page_size]
        ).fetchall()

    return {
        "table": table, "columns": cols, "rows": [dict(r) for r in rows],
        "total": total, "page": page, "page_size": page_size,
        "pages": max(1, -(-total // page_size)),
    }


def iter_all(db_path, table, filters=None, match="AND", select=None,
             sort=None, desc=False, limit=None):
    """Потоковый обход всех подходящих строк — для экспорта без загрузки в память."""
    with connect_ro(db_path) as conn:
        cols = select or [c["name"] for c in _columns(conn, table)]
        _check_columns(conn, table, cols)
        where, args = build_where(conn, table, filters or [], match)
        order = ""
        if sort:
            _check_columns(conn, table, [sort])
            order = f'ORDER BY "{_safe_ident(sort)}" {"DESC" if desc else "ASC"}'
        col_sql = ", ".join(f'"{_safe_ident(c)}"' for c in cols)
        sql = f'SELECT {col_sql} FROM "{_safe_ident(table)}" {where} {order}'
        if limit:
            sql += f" LIMIT {int(limit)}"
        yield cols
        for row in conn.execute(sql, args):
            yield dict(row)


# ──────────────────────────────────────────────────────────────
#  ФАСЕТЫ, СТАТИСТИКА, СВОДНЫЕ
# ──────────────────────────────────────────────────────────────
def facets(db_path, table, field, filters=None, match="AND", limit=40) -> list:
    """Частые значения колонки с количествами — для быстрых фильтров."""
    with connect_ro(db_path) as conn:
        _check_columns(conn, table, [field])
        where, args = build_where(conn, table, filters or [], match)
        rows = conn.execute(
            f'SELECT "{_safe_ident(field)}" AS value, COUNT(*) AS n '
            f'FROM "{_safe_ident(table)}" {where} GROUP BY 1 '
            f'ORDER BY n DESC LIMIT ?', args + [limit]).fetchall()
    return [{"value": r["value"], "count": r["n"]} for r in rows]


def column_stats(db_path, table, field, filters=None, match="AND") -> dict:
    """Профиль одной колонки: заполненность, уникальность, числовая сводка."""
    with connect_ro(db_path) as conn:
        _check_columns(conn, table, [field])
        where, args = build_where(conn, table, filters or [], match)
        col, num = _col_sql(conn, table, field)
        t = f'"{_safe_ident(table)}"'
        row = conn.execute(
            f"SELECT COUNT(*) total, "
            f"SUM(CASE WHEN {col} IS NULL OR TRIM(CAST({col} AS TEXT))='' "
            f"THEN 1 ELSE 0 END) empty, "
            f"COUNT(DISTINCT {col}) uniq, "
            f"MIN({num}) mn, MAX({num}) mx, AVG({num}) avg, "
            f"MEDIAN({num}) med, PCT25({num}) p25, PCT75({num}) p75 "
            f"FROM {t} {where}", args).fetchone()

    total = row["total"] or 0
    empty = row["empty"] or 0
    numeric = row["mn"] is not None
    return {
        "field": field, "total": total, "empty": empty,
        "fill_rate": round(100 * (total - empty) / total, 2) if total else 0,
        "unique": row["uniq"], "numeric": numeric,
        "min": row["mn"], "max": row["mx"],
        "avg": round(row["avg"], 2) if row["avg"] is not None else None,
        "median": row["med"], "p25": row["p25"], "p75": row["p75"],
    }


def pivot(db_path, table, dimensions, metric=None, agg="count",
          filters=None, match="AND", limit=100, sort_desc=True) -> dict:
    """Сводная таблица: разрез(ы) × агрегат. Считает SQLite, не Python."""
    if agg not in AGGREGATIONS:
        raise DataError(f"Неизвестная агрегация: {agg}")
    dimensions = [d for d in (dimensions or []) if d]
    if not dimensions:
        raise DataError("Укажите хотя бы один разрез (поле группировки).")

    with connect_ro(db_path) as conn:
        _check_columns(conn, table, dimensions)
        if agg == "count" and not metric:
            agg_sql = "COUNT(*)"
        else:
            if not metric:
                raise DataError(f"Для агрегации «{AGGREGATION_LABELS[agg]}» "
                                f"нужно выбрать поле.")
            _check_columns(conn, table, [metric])
            col, num = _col_sql(conn, table, metric)
            agg_sql = AGGREGATIONS[agg].format(col=col, num=num)

        where, args = build_where(conn, table, filters or [], match)
        dim_sql = ", ".join(f'"{_safe_ident(d)}"' for d in dimensions)
        rows = conn.execute(
            f'SELECT {dim_sql}, {agg_sql} AS value, COUNT(*) AS rows_count '
            f'FROM "{_safe_ident(table)}" {where} GROUP BY {dim_sql} '
            f'ORDER BY value {"DESC" if sort_desc else "ASC"} LIMIT ?',
            args + [limit]).fetchall()

    data = []
    for r in rows:
        item = {d: r[d] for d in dimensions}
        item["value"] = round(r["value"], 2) if isinstance(r["value"], float) else r["value"]
        item["rows_count"] = r["rows_count"]
        data.append(item)
    return {"dimensions": dimensions, "metric": metric, "agg": agg,
            "agg_label": AGGREGATION_LABELS[agg], "data": data}


def histogram(db_path, table, field, bins=20, filters=None, match="AND") -> dict:
    """Распределение числового поля — видно перекосы и хвосты."""
    with connect_ro(db_path) as conn:
        _check_columns(conn, table, [field])
        _, num = _col_sql(conn, table, field)
        where, args = build_where(conn, table, filters or [], match)
        t = f'"{_safe_ident(table)}"'
        bounds = conn.execute(
            f"SELECT MIN({num}) lo, MAX({num}) hi, COUNT({num}) n "
            f"FROM {t} {where}", args).fetchone()
        lo, hi, n = bounds["lo"], bounds["hi"], bounds["n"]
        if lo is None or n == 0:
            return {"field": field, "bins": [], "message":
                    f"В колонке «{field}» нет числовых значений."}
        if hi == lo:
            return {"field": field, "bins": [{"from": lo, "to": hi, "count": n}]}

        bins = max(4, min(int(bins), 60))
        width = (hi - lo) / bins
        rows = conn.execute(
            f"SELECT MIN(CAST(({num} - ?) / ? AS INTEGER), ?) AS bucket, "
            f"COUNT(*) AS n FROM {t} {where} "
            f"{'AND' if where else 'WHERE'} {num} IS NOT NULL "
            f"GROUP BY bucket ORDER BY bucket",
            args + [lo, width, bins - 1]).fetchall()

    counts = {r["bucket"]: r["n"] for r in rows}
    return {"field": field, "min": lo, "max": hi, "total": n,
            "bins": [{"from": round(lo + i * width, 2),
                      "to": round(lo + (i + 1) * width, 2),
                      "count": counts.get(i, 0)} for i in range(bins)]}


def duplicates(db_path, table, fields, filters=None, match="AND", limit=200) -> dict:
    """Записи с одинаковым значением ключа — классический источник двойного счёта."""
    fields = [f for f in (fields or []) if f]
    if not fields:
        raise DataError("Выберите поле, по которому искать дубликаты.")
    with connect_ro(db_path) as conn:
        _check_columns(conn, table, fields)
        where, args = build_where(conn, table, filters or [], match)
        key_sql = ", ".join(f'"{_safe_ident(f)}"' for f in fields)
        rows = conn.execute(
            f'SELECT {key_sql}, COUNT(*) AS n FROM "{_safe_ident(table)}" {where} '
            f'GROUP BY {key_sql} HAVING n > 1 ORDER BY n DESC LIMIT ?',
            args + [limit]).fetchall()
        groups = [dict(r) for r in rows]
        extra = conn.execute(
            f'SELECT COUNT(*) FROM (SELECT 1 FROM "{_safe_ident(table)}" {where} '
            f'GROUP BY {key_sql} HAVING COUNT(*) > 1)', args).fetchone()[0]
    return {"fields": fields, "groups": groups, "group_count": extra,
            "extra_rows": sum(g["n"] - 1 for g in groups)}


def outliers(db_path, table, field, filters=None, match="AND",
             threshold=3.5, limit=100, group_by=None, sample=20000) -> dict:
    """Выбросы по модифицированному z-score (медиана + MAD).

    Медиана и MAD выбраны вместо среднего и σ намеренно: одно экстремальное
    значение сдвигает среднее так, что настоящие выбросы перестают выделяться.
    Если задан разрез (group_by) — выброс ищется внутри группы, и тогда
    видно «дешёвый лот в дорогом ЖК», незаметный на фоне всего рынка.
    """
    with connect_ro(db_path) as conn:
        _check_columns(conn, table, [field])
        if group_by:
            _check_columns(conn, table, [group_by])
        _, num = _col_sql(conn, table, field)
        where, args = build_where(conn, table, filters or [], match)
        cols = [f"{num} AS _v", f'ROWID AS _rowid']
        if group_by:
            cols.append(f'"{_safe_ident(group_by)}" AS _g')
        rows = conn.execute(
            f'SELECT {", ".join(cols)} FROM "{_safe_ident(table)}" {where} '
            f'LIMIT ?', args + [sample]).fetchall()

    buckets: dict = {}
    for r in rows:
        if r["_v"] is None:
            continue
        key = r["_g"] if group_by else "__all__"
        buckets.setdefault(key, []).append((r["_rowid"], float(r["_v"])))

    found = []
    for key, items in buckets.items():
        values = [v for _, v in items]
        if len(values) < 8:
            continue
        med = statistics.median(values)
        mad = statistics.median([abs(v - med) for v in values])
        scale = 1.4826 * mad if mad else 0
        if not scale:
            continue
        for rowid, v in items:
            z = abs(v - med) / scale
            if z >= threshold:
                found.append({"rowid": rowid, "value": v, "z": round(z, 2),
                              "median": round(med, 2),
                              "group": None if key == "__all__" else key,
                              "direction": "выше" if v > med else "ниже"})
    found.sort(key=lambda x: -x["z"])
    return {"field": field, "group_by": group_by, "threshold": threshold,
            "checked": sum(len(v) for v in buckets.values()),
            "found": len(found), "items": found[:limit]}


def top_bottom(db_path, table, field, n=10, filters=None, match="AND",
               label_field=None) -> dict:
    """Топ и антитоп по числовому полю — быстрый взгляд на края распределения."""
    with connect_ro(db_path) as conn:
        _check_columns(conn, table, [field])
        cols = [field] + ([label_field] if label_field else [])
        _check_columns(conn, table, cols)
        _, num = _col_sql(conn, table, field)
        where, args = build_where(conn, table, filters or [], match)
        sel = ", ".join(f'"{_safe_ident(c)}"' for c in cols)
        base = (f'SELECT {sel}, {num} AS _v FROM "{_safe_ident(table)}" {where} '
                f'{"AND" if where else "WHERE"} {num} IS NOT NULL')
        top = conn.execute(f"{base} ORDER BY _v DESC LIMIT ?", args + [n]).fetchall()
        bottom = conn.execute(f"{base} ORDER BY _v ASC LIMIT ?", args + [n]).fetchall()
    return {"field": field, "top": [dict(r) for r in top],
            "bottom": [dict(r) for r in bottom]}


def crosstab(db_path, table, row_field, col_field, metric=None, agg="count",
             filters=None, match="AND", row_limit=25, col_limit=12) -> dict:
    """Таблица «строки × колонки»: например застройщик × тип отделки."""
    p = pivot(db_path, table, [row_field, col_field], metric, agg,
              filters, match, limit=row_limit * col_limit * 4)
    rows_order, cols_order, cells = [], [], {}
    for item in p["data"]:
        r, c = item[row_field], item[col_field]
        if r not in rows_order:
            rows_order.append(r)
        if c not in cols_order:
            cols_order.append(c)
        cells[(r, c)] = item["value"]
    rows_order = rows_order[:row_limit]
    cols_order = cols_order[:col_limit]
    matrix = [[cells.get((r, c)) for c in cols_order] for r in rows_order]
    return {"row_field": row_field, "col_field": col_field,
            "agg_label": p["agg_label"], "rows": rows_order,
            "cols": cols_order, "matrix": matrix}


# ──────────────────────────────────────────────────────────────
#  SQL-КОНСОЛЬ (строго SELECT)
# ──────────────────────────────────────────────────────────────
# Опасные команды. Ищем их как команды, а не как часть слова, иначе
# REPLACE(...) и sales_analyze ловятся как запрещённые.
# Команда должна стоять в начале утверждения, после ; или после (.
_FORBIDDEN_STMT = re.compile(
    r"(?im)(?:^|;|\()\s*("
    r"insert|update|delete|drop|alter|create|attach|detach|"
    r"vacuum|reindex|pragma|begin|commit|rollback|savepoint|release"
    r")\b"
)
# Модификатор INSERT OR REPLACE.
_FORBIDDEN_KEYWORD = re.compile(r"(?i)\breplace\s+into\b")


def _is_safe_sql(text: str) -> bool:
    """True — запрос безопасен (чтение). False — есть запрещённое."""
    stripped = re.sub(r"'[^']*'", "''", text)
    if _FORBIDDEN_STMT.search(stripped):
        return False
    if _FORBIDDEN_KEYWORD.search(stripped):
        return False
    return True



def run_sql(db_path, sql, limit=SQL_ROW_LIMIT) -> dict:
    """Выполняет пользовательский SELECT. Всё остальное отклоняется.

    Защит три, и они независимы: соединение открыто только на чтение,
    разрешено единственное выражение, и оно обязано начинаться с SELECT
    или WITH. Даже при обходе одной проверки остальные держат.
    """
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        raise DataError("Пустой запрос.")
    if ";" in text:
        raise DataError("Можно выполнить только один запрос за раз "
                        "(точка с запятой внутри не допускается).")
    if not re.match(r"^(select|with)\b", text, re.IGNORECASE):
        raise DataError("Разрешены только запросы SELECT / WITH — "
                        "приложение не изменяет собранные данные.")
    if not _is_safe_sql(text):
        raise DataError("В запросе есть изменяющая команда. "
                        "Доступно только чтение.")

    with connect_ro(db_path) as conn:
        try:
            cur = conn.execute(text)
            rows = cur.fetchmany(limit)
            cols = [d[0] for d in cur.description] if cur.description else []
        except sqlite3.Error as e:
            raise DataError(f"SQLite: {e}") from e
    return {"columns": cols, "rows": [dict(r) for r in rows],
            "truncated": len(rows) >= limit, "limit": limit}


# ──────────────────────────────────────────────────────────────
#  ВЫЧИСЛЯЕМЫЕ КОЛОНКИ (безопасная формула)
# ──────────────────────────────────────────────────────────────
_SAFE_EXPR = re.compile(r"^[\w\s.+\-*/()%<>=!]+$")


_DOUBLE_STAR = re.compile(r"\*\s*\*")
def validate_formula(expr: str, column_names) -> list:
    """Проверяет формулу до сохранения: символы, синтаксис, знакомые имена.

    Отдельно от eval_formula, потому что там проверить нельзя: на пустой
    строке любая формула честно падает на «нет такого имени», хотя сама
    формула правильная.
    """
    if _DOUBLE_STAR.search(expr or ""):
        raise DataError(
            "Оператор ** запрещён — вычисление слишком дорогое.")
    if not _SAFE_EXPR.match(expr or ""):
        raise DataError("В формуле есть недопустимые символы. Разрешены имена "
                        "колонок, числа и знаки + - * / ( ) % > < =")
    normalized = re.sub(r"\b([A-Za-z_]\w*)\.(\w+)", r"\1_\2", expr)
    try:
        compile(normalized, "<формула>", "eval")
    except SyntaxError as e:
        raise DataError(f"Синтаксическая ошибка в формуле: {e.msg}") from e
    known = {re.sub(r"\W", "_", c) for c in column_names}
    used = set(re.findall(r"[A-Za-z_]\w*", normalized))
    unknown = sorted(used - known)
    if unknown:
        raise DataError(f"В формуле нет таких колонок: {', '.join(unknown)}")
    return sorted(used & known)


def eval_formula(expr: str, row: dict):
    """Считает формулу вида `price / total_area` по значениям строки.

    Разрешены только имена колонок, числа и арифметика: никаких вызовов
    функций, импортов и доступа к атрибутам. Поэтому формулу безопасно
    принимать из браузера.
    """
    if _DOUBLE_STAR.search(expr or ""):
        raise DataError(
            "Оператор ** запрещён — вычисление слишком дорогое.")
    if not _SAFE_EXPR.match(expr or ""):
        raise DataError("В формуле есть недопустимые символы. Разрешены имена "
                        "колонок, числа и знаки + - * / ( ) % > < =")
    scope = {}
    for key, value in row.items():
        name = re.sub(r"\W", "_", key)
        try:
            scope[name] = float(str(value).replace("\xa0", "").replace(" ", "")
                                .replace(",", ".")) if value not in (None, "") else None
        except (TypeError, ValueError):
            scope[name] = None
    safe = re.sub(r"[^\w\s.+\-*/()%<>=!]", "", expr)
    safe = re.sub(r"\b([A-Za-z_]\w*)\.(\w+)", r"\1_\2", safe)
    try:
        return eval(safe, {"__builtins__": {}}, scope)  # noqa: S307 — выражение отфильтровано выше
    except ZeroDivisionError:
        return None
    except (NameError, TypeError, SyntaxError) as e:
        raise DataError(f"Формула не посчиталась: {e}") from e


# ──────────────────────────────────────────────────────────────
#  ЭКСПОРТ
# ──────────────────────────────────────────────────────────────
def to_csv(rows, columns) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore",
                            delimiter=";")
    writer.writeheader()
    for r in rows:
        writer.writerow({c: _flat(r.get(c)) for c in columns})
    return buf.getvalue()


def to_json(rows) -> str:
    return json.dumps(rows, ensure_ascii=False, indent=2, default=str)


def _flat(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


def group_summary(db_path, table, dimension, metric, filters=None, match="AND",
                  limit=60) -> dict:
    """Полная сводка по каждой группе одним запросом.

    Отличие от обычной сводной: там одна агрегация за раз, и чтобы понять
    группу, приходится строить её несколько раз. Здесь сразу количество,
    среднее, медиана, квартили и края — то есть видно не только «сколько», но
    и насколько группа однородна. Большой разрыв между медианой и средним —
    признак, что в группе есть перекос, и по среднему судить нельзя.
    """
    with connect_ro(db_path) as conn:
        _check_columns(conn, table, [dimension, metric])
        _, num = _col_sql(conn, table, metric)
        dim = f'"{_safe_ident(dimension)}"'
        where, args = build_where(conn, table, filters or [], match)
        rows = conn.execute(
            f'SELECT {dim} AS _g, COUNT(*) AS rows_count, '
            f'COUNT({num}) AS filled, AVG({num}) AS avg, MEDIAN({num}) AS median, '
            f'MIN({num}) AS min, MAX({num}) AS max, PCT25({num}) AS p25, '
            f'PCT75({num}) AS p75, SUM({num}) AS sum '
            f'FROM "{_safe_ident(table)}" {where} GROUP BY {dim} '
            f'ORDER BY rows_count DESC LIMIT ?', args + [limit]).fetchall()

    out = []
    for r in rows:
        item = {"group": r["_g"], "rows_count": r["rows_count"], "filled": r["filled"]}
        for key in ("avg", "median", "min", "max", "p25", "p75", "sum"):
            value = r[key]
            item[key] = round(value, 2) if isinstance(value, float) else value
        # разброс внутри группы: насколько середина отличается от краёв
        if item["p75"] is not None and item["p25"] is not None:
            item["iqr"] = round(item["p75"] - item["p25"], 2)
        out.append(item)
    return {"dimension": dimension, "metric": metric, "groups": out}


def correlation(db_path, table, x_field, y_field, filters=None, match="AND",
                sample=20000, points=400) -> dict:
    """Связь двух числовых полей: коэффициент Пирсона + точки для графика.

    Отвечает на вопрос «растёт ли одно вместе с другим»: например, растёт ли
    цена за м² с этажом. Важная оговорка, которую возвращаем вместе с числом:
    связь — это не причина. Совпадение динамики может объясняться третьим
    фактором, которого нет в этих двух колонках.
    """
    with connect_ro(db_path) as conn:
        _check_columns(conn, table, [x_field, y_field])
        _, xnum = _col_sql(conn, table, x_field)
        _, ynum = _col_sql(conn, table, y_field)
        where, args = build_where(conn, table, filters or [], match)
        rows = conn.execute(
            f'SELECT {xnum} AS x, {ynum} AS y FROM "{_safe_ident(table)}" {where} '
            f'LIMIT ?', args + [sample]).fetchall()

    pairs = [(float(r["x"]), float(r["y"])) for r in rows
             if r["x"] is not None and r["y"] is not None]
    if len(pairs) < 3:
        return {"x_field": x_field, "y_field": y_field, "n": len(pairs), "r": None,
                "message": "Слишком мало строк, где заполнены оба поля."}

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((a - mx) * (b - my) for a, b in pairs)
    den_x = sum((a - mx) ** 2 for a in xs)
    den_y = sum((b - my) ** 2 for b in ys)
    r = num / ((den_x * den_y) ** 0.5) if den_x and den_y else None

    # линия тренда (метод наименьших квадратов) — чтобы связь была видна глазом
    slope = num / den_x if den_x else None
    intercept = my - slope * mx if slope is not None else None

    step = max(1, len(pairs) // points)
    return {
        "x_field": x_field, "y_field": y_field, "n": len(pairs),
        "r": round(r, 4) if r is not None else None,
        "strength": _describe_correlation(r),
        "slope": slope, "intercept": intercept,
        "points": [{"x": a, "y": b} for a, b in pairs[::step]][:points],
    }


def _describe_correlation(r) -> str:
    if r is None:
        return "не определена"
    a = abs(r)
    direction = "прямая" if r > 0 else "обратная"
    if a < 0.2:
        return "связи практически нет"
    if a < 0.4:
        return f"слабая {direction} связь"
    if a < 0.6:
        return f"умеренная {direction} связь"
    if a < 0.8:
        return f"заметная {direction} связь"
    return f"сильная {direction} связь"


GRANULARITY = {
    "day": ("substr(CAST({col} AS TEXT), 1, 10)", "по дням"),
    "month": ("substr(CAST({col} AS TEXT), 1, 7)", "по месяцам"),
    "year": ("substr(CAST({col} AS TEXT), 1, 4)", "по годам"),
    "week": ("strftime('%Y-W%W', date(substr(CAST({col} AS TEXT), 1, 10)))", "по неделям"),
}


def timeseries(db_path, table, date_field, granularity="month", metric=None,
               agg="count", filters=None, match="AND", limit=400) -> dict:
    """Динамика внутри самих данных по колонке с датой.

    Это другое измерение времени, чем история запусков: там видно, как менялся
    снимок рынка от сбора к сбору, а здесь — как распределены сами записи по
    их собственной дате (когда лот появился, когда начал действовать регламент).
    """
    if granularity not in GRANULARITY:
        raise DataError(f"Неизвестная детализация: {granularity}")
    if agg not in AGGREGATIONS:
        raise DataError(f"Неизвестная агрегация: {agg}")

    with connect_ro(db_path) as conn:
        _check_columns(conn, table, [date_field])
        bucket = GRANULARITY[granularity][0].format(
            col=f'"{_safe_ident(date_field)}"')
        if agg == "count" and not metric:
            agg_sql = "COUNT(*)"
        else:
            if not metric:
                raise DataError(f"Для агрегации «{AGGREGATION_LABELS[agg]}» "
                                f"нужно выбрать поле.")
            _check_columns(conn, table, [metric])
            col, num = _col_sql(conn, table, metric)
            agg_sql = AGGREGATIONS[agg].format(col=col, num=num)

        where, args = build_where(conn, table, filters or [], match)
        tail = "AND" if where else "WHERE"
        rows = conn.execute(
            f'SELECT {bucket} AS period, {agg_sql} AS value, COUNT(*) AS rows_count '
            f'FROM "{_safe_ident(table)}" {where} {tail} {bucket} IS NOT NULL '
            f"AND {bucket} <> '' "
            f'GROUP BY period ORDER BY period LIMIT ?', args + [limit]).fetchall()

    data = [{"period": r["period"],
             "value": round(r["value"], 2) if isinstance(r["value"], float) else r["value"],
             "rows_count": r["rows_count"]} for r in rows]
    return {"date_field": date_field, "granularity": granularity,
            "granularity_label": GRANULARITY[granularity][1],
            "agg": agg, "agg_label": AGGREGATION_LABELS[agg],
            "metric": metric, "data": data}


def pareto(db_path, table, dimension, metric=None, agg="count",
           filters=None, match="AND", limit=200) -> dict:
    """ABC-анализ: какая доля групп даёт основную часть объёма.

    Практический смысл — понять, на чём сосредоточиться. Если 80% предложения
    приходится на 6 застройщиков из 40, то следить в первую очередь нужно за
    ними, а остальные можно проверять реже.
    """
    result = pivot(db_path, table, [dimension], metric, agg, filters, match,
                   limit=limit, sort_desc=True)
    rows = [r for r in result["data"]
            if isinstance(r["value"], (int, float)) and r["value"] > 0]
    total = sum(r["value"] for r in rows)
    if not total:
        return {"dimension": dimension, "total": 0, "data": [],
                "message": "Нет положительных значений для разбора."}

    cumulative = 0.0
    data = []
    a_count = b_count = 0
    for r in rows:
        cumulative += r["value"]
        share = 100 * r["value"] / total
        cum_share = 100 * cumulative / total
        if cum_share <= 80:
            group = "A"
            a_count += 1
        elif cum_share <= 95:
            group = "B"
            b_count += 1
        else:
            group = "C"
        data.append({dimension: r[dimension], "value": r["value"],
                     "share": round(share, 2), "cumulative": round(cum_share, 2),
                     "abc": group})
    return {"dimension": dimension, "metric": metric, "agg_label": result["agg_label"],
            "total": round(total, 2), "data": data,
            "a_count": a_count, "b_count": b_count,
            "c_count": len(data) - a_count - b_count,
            "summary": f"{a_count} из {len(data)} групп дают 80% объёма"}


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ──────────────────────────────────────────────────────────────
#  ДОКУМЕНТЫ ДЛЯ ПОИСКОВОГО ИНДЕКСА
# ──────────────────────────────────────────────────────────────
def iter_search_documents(db_path, specs_by_key=None, max_rows_per_table=200000):
    """Готовит (source, record_id, title, body) по всем таблицам для FTS."""
    specs_by_key = specs_by_key or {}
    with connect_ro(db_path) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
            if r[0] not in INTERNAL_TABLES and not r[0].startswith("sqlite_")]
        for table in tables:
            try:
                cols = [c["name"] for c in _columns(conn, table)]
            except DataError:
                continue
            spec = specs_by_key.get(table)
            id_field = (spec.id_field if spec and spec.id_field in cols
                        else ("id" if "id" in cols else cols[0]))
            name_field = (spec.name_field if spec and spec.name_field in cols
                          else next((c for c in cols if "name" in c.lower()
                                     or "title" in c.lower()), id_field))
            # в индекс идут текстовые колонки: числа ищут фильтрами, не поиском
            text_cols = [c["name"] for c in _columns(conn, table)
                         if not any(t in c["type"] for t in ("INT", "REAL", "FLOA"))]
            text_cols = text_cols[:40] or cols[:40]
            sel = ", ".join(f'"{_safe_ident(c)}"' for c in
                            dict.fromkeys([id_field, name_field] + text_cols))
            for row in conn.execute(
                    f'SELECT {sel} FROM "{_safe_ident(table)}" LIMIT ?',
                    (max_rows_per_table,)):
                d = dict(row)
                body = " ".join(str(v) for v in d.values() if v not in (None, ""))
                yield (table, str(d.get(id_field, "")),
                       str(d.get(name_field) or ""), body[:4000])
