"""do_query — построение WHERE, постраничные выборки и полный обход таблицы."""
from __future__ import annotations

from redcat.data.do_core import DataError, MAX_PAGE_SIZE, OPERATORS, OPERATOR_LABELS, _check_columns, _chunks, _col_sql, _columns, _is_numeric, _safe_ident, connect_ro


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
