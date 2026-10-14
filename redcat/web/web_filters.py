"""web_filters — санитизация и применение фильтров запроса."""
from __future__ import annotations

from redcat.data import dataops


def _sanitize_filters(db_path, table, filters):
    """Возвращает (safe, dropped, virtual).

    safe     — фильтры по существующим колонкам,
    dropped  — имена колонок, которых нет (для лога),
    virtual  — фильтры по «виртуальным» колонкам, которые надо
               наложить особым способом. Сейчас поддерживается
               developer_name: если его нет в таблице, но есть
               housing_complex_id, фильтр применяется через JOIN
               с housing_complexes.
    """
    if not filters:
        return [], [], []
    try:
        cols = {c["name"] for c in dataops.columns(db_path, table)}
    except Exception:
        return list(filters), [], []
    safe, dropped, virtual = [], [], []
    for f in filters:
        if not isinstance(f, dict):
            continue
        fld = f.get("field")
        if not fld:
            continue
        if fld in cols:
            safe.append(f)
        elif fld == "developer_name" and "housing_complex_id" in cols:
            # developer_name → через JOIN с housing_complexes.
            virtual.append(f)
        else:
            dropped.append(fld)
    return safe, dropped, virtual


def _apply_virtual_filters(db_path, table, virtual):
    """Возвращает SQL-фрагмент WHERE для «виртуальных» фильтров.

    Сейчас: developer_name. Проверяем, что в базе есть таблица
    housing_complexes с колонками id и developer_name. Формируем
    EXISTS-подзапрос.
    """
    if not virtual:
        return "", []
    conn = dataops.connect_ro(db_path)
    try:
        hc_cols = {c["name"] for c in dataops.columns(db_path, "housing_complexes")}
    except Exception:
        conn.close()
        return "", []
    conn.close()

    if "id" not in hc_cols or "developer_name" not in hc_cols:
        return "", []

    clauses, args = [], []
    for f in virtual:
        if f.get("field") != "developer_name":
            continue
        op = str(f.get("op") or "eq").lower()
        val = str(f.get("value") or "")
        if not val:
            continue
        if op == "eq":
            sql_op, sql_val = "=", val
        elif op == "contains":
            sql_op, sql_val = "LIKE", f"%{val}%"
        elif op == "starts":
            sql_op, sql_val = "LIKE", f"{val}%"
        elif op == "ends":
            sql_op, sql_val = "LIKE", f"%{val}"
        elif op == "ne":
            sql_op, sql_val = "<>", val
        else:
            continue

        clauses.append(
            f'EXISTS (SELECT 1 FROM "housing_complexes" hc '
            f'WHERE CAST(hc."id" AS TEXT) = '
            f'CAST("{table}"."housing_complex_id" AS TEXT) '
            f'AND hc."developer_name" {sql_op} ?)'
        )
        args.append(sql_val)
    return " AND ".join(clauses), args
