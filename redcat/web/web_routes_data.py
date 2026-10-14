"""web_routes_data — метаданные, выборки, записи, массовая правка, связи."""
from __future__ import annotations

import re
from datetime import datetime
from redcat.core import api_guard
from redcat.data import dataops
from redcat.sources import hc_aliases
from redcat.data import studio_store as store
from redcat.web.web_config import DATA_DB, EXTERNAL_DB, STUDIO_DB
from redcat.web.web_data import db_for_table, decorate, id_field_for, load_specs, name_field_for, runs_history
from redcat.web.web_telegram import token_status


class DataRoutes:
    """Метаданные, выборки, записи, массовая правка, связи."""

    # ---------- реализации ----------
    def _meta(self):
        redcat_ok = DATA_DB.exists()
        ext_ok = EXTERNAL_DB.exists()
        redcat_tables = dataops.list_tables(DATA_DB) if redcat_ok else []
        external_tables = ([t for t in dataops.list_tables(EXTERNAL_DB)]
                           if ext_ok else [])
        external_tables = [t for t in external_tables
                           if t["name"] != "comparison_vs_previous"]
        specs = load_specs()
        ext_names = {t["name"] for t in external_tables}
        for t in redcat_tables + external_tables:
            spec = specs.get(t["name"])
            t["title"] = spec.title if spec else ""
            t["external"] = t["name"] in ext_names
        all_tables = redcat_tables + external_tables
        return {
            "tables": all_tables,
            "redcat_tables": redcat_tables,
            "external_tables": external_tables,
            "data_db": str(DATA_DB), "data_ok": redcat_ok,
            "external_db": str(EXTERNAL_DB), "external_ok": ext_ok,
            "studio": store.stats(STUDIO_DB),
            "edit_counts": store.edit_counts(STUDIO_DB),
            "token": token_status(),
            "guard": {"text": api_guard.status_text(),
                      "allowed": sorted(api_guard.ALLOWED_METHODS),
                      "blocked": api_guard.blocked_count()},
            "search": store.search_meta(STUDIO_DB),
            "sources": [{"key": s.key, "title": s.title,
                         "split": bool(s.split_param),
                         "external": bool(s.external),
                         "browser": getattr(s, "fetch_mode", "http") == "browser",
                         "has_cross_check": bool(getattr(s, "cross_check", None))}
                        for s in specs.values()],
            "aliases_total": hc_aliases.count(),
            "runs": len(runs_history(limit=500)),
            "server_time": datetime.now().isoformat(timespec="seconds"),
        }

    def _query(self, table, q):
        db = db_for_table(table)
        cols = dataops.columns(db, table)
        id_field = id_field_for(table, cols)
        exclude = None
        if not q.get("include_hidden"):
            exclude = store.flagged_ids(STUDIO_DB, table, store.FLAG_HIDDEN)
        only_ids = None
        if q.get("only") == "starred":
            only_ids = store.flagged_ids(STUDIO_DB, table, store.FLAG_STAR)
        elif q.get("only") == "edited":
            only_ids = store.edited_ids(STUDIO_DB, table)

        result = dataops.query(
            db, table, q.get("filters"), q.get("match", "AND"),
            q.get("sort"), bool(q.get("desc")), int(q.get("page", 1)),
            int(q.get("page_size", 100)), q.get("select"),
            exclude_ids=exclude, id_field=id_field, only_ids=only_ids)
        result["rows"] = decorate(result["rows"], table, id_field)
        result["id_field"] = id_field
        result["name_field"] = name_field_for(table, cols)

        for c in store.list_computed(STUDIO_DB, table):
            for row in result["rows"]:
                try:
                    row[c["name"]] = dataops.eval_formula(c["expr"], row)
                except dataops.DataError:
                    row[c["name"]] = None
            if c["name"] not in result["columns"]:
                result["columns"].append(c["name"])
        return result

    def _record(self, table, record_id):
        db = db_for_table(table)
        cols = dataops.columns(db, table)
        id_field = id_field_for(table, cols)
        res = dataops.query(db, table,
                            [{"field": id_field, "op": "eq", "value": record_id}],
                            page_size=1)
        if not res["rows"]:
            raise dataops.DataError(f"Запись {record_id} не найдена в «{table}».")
        row = decorate(res["rows"], table, id_field)[0]
        spec = load_specs().get(table)
        return {
            "table": table, "id_field": id_field, "row": row,
            "edits": store.edit_history(STUDIO_DB, table, record_id),
            "numeric_fields": (spec.numeric_fields if spec else
                               [c["name"] for c in cols
                                if any(t in c["type"]
                                       for t in ("INT", "REAL"))][:8]),
            "columns": cols,
        }

    def _bulk_edit(self, b):
        table, field = b["table"], b["field"]
        db = db_for_table(table)
        cols = dataops.columns(db, table)
        id_field = id_field_for(table, cols)
        mode = b.get("mode", "set")
        limit = int(b.get("limit", 5000))

        stream = dataops.iter_all(db, table, b.get("filters"),
                                  b.get("match", "AND"),
                                  select=[id_field, field], limit=limit)
        next(stream)
        rows = list(stream)

        pairs = []
        for row in rows:
            rid, old = row.get(id_field), row.get(field)
            if mode == "set":
                new = b.get("value")
            elif mode == "replace":
                if old is None:
                    continue
                new = str(old).replace(b.get("find", ""), b.get("value", ""))
            elif mode == "regex":
                if old is None:
                    continue
                try:
                    new = re.sub(b.get("find", ""), b.get("value", ""), str(old))
                except re.error as e:
                    raise dataops.DataError(
                        f"Ошибка в регулярном выражении: {e}") from e
            elif mode == "trim":
                new = str(old).strip() if old is not None else old
            elif mode == "formula":
                new = dataops.eval_formula(b.get("value", ""), row)
            else:
                raise dataops.DataError(f"Неизвестный режим правки: {mode}")
            if str(new) != str(old):
                pairs.append((rid, old, new))

        return store.set_edits_bulk(STUDIO_DB, table, field, pairs,
                                    b.get("author", ""),
                                    b.get("note", "массовая правка"))

    def _related(self, table, record_id):
        from redcat.quality import crosschecks
        specs = load_specs()
        relations = crosschecks.detect_relations(DATA_DB, specs)
        out = []
        db = db_for_table(table)

        for rel in relations:
            if rel["child"] == table:
                cols = dataops.columns(db, table)
                res = dataops.query(
                    db, table,
                    [{"field": id_field_for(table, cols), "op": "eq",
                      "value": record_id}], page_size=1)
                if not res["rows"]:
                    continue
                value = res["rows"][0].get(rel["child_field"])
                if value is None:
                    continue
                parent_db = db_for_table(rel["parent"])
                parent = dataops.query(
                    parent_db, rel["parent"],
                    [{"field": rel["parent_field"], "op": "eq",
                      "value": value}], page_size=5)
                if parent["rows"]:
                    out.append({
                        "direction": "parent", "table": rel["parent"],
                        "title": f"Справочник «{rel['parent']}»",
                        "count": parent["total"], "rows": parent["rows"][:5],
                        "id_field": rel["parent_field"], "link_value": value})

            if rel["parent"] == table:
                child_db = db_for_table(rel["child"])
                child = dataops.query(
                    child_db, rel["child"],
                    [{"field": rel["child_field"], "op": "eq",
                      "value": record_id}], page_size=5)
                out.append({
                    "direction": "child", "table": rel["child"],
                    "title": f"Связанные записи в «{rel['child']}»",
                    "count": child["total"], "rows": child["rows"][:5],
                    "id_field": rel["child_field"], "link_value": record_id,
                    "filter_field": rel["child_field"]})
        return {"table": table, "id": record_id, "groups": out}
