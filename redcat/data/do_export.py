"""do_export — выгрузка в CSV/JSON и документы для поиска."""
from __future__ import annotations

import csv
import io
import json
from redcat.data.do_core import DataError, INTERNAL_TABLES, _columns, _flat, _safe_ident, connect_ro


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
