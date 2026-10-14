"""web_routes_export — файлы и экспорт."""
from __future__ import annotations

from datetime import datetime
from redcat.data import dataops
from redcat.quality import completeness
from redcat.data import field_labels
from redcat.data import studio_store as store
from redcat.web.web_config import HISTORY_DIR, OUTPUT_DIR, STUDIO_DB
from redcat.web.web_crosscheck import _cross_check_report
from redcat.web.web_data import db_for_table, decorate, id_field_for, load_specs, resolve_sql_db


class ExportRoutes:
    """Файлы и экспорт."""

    def _files(self):
        items = []
        for folder in (OUTPUT_DIR, HISTORY_DIR):
            if not folder.exists():
                continue
            for path in sorted(folder.glob("*"),
                               key=lambda x: x.stat().st_mtime if x.is_file() else 0,
                               reverse=True):
                if path.is_file() and path.name != ".gitkeep":
                    items.append({
                        "name": path.name, "folder": folder.name,
                        "size": path.stat().st_size,
                        "modified": datetime.fromtimestamp(
                            path.stat().st_mtime).isoformat(timespec="seconds")})
        return items[:80]

    def _export(self, one, q):
        table = one("table")
        fmt = one("format", "csv")
        what = one("what", "table")
        stamp = datetime.now().strftime("%Y%m%d_%H%M")

        if what == "edits":
            rows = store.list_edits(STUDIO_DB, one("source") or None, limit=100000)
            cols = ["source", "record_id", "field", "old_value", "new_value",
                    "author", "note", "ts"]
            name = f"edits_{stamp}"

        elif what == "sql":
            db_path = resolve_sql_db(one("db", "redcat"))
            result = dataops.run_sql(db_path, one("sql", ""))
            rows, cols = result["rows"], result["columns"]
            name = f"sql_{one('db', 'redcat')}_{stamp}"

        elif what == "completeness_fields":
            spec = load_specs().get(table)
            db = db_for_table(table)
            fields = completeness.completeness_by_field(
                db, table, q.get("filters"), q.get("match", "AND"))
            for f in fields:
                f["kind"] = completeness._kind(f["field"], spec)
            rows = [{
                "поле": field_labels.label_for(f["field"]),
                "тип": f.get("kind", "other"),
                "всего": f.get("total"),
                "пусто": f.get("empty"),
                "заполнено_%": f.get("fill_rate"),
                "уникальных": f.get("unique"),
                "минимум": f.get("min"),
                "медиана": f.get("median"),
                "максимум": f.get("max"),
            } for f in fields]
            cols = ["поле", "тип", "всего", "пусто", "заполнено_%",
                    "уникальных", "минимум", "медиана", "максимум"]
            name = f"completeness_fields_{table}_{stamp}"

        elif what == "completeness_records":
            spec = load_specs().get(table)
            db = db_for_table(table)
            all_records = completeness.completeness_by_record(
                db, table, spec, q.get("filters"), q.get("match", "AND"))
            incomplete = [r for r in all_records if r["pct"] < 100]
            incomplete.sort(key=lambda r: r["pct"])
            rows = [{
                "запись": r["name"] or r["id"],
                "id": r["id"],
                "%": r["pct"],
                "не хватает полей": ", ".join(
                    field_labels.label_for(f) for f in r["missing"]),
            } for r in incomplete]
            cols = ["запись", "id", "%", "не хватает полей"]
            name = f"completeness_records_{table}_{stamp}"

        elif what == "cross_check":
            source = one("source", "")
            try:
                report = _cross_check_report(source)
            except dataops.DataError as e:
                return self._error(e)
            rows = []
            cls_titles = {"critical": "смотреть руками", "warn": "внимание",
                          "ok": "сходится", "insufficient": "мало данных",
                          "left_only": "только слева",
                          "right_only": "только справа"}
            for cls, items in report["items_by_class"].items():
                for it in items:
                    row = {
                        "класс": cls_titles.get(cls, cls),
                        "название": it.get("display"),
                        "строк слева": it.get("left_rows"),
                        "строк справа": it.get("right_rows"),
                    }
                    for lf, m in (it.get("metrics") or {}).items():
                        row[f"{lf} слева"] = m.get("left")
                        row[f"{lf} справа"] = m.get("right")
                        row[f"{lf} Δ%"] = m.get("diff_pct")
                    rows.append(row)
            cols = list(rows[0].keys()) if rows else ["класс", "название"]
            name = f"cross_check_{source}_{stamp}"

        else:
            db = db_for_table(table)
            stream = dataops.iter_all(db, table, q.get("filters"),
                                      q.get("match", "AND"), q.get("select"),
                                      q.get("sort"), bool(q.get("desc")))
            cols = next(stream)
            rows = list(stream)
            cols_meta = dataops.columns(db, table)
            rows = decorate(rows, table, id_field_for(table, cols_meta))
            name = f"{table}_{stamp}"

        if fmt == "json":
            body = dataops.to_json(rows).encode("utf-8")
            ctype, ext = "application/json; charset=utf-8", "json"
        else:
            body = ("\ufeff" + dataops.to_csv(rows, cols)).encode("utf-8")
            ctype, ext = "text/csv; charset=utf-8", "csv"
        self._send(body, 200, ctype, {
            "Content-Disposition": f'attachment; filename="{name}.{ext}"'})
