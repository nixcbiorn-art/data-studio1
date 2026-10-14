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
from redcat.web.web_mail_settings import (save_mail_settings, test_mail_settings)


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

        if route == "mail_settings_test":
            return self._send(test_mail_settings(b))

        if route == "mail_settings_save":
            try:
                return self._send(save_mail_settings(b))
            except ValueError as e:
                return self._error(str(e))

        if route == "mail_collect":
            return self._send(RUNNER.start(["--only", "mail_inbox"]))

        if route == "mail_chat":
            return self._send(self._mail_chat(b))

        if route == "mail_notify_save":
            return self._send(self._mail_notify_save(b))

        if route == "mail_notify_test":
            return self._send(self._mail_notify_test())

        if route == "mail_run_now":
            return self._send(self._mail_run_now())

        if route == "mail_mark":
            return self._send(self._mail_mark(b))

        if route == "mail_delete":
            return self._send(self._mail_delete(b))

        if route == "mail_reply":
            return self._send(self._mail_reply(b))

        if route == "mail_smtp_save":
            try:
                return self._send(self._mail_smtp_save(b))
            except ValueError as e:
                return self._error(str(e))

        if route == "mail_smtp_test":
            return self._send(self._mail_smtp_test())

        if route == "mail_policy_save":
            return self._send(self._mail_policy_save(b))

        if route == "mail_period_save":
            return self._send(self._mail_period_save(b))

        if route == "mail_label_add":
            return self._send(self._mail_label_add(b))

        if route == "mail_label_remove":
            return self._send(self._mail_label_remove(b))

        if route == "mail_template_save":
            return self._send(self._mail_template_save(b))

        if route == "mail_template_delete":
            return self._send(self._mail_template_delete(b))

        if route == "mail_template_apply":
            return self._send(self._mail_template_apply(b))
        if route == "mail_send":
            from redcat.web.web_mail_send_hook import send as _send_mail
            return self._send(_send_mail(
                to=b.get("to", ""),
                subject=b.get("subject", ""),
                body=b.get("body", ""),
                cc=b.get("cc", ""),
                bcc=b.get("bcc", ""),
                in_reply_to=b.get("in_reply_to", ""),
            ))

        if route == "mail_task_create":
            return self._send(self._mail_task_create(b))

        if route == "mail_task_update":
            return self._send(self._mail_task_update(b))

        if route == "mail_task_delete":
            return self._send(self._mail_task_delete(b))

        if route == "mail_tasks_apply":
            return self._send(self._mail_tasks_apply(b))

        if route == "mail_task_rule_create":
            return self._send(self._mail_task_rule_create(b))

        if route == "mail_task_rule_update":
            return self._send(self._mail_task_rule_update(b))

        if route == "mail_task_rule_delete":
            return self._send(self._mail_task_rule_delete(b))

        if route == "hub_job_save":
            from redcat.web.web_hub import save_job
            key = (b.get("key") or "").strip()
            if not key:
                return self._error("Не указан key")
            fields = {k: v for k, v in b.items() if k != "key"}
            return self._send(save_job(key, fields))

        if route == "hub_run":
            from redcat.web.web_hub_scheduler import run_now
            key = (b.get("key") or "").strip()
            if not key:
                return self._error("Не указан key")
            return self._send(run_now(key))

        if route == "mail_label_tasks_set":
            from redcat.web.web_mail_label_tasks import set_config
            label = (b.get("label") or "").strip()
            if not label:
                return self._error("Не указан label")
            fields = {k: v for k, v in b.items() if k != "label"}
            return self._send(set_config(label, **fields))

        if route == "mail_notify_filters_save":
            from redcat.web.web_mail_notify_filters import set_fields
            return self._send(set_fields(**b))

        if route == "mail_save":
            from redcat.web.web_mail_saved import save_from_imap
            import sqlite3 as _sq
            table = self._mail_source({})
            db = db_for_table(table)
            mid = (b.get("message_id") or "").strip()
            if not mid:
                return self._error("Нужен message_id")
            _c = _sq.connect(f"file:{db}?mode=ro", uri=True)
            _c.row_factory = _sq.Row
            try:
                row = _c.execute(
                    f'SELECT * FROM "{table}" WHERE message_id = ?',
                    (mid,)).fetchone()
            finally:
                _c.close()
            if not row:
                return self._error("Письмо не найдено")
            meta = dict(row)
            return self._send(save_from_imap(
                table, mid,
                meta.get("folder") or "INBOX",
                int(meta["uid"]),
                subject=meta.get("subject") or "",
                from_addr=meta.get("from_addr") or "",
                date=meta.get("date") or "",
            ))

        if route == "mail_saved_update":
            from redcat.web.web_mail_saved import update_meta
            try:
                sid = int(b.get("id") or 0)
            except (TypeError, ValueError):
                return self._error("Нет id")
            return self._send(update_meta(
                sid, tags=b.get("tags"), note=b.get("note")))

        if route == "mail_saved_delete":
            from redcat.web.web_mail_saved import delete_saved
            try:
                sid = int(b.get("id") or 0)
            except (TypeError, ValueError):
                return self._error("Нет id")
            return self._send(delete_saved(sid))

        if route == "hub_stop":
            from redcat.web.web_hub_scheduler import stop_job
            key = (b.get("key") or "").strip()
            if not key:
                return self._error("Не указан key")
            return self._send(stop_job(key))

        return self._error(f"Неизвестный маршрут: /api/{route}", 404)
