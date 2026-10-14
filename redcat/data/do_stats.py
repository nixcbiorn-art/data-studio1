"""do_stats — фасеты, статистика колонок, сводные, гистограммы, дубли, выбросы, кросс-таблицы."""
from __future__ import annotations

import statistics
from redcat.data.do_core import AGGREGATIONS, AGGREGATION_LABELS, DataError, _check_columns, _col_sql, _safe_ident, connect_ro
from redcat.data.do_query import build_where


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
