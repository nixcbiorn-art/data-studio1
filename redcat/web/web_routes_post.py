"""web_routes_post — маршруты POST /api/*."""
from __future__ import annotations

import json
from pathlib import Path
from redcat.data import dataops
from redcat.sources import registry as src
from redcat.data import studio_store as store
from redcat.web.web_config import DATA_DB, EXTERNAL_DB, SOURCES_DIR, SOURCES_EXT_DIR, STUDIO_DB
from redcat.web.web_data import db_for_table, load_specs
from redcat.web.web_runners import RUNNER, TOOLS, TOOLS_RUNNER
from redcat.web.web_telegram import telegram_probe, telegram_status, token_status, write_env_token, write_telegram_env


class PostRoutes:
    """Маршруты POST /api/*."""

    def _api_post(self, route, b):
        if route == "tool_start":
            key = (b.get("key") or "").strip()
            tool = next((t for t in TOOLS if t["key"] == key), None)
            if tool is None: return self._error(f"Инструмент не найден")
            return self._send(TOOLS_RUNNER.start(tool))

        if route == "tool_stop":
            return self._send(TOOLS_RUNNER.stop())

        if route == "telegram_save":
            token = (b.get("token") or "").strip()
            chat_id = (b.get("chat_id") or "").strip()
            if not token: return self._error("Токен не может быть пустым")
            write_telegram_env(token, chat_id)
            return self._send({"ok": True, "status": telegram_status()})

        if route == "telegram_test":
            cur = telegram_status()
            token = (b.get("token") or "").strip()
            chat_id = (b.get("chat_id") or "").strip() or cur.get("chat_id", "")
            return self._send(telegram_probe(token, chat_id))


        if route == "edit":
            return self._send(store.set_edit(
                STUDIO_DB, b["table"], b["id"], b["field"], b.get("value"),
                b.get("old_value"), b.get("author", ""), b.get("note", "")))

        if route == "edit_bulk":
            return self._send({"updated": self._bulk_edit(b)})

        if route == "edit_revert":
            if b.get("edit_id"):
                store.revert_edit(STUDIO_DB, b["edit_id"])
                return self._send({"ok": True})
            n = store.revert_scope(STUDIO_DB, b["table"], b.get("id"),
                                   b.get("field"))
            return self._send({"ok": True, "reverted": n})

        if route == "note":
            store.set_note(STUDIO_DB, b["table"], b["id"], b.get("note", ""))
            return self._send({"ok": True})

        if route == "tag":
            store.toggle_tag(STUDIO_DB, b["table"], b["id"], b["tag"],
                             bool(b.get("on", True)))
            return self._send({"ok": True})

        if route == "flag":
            store.toggle_flag(STUDIO_DB, b["table"], b["id"], b["flag"],
                              bool(b.get("on", True)))
            return self._send({"ok": True})

        if route == "view_save":
            vid = store.save_view(STUDIO_DB, b["name"], b["table"],
                                  b.get("payload", {}))
            return self._send({"ok": True, "id": vid})

        if route == "view_delete":
            store.delete_view(STUDIO_DB, b["id"])
            return self._send({"ok": True})

        if route == "computed_save":
            names = [c["name"] for c in dataops.columns(db_for_table(b["table"]), b["table"])]
            used = dataops.validate_formula(b["expr"], names)
            store.save_computed(STUDIO_DB, b["table"], b["name"], b["expr"])
            return self._send({"ok": True, "uses": used})

        if route == "computed_delete":
            store.delete_computed(STUDIO_DB, b["table"], b["name"])
            return self._send({"ok": True})

        if route == "search_rebuild":
            specs = load_specs()

            def _docs():
                seen = set()
                for db in (DATA_DB, EXTERNAL_DB):
                    if not db.exists():
                        continue
                    for doc in dataops.iter_search_documents(db, specs):
                        key = (doc[0], doc[1])
                        if key in seen:
                            continue
                        seen.add(key)
                        yield doc

            count = store.rebuild_search_index(STUDIO_DB, _docs())
            return self._send({"ok": True, "documents": count})

        if route == "upload_csv":
            return self._send(self._upload_csv_batch(b))

        if route == "token":
            write_env_token(b.get("token", "").strip())
            return self._send({"ok": True, "status": token_status()})

        if route == "source_save":
            target_dir = SOURCES_EXT_DIR if b.get("external") else SOURCES_DIR
            path = src.save_source_file(target_dir, b["spec"])
            return self._send({"ok": True, "file": path.name,
                               "external": target_dir == SOURCES_EXT_DIR})

        if route == "source_save_raw":
            name = Path(str(b["file"])).name
            if not name.endswith(".json"):
                return self._error("Можно сохранять только *.json")
            if b.get("external"):
                target = SOURCES_EXT_DIR / name
            else:
                target = SOURCES_DIR / name
            if target.parent not in (SOURCES_DIR, SOURCES_EXT_DIR):
                return self._error("Недопустимый путь.")
            target.parent.mkdir(exist_ok=True)
            json.loads(b["content"])
            target.write_text(b["content"], encoding="utf-8")
            return self._send({"ok": True})

        if route == "source_delete":
            name = Path(str(b["file"])).name
            if not name.endswith(".json"):
                return self._error("Можно удалять только *.json")
            for folder in (SOURCES_DIR, SOURCES_EXT_DIR):
                path = folder / name
                if path.is_file():
                    src.delete_source_file(path)
            return self._send({"ok": True})

        if route == "probe":
            return self._send(self._probe(b))

        if route == "probe_spec":
            return self._send(self._probe_spec(b))

        if route == "run_start":
            args = []
            if b.get("only"):
                args += ["--only"] + list(b["only"])
            if b.get("excel"):
                args.append("--excel")
            if b.get("no_anomalies"):
                args.append("--no-anomalies")
            if b.get("sensitivity"):
                args += ["--sensitivity", str(b["sensitivity"])]
            if b.get("concurrency"):
                args += ["--concurrency", str(b["concurrency"])]
            return self._send(RUNNER.start(args, b.get("token") or None))

        if route == "run_stop":
            return self._send(RUNNER.stop())

        return self._error(f"Неизвестный маршрут: /api/{route}", 404)
