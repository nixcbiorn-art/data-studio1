"""web_routes_get — маршруты GET /api/*."""
from __future__ import annotations

from pathlib import Path
from redcat.core import api_guard
from redcat.data import dataops
from redcat.quality import completeness
from redcat.data import field_labels
from redcat.data import studio_store as store
from redcat.web.web_config import DATA_DB, SOURCES_DIR, SOURCES_EXT_DIR, STUDIO_DB
from redcat.web.web_crosscheck import _cross_check_report
from redcat.web.web_data import anomalies_list, db_for_table, id_field_for, load_specs, name_field_for, parse_query, record_series, resolve_sql_db, runs_history, source_files, sql_databases
from redcat.web.web_runners import RUNNER, TOOLS_RUNNER, tools_list
from redcat.web.web_telegram import telegram_status


class GetRoutes:
    """Маршруты GET /api/*."""

    def _api_get(self, route, p):
        one = lambda k, d=None: p.get(k, [d])[0]  # noqa: E731
        q = parse_query(p)

        if route == "meta":
            return self._send(self._meta())

        if route == "tools":
            return self._send({"items": tools_list()})

        if route == "tool_state":
            return self._send(TOOLS_RUNNER.state(int(one("since", 0))))

        if route == "telegram_status":
            return self._send(telegram_status())

        if route == "tools":
            return self._send({"items": tools_list()})

        if route == "tool_state":
            return self._send(TOOLS_RUNNER.state(int(one("since", 0))))

        if route == "telegram_status":
            return self._send(telegram_status())

        if route == "columns":
            table = one("table")
            cols = dataops.columns(db_for_table(table), table)
            cols = field_labels.apply_to_columns(cols)
            computed = store.list_computed(STUDIO_DB, table)
            return self._send({
                "columns": cols, "id_field": id_field_for(table, cols),
                "name_field": name_field_for(table, cols), "computed": computed})

        if route == "query":
            return self._send(self._query(one("table"), q))

        if route == "facets":
            table = one("table")
            return self._send({"values": dataops.facets(
                db_for_table(table), table, one("field"), q.get("filters"),
                q.get("match", "AND"), int(one("limit", 40)))})

        if route == "stats":
            table = one("table")
            return self._send(dataops.column_stats(
                db_for_table(table), table, one("field"),
                q.get("filters"), q.get("match", "AND")))

        if route == "profile":
            table = one("table")
            db = db_for_table(table)
            cols = [c["name"] for c in dataops.columns(db, table)]
            stats = [dataops.column_stats(db, table, c, q.get("filters"),
                                          q.get("match", "AND")) for c in cols]
            for s in stats:
                s["label"] = field_labels.label_for(s["field"])
            return self._send({"columns": stats})

        if route == "pivot":
            table = one("table")
            dims = [d for d in (one("dimensions", "") or "").split(",") if d]
            return self._send(dataops.pivot(
                db_for_table(table), table, dims, one("metric") or None,
                one("agg", "count"), q.get("filters"), q.get("match", "AND"),
                int(one("limit", 100))))

        if route == "crosstab":
            table = one("table")
            return self._send(dataops.crosstab(
                db_for_table(table), table, one("row_field"), one("col_field"),
                one("metric") or None, one("agg", "count"),
                q.get("filters"), q.get("match", "AND")))

        if route == "histogram":
            table = one("table")
            return self._send(dataops.histogram(
                db_for_table(table), table, one("field"), int(one("bins", 20)),
                q.get("filters"), q.get("match", "AND")))

        if route == "duplicates":
            table = one("table")
            fields = [f for f in (one("fields", "") or "").split(",") if f]
            return self._send(dataops.duplicates(
                db_for_table(table), table, fields,
                q.get("filters"), q.get("match", "AND")))

        if route == "outliers":
            table = one("table")
            return self._send(dataops.outliers(
                db_for_table(table), table, one("field"), q.get("filters"),
                q.get("match", "AND"), float(one("threshold", 3.5)),
                group_by=one("group_by") or None))

        if route == "topbottom":
            table = one("table")
            return self._send(dataops.top_bottom(
                db_for_table(table), table, one("field"), int(one("n", 10)),
                q.get("filters"), q.get("match", "AND"),
                one("label_field") or None))

        if route == "group_summary":
            table = one("table")
            return self._send(dataops.group_summary(
                db_for_table(table), table, one("dimension"), one("metric"),
                q.get("filters"), q.get("match", "AND")))

        if route == "correlation":
            table = one("table")
            return self._send(dataops.correlation(
                db_for_table(table), table, one("x"), one("y"),
                q.get("filters"), q.get("match", "AND")))

        if route == "timeseries":
            table = one("table")
            return self._send(dataops.timeseries(
                db_for_table(table), table, one("date_field"),
                one("granularity", "month"), one("metric") or None,
                one("agg", "count"), q.get("filters"), q.get("match", "AND")))

        if route == "pareto":
            table = one("table")
            return self._send(dataops.pareto(
                db_for_table(table), table, one("dimension"),
                one("metric") or None, one("agg", "count"),
                q.get("filters"), q.get("match", "AND")))

        if route == "sql":
            db_path = resolve_sql_db(one("db", "redcat"))
            return self._send(dataops.run_sql(db_path, one("sql", "")))

        if route == "sql_databases":
            return self._send({"items": sql_databases()})

        if route == "sql_schema":
            db_path = resolve_sql_db(one("db", "redcat"))
            out = []
            for t in dataops.list_tables(db_path):
                item = {"name": t["name"], "rows": t["rows"], "schema": []}
                try:
                    item["schema"] = [
                        {"name": c["name"], "type": (c.get("type") or "").upper()}
                        for c in dataops.columns(db_path, t["name"])
                    ]
                except dataops.DataError:
                    pass
                out.append(item)
            return self._send({"items": out})

        if route == "search":
            return self._send({
                "results": store.search(STUDIO_DB, one("q", ""),
                                        one("source") or None,
                                        int(one("limit", 100))),
                "meta": store.search_meta(STUDIO_DB)})

        if route == "record":
            return self._send(self._record(one("table"), one("id")))

        if route == "record_series":
            return self._send({"series": record_series(
                one("source"), one("id"), one("field"))})

        if route == "crosschecks":
            from redcat.quality import crosschecks
            history = runs_history(limit=1)
            return self._send(crosschecks.run(
                DATA_DB, load_specs(), history[-1] if history else None))

        if route == "cross_check":
            source = one("source")
            if not source:
                return self._error("Укажите ?source=<ключ>")
            try:
                return self._send(_cross_check_report(source))
            except dataops.DataError as e:
                return self._error(e)

        if route == "related":
            return self._send(self._related(one("table"), one("id")))

        if route == "runs":
            return self._send({"runs": runs_history()})

        if route == "anomalies":
            return self._send({"items": anomalies_list(
                one("severity") or None, one("source") or None,
                one("kind") or None)})

        if route == "edits":
            return self._send({"items": store.list_edits(
                STUDIO_DB, one("source") or None)})

        if route == "edit_history":
            return self._send({"items": store.edit_history(
                STUDIO_DB, one("source"), one("id"))})

        if route == "views":
            return self._send({"items": store.list_views(
                STUDIO_DB, one("source") or None)})

        if route == "tags":
            return self._send({"items": store.all_tags(
                STUDIO_DB, one("source") or None)})

        if route == "apilog":
            return self._send({
                "items": store.list_api_log(STUDIO_DB, int(one("limit", 300)),
                                            one("blocked") == "1"),
                "summary": store.api_log_summary(STUDIO_DB),
                "guard": api_guard.status_text(),
                "allowed": sorted(api_guard.ALLOWED_METHODS)})

        if route == "sources":
            items = source_files()
            hint = ""
            if not items:
                parts = []
                if not SOURCES_DIR.exists():
                    parts.append(f"sources/ не найдена ({SOURCES_DIR})")
                else:
                    n1 = len(list(SOURCES_DIR.glob("*.json")))
                    if n1:
                        parts.append(f"в sources/ есть {n1} *.json")
                if not SOURCES_EXT_DIR.exists():
                    parts.append(f"sources_external/ не найдена "
                                 f"({SOURCES_EXT_DIR})")
                else:
                    n2 = len(list(SOURCES_EXT_DIR.glob("*.json")))
                    if n2:
                        parts.append(f"в sources_external/ есть {n2} *.json")
                hint = "; ".join(parts) or "ни одного *.json ни в одной папке"
            return self._send({"items": items,
                               "dir": str(SOURCES_DIR),
                               "dir_ext": str(SOURCES_EXT_DIR),
                               "hint": hint})

        if route == "source":
            name = Path(one("file", "")).name
            for folder in (SOURCES_DIR, SOURCES_EXT_DIR):
                path = folder / name
                if path.is_file() and path.suffix == ".json":
                    return self._send({"file": path.name,
                                       "content": path.read_text(encoding="utf-8"),
                                       "external": folder == SOURCES_EXT_DIR})
            return self._error("Файл источника не найден.", 404)

        if route == "run":
            return self._send(RUNNER.state(int(one("since", 0))))

        if route == "files":
            return self._send({"items": self._files()})

        if route == "export":
            return self._export(one, q)

        if route == "completeness_fields":
            specs = load_specs()
            table = one("table")
            spec = specs.get(table)
            db = db_for_table(table)
            fields = completeness.completeness_by_field(
                db, table, q.get("filters"), q.get("match", "AND"))
            for f in fields:
                f["kind"] = completeness._kind(f["field"], spec)
                f["label"] = field_labels.label_for(f["field"])
            return self._send({"fields": fields,
                               "overall": completeness.overall_score(fields, spec)})

        if route == "completeness_records":
            table = one("table")
            spec = load_specs().get(table)
            db = db_for_table(table)
            threshold = float(one("threshold", 80))
            rows = completeness.completeness_by_record(
                db, table, spec, q.get("filters"), q.get("match", "AND"))
            for r in rows:
                r["missing_labels"] = [field_labels.label_for(f)
                                       for f in r["missing"]]
            below = [r for r in rows if r["pct"] < threshold]
            incomplete = [r for r in rows if r["pct"] < 100]
            incomplete.sort(key=lambda r: r["pct"])
            return self._send({
                "records": incomplete[:2000],
                "total": len(rows),
                "below": len(below),
                "incomplete": len(incomplete),
                "threshold": threshold,
            })

        if route == "completeness_groups":
            table = one("table")
            spec = load_specs().get(table)
            db = db_for_table(table)
            return self._send({"groups": completeness.completeness_by_group(
                db, table, spec, one("group_field"),
                q.get("filters"), q.get("match", "AND"))})

        return self._error(f"Неизвестный маршрут: /api/{route}", 404)
