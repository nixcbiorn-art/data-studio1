"""web_routes_mail — специализированная вкладка «Почта».

Обычные таблицы во вкладке «Внешние» — это grid со всеми колонками.
Для почты это неудобно: интересны группировка по отправителю, поиск
по теме, превью тела. Поэтому для источника с ключом, начинающимся на
"mail_" (mail_inbox, mail_sent, ...) фронтенд ходит по этим маршрутам,
а не по /api/query.

Маршруты:
    GET /api/mail_overview?days=30
    GET /api/mail_list?days=30&page=1&q=...&from_addr=...&unseen=1
    GET /api/mail_message?message_id=...&with_body=1
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from redcat.data import dataops
from redcat.web.web_data import db_for_table, load_specs

MAIL_KEY_PREFIX = "mail_"
MAIL_TABLE_DEFAULT = "mail_inbox"


def _one(p, key, default=None):
    """Достаёт одно значение из parse_qs-результата."""
    v = p.get(key)
    if isinstance(v, list):
        return v[0] if v else default
    return v if v is not None else default


class MailRoutes:
    """Только читает. Никаких действий с ящиком — ни пометок, ни удалений."""

    # ---------- вспомогательное ----------
    def _mail_sources(self) -> list[str]:
        """Все источники-почты: ключ начинается на mail_."""
        out = [k for k in load_specs() if k.startswith(MAIL_KEY_PREFIX)]
        return out or [MAIL_TABLE_DEFAULT]

    def _mail_source(self, p) -> str:
        """Какую почтовую таблицу смотреть. ?table=... или первая из
        списка (обычно mail_inbox)."""
        want = self._one(p, "table")
        available = self._mail_sources()
        if want and want in available:
            return want
        # приоритет — входящие
        return "mail_inbox" if "mail_inbox" in available else available[0]

    # ---------- GET /api/mail_overview ----------
    def _mail_overview(self, p) -> dict:
        """Сводка для верхней панели вкладки: всего/непрочитанных/за N дней,
        топ отправителей, разбивка по дням."""
        table = self._mail_source(p)
        db = db_for_table(table)
        days = int(self._one(p, "days", 30) or 30)
        since = (datetime.now() - timedelta(days=days)).isoformat()

        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            total = conn.execute(
                f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            unseen = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE seen = 0'
            ).fetchone()[0]
            in_window = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE date >= ?',
                (since,)).fetchone()[0]

            by_sender = [dict(r) for r in conn.execute(
                f'SELECT from_addr, from_name, COUNT(*) AS n, '
                f'SUM(CASE WHEN seen=0 THEN 1 ELSE 0 END) AS unseen '
                f'FROM "{table}" WHERE date >= ? '
                f'GROUP BY from_addr ORDER BY n DESC LIMIT 20',
                (since,)).fetchall()]

            by_day = [dict(r) for r in conn.execute(
                f'SELECT substr(date, 1, 10) AS day, COUNT(*) AS n, '
                f'SUM(CASE WHEN seen=0 THEN 1 ELSE 0 END) AS unseen '
                f'FROM "{table}" WHERE date >= ? '
                f'GROUP BY day ORDER BY day',
                (since,)).fetchall()]

            has_attach = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE attachments > 0'
            ).fetchone()[0]
        finally:
            conn.close()

        return {
            "table": table,
            "available_tables": self._mail_sources(),
            "window_days": days,
            "total": total,
            "unseen": unseen,
            "in_window": in_window,
            "with_attachments": has_attach,
            "by_sender": by_sender,
            "by_day": by_day,
        }

    # ---------- GET /api/mail_list ----------
    def _mail_list(self, p) -> dict:
        """Список писем с фильтрами."""
        table = self._mail_source(p)
        db = db_for_table(table)
        page = max(1, int(self._one(p, "page", 1) or 1))
        page_size = min(500, max(10, int(self._one(p, "page_size", 50) or 50)))
        days = int(self._one(p, "days", 30) or 30)
        since = (datetime.now() - timedelta(days=days)).isoformat()

        where, args = ["date >= ?"], [since]
        q = (self._one(p, "q") or "").strip()
        if q:
            where.append(
                "(subject LIKE ? OR from_addr LIKE ? OR from_name LIKE ?)")
            args += [f"%{q}%", f"%{q}%", f"%{q}%"]
        if self._one(p, "from_addr"):
            where.append("from_addr = ?")
            args.append(self._one(p, "from_addr"))
        if self._one(p, "unseen") == "1":
            where.append("seen = 0")
        if self._one(p, "attach") == "1":
            where.append("attachments > 0")

        where_sql = " AND ".join(where)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            total = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE {where_sql}',
                args).fetchone()[0]
            offset = (page - 1) * page_size
            rows = [dict(r) for r in conn.execute(
                f'SELECT message_id, uid, folder, date, from_addr, from_name, '
                f'"to", subject, size, attachments, seen '
                f'FROM "{table}" WHERE {where_sql} '
                f'ORDER BY date DESC LIMIT ? OFFSET ?',
                args + [page_size, offset]).fetchall()]
        finally:
            conn.close()

        return {
            "table": table,
            "rows": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": max(1, (total + page_size - 1) // page_size),
        }

    # ---------- GET /api/mail_message ----------
    def _mail_message(self, p) -> dict:
        """Одно письмо: метаданные из базы + тело через IMAP (по запросу)."""
        table = self._mail_source(p)
        db = db_for_table(table)
        message_id = self._one(p, "message_id")
        if not message_id:
            raise dataops.DataError("Нужен ?message_id=...")

        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                f'SELECT * FROM "{table}" WHERE message_id = ?',
                (message_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise dataops.DataError(f"Письмо {message_id} не найдено.")
        meta = dict(row)

        body = None
        error = None
        body = None
        error = None
        body_from_cache = False
        cached_at = None
        if self._one(p, "with_body") == "1":
            force = self._one(p, "refresh") == "1"
            try:
                from redcat.web.web_mail_body_cache import get_body
                res = get_body(
                    table, message_id,
                    meta.get("folder") or "INBOX",
                    int(meta["uid"]),
                    force_refresh=force,
                )
                body = {
                    "body": res.get("body"),
                    "content_type": res.get("content_type"),
                    "truncated": res.get("truncated"),
                }
                error = res.get("error")
                body_from_cache = res.get("from_cache", False)
                cached_at = res.get("cached_at")
            except Exception as e:  # noqa: BLE001
                error = f"{type(e).__name__}: {e}"
        
        return {"message": meta, "body": body, "body_error": error}


# ──────────────────────────────────────────────────────────────
#  Настройки IMAP из веб-формы (расширение MailRoutes)
# ──────────────────────────────────────────────────────────────
def _mail_settings_status(self) -> dict:
    """Возвращает статус: что сохранено в .env, есть ли spec, есть ли таблица."""
    from redcat.web.web_mail_settings import mail_status
    return mail_status()


MailRoutes._mail_settings_status = _mail_settings_status


# ──────────────────────────────────────────────────────────────
#  Расширения MailRoutes: чат, уведомления, планировщик
# ──────────────────────────────────────────────────────────────
def _mail_chat(self, body: dict) -> dict:
    """Обрабатывает одно сообщение чата. Тело: {"text": "..."}."""
    from redcat.web.web_mail_chat import answer
    text = (body.get("text") or "").strip()
    if not text:
        return {"text": "Напишите что-нибудь."}
    return answer(text)


def _mail_chat_info(self) -> dict:
    from redcat.web.web_mail_chat import info
    return info()


def _mail_notify_status(self) -> dict:
    from redcat.web.web_mail_notify import status
    return status()


def _mail_notify_save(self, body: dict) -> dict:
    from redcat.web.web_mail_notify import save_settings
    return save_settings(body)


def _mail_notify_test(self) -> dict:
    from redcat.web.web_mail_notify import test_notification
    return test_notification()


def _mail_run_now(self) -> dict:
    from redcat.web.web_mail_scheduler import run_now
    return run_now()


MailRoutes._mail_chat = _mail_chat
MailRoutes._mail_chat_info = _mail_chat_info
MailRoutes._mail_notify_status = _mail_notify_status
MailRoutes._mail_notify_save = _mail_notify_save
MailRoutes._mail_notify_test = _mail_notify_test
MailRoutes._mail_run_now = _mail_run_now


# ──────────────────────────────────────────────────────────────
#  Действия с письмами, SMTP, период сбора
# ──────────────────────────────────────────────────────────────
def _mail_view_status(self, p: dict) -> dict:
    """Статус карточки письма: метаданные + политика действий + лог."""
    from redcat.web.web_mail_actions import list_actions, read_policy
    import sqlite3
    table = self._mail_source(p)
    db = db_for_table(table)
    message_id = self._one(p, "message_id")
    if not message_id:
        raise dataops.DataError("Нужен ?message_id=...")

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f'SELECT * FROM "{table}" WHERE message_id = ?',
            (message_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise dataops.DataError(f"Письмо {message_id} не найдено.")
    return {
        "message": dict(row),
        "policy": read_policy(),
        "actions": list_actions(50),
    }


def _mail_mark(self, body: dict) -> dict:
    from redcat.web.web_mail_actions import mark_message
    import sqlite3
    message_id = body.get("message_id")
    seen = bool(body.get("seen"))
    if not message_id:
        return {"ok": False, "error": "Не указан message_id."}
    table = self._mail_source({})
    db = db_for_table(table)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f'SELECT uid, folder, subject FROM "{table}" WHERE message_id = ?',
            (message_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "error": "Письмо не найдено."}
    return mark_message(row["folder"] or "INBOX", int(row["uid"]),
                        row["subject"] or "", seen)


def _mail_delete(self, body: dict) -> dict:
    from redcat.web.web_mail_actions import delete_message
    import sqlite3
    message_id = body.get("message_id")
    if not message_id:
        return {"ok": False, "error": "Не указан message_id."}
    table = self._mail_source({})
    db = db_for_table(table)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f'SELECT uid, folder, subject FROM "{table}" WHERE message_id = ?',
            (message_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "error": "Письмо не найдено."}
    return delete_message(row["folder"] or "INBOX", int(row["uid"]),
                          row["subject"] or "")


def _mail_reply(self, body: dict) -> dict:
    from redcat.web.web_mail_actions import send_reply
    return send_reply(
        to_addr=body.get("to", ""),
        subject=body.get("subject", ""),
        body=body.get("body", ""),
        in_reply_to=body.get("in_reply_to", ""),
        references=body.get("references", ""),
    )


def _mail_smtp_status(self) -> dict:
    from redcat.web.web_mail_actions import smtp_status
    return smtp_status()


def _mail_smtp_save(self, body: dict) -> dict:
    from redcat.web.web_mail_actions import save_smtp
    return save_smtp(body)


def _mail_smtp_test(self) -> dict:
    from redcat.web.web_mail_actions import test_smtp
    return test_smtp()


def _mail_policy_status(self) -> dict:
    from redcat.web.web_mail_actions import read_policy
    return read_policy()


def _mail_policy_save(self, body: dict) -> dict:
    from redcat.web.web_mail_actions import save_policy
    return save_policy(body)


def _mail_actions_list(self, p: dict) -> dict:
    from redcat.web.web_mail_actions import list_actions
    try:
        limit = int(self._one(p, "limit", 100) or 100)
    except (TypeError, ValueError):
        limit = 100
    return {"items": list_actions(limit)}


def _mail_period_save(self, body: dict) -> dict:
    from redcat.web.web_mail_actions import set_collect_period
    try:
        days = int(body.get("days") or 30)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Период должен быть числом дней."}
    try:
        res = set_collect_period(days)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if body.get("collect"):
        from redcat.web.web_runners import RUNNER
        run = RUNNER.start(["--only", "mail_inbox"])
        res["collect"] = run
    return res


MailRoutes._mail_view_status = _mail_view_status
MailRoutes._mail_mark = _mail_mark
MailRoutes._mail_delete = _mail_delete
MailRoutes._mail_reply = _mail_reply
MailRoutes._mail_smtp_status = _mail_smtp_status
MailRoutes._mail_smtp_save = _mail_smtp_save
MailRoutes._mail_smtp_test = _mail_smtp_test
MailRoutes._mail_policy_status = _mail_policy_status
MailRoutes._mail_policy_save = _mail_policy_save
MailRoutes._mail_actions_list = _mail_actions_list
MailRoutes._mail_period_save = _mail_period_save


# ──────────────────────────────────────────────────────────────
#  Метки и шаблоны (добавлено fix_mail_labels.py)
# ──────────────────────────────────────────────────────────────
def _mail_labels_list(self) -> dict:
    from redcat.web.web_mail_labels import LABELS, counts_by_label
    table = self._mail_source({})
    return {"labels": LABELS, "counts": counts_by_label(table),
            "table": table}


def _mail_label_add(self, body: dict) -> dict:
    from redcat.web.web_mail_labels import add_label
    try:
        table = self._mail_source({})
        add_label(table, body.get("message_id", ""), body.get("label", ""))
        return {"ok": True}
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}


def _mail_label_remove(self, body: dict) -> dict:
    from redcat.web.web_mail_labels import remove_label
    try:
        table = self._mail_source({})
        remove_label(table, body.get("message_id", ""), body.get("label", ""))
        return {"ok": True}
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}


def _mail_labels_for(self, p: dict) -> dict:
    from redcat.web.web_mail_labels import labels_for
    table = self._mail_source(p)
    raw = self._one(p, "message_ids") or ""
    mids = [s.strip() for s in raw.split(",") if s.strip()]
    return {"labels": labels_for(table, mids)}


def _mail_templates_list(self) -> dict:
    from redcat.web.web_mail_labels import list_templates
    return {"items": list_templates()}


def _mail_template_save(self, body: dict) -> dict:
    from redcat.web.web_mail_labels import save_template
    try:
        return save_template(
            name=body.get("name", ""),
            body=body.get("body", ""),
            subject_prefix=body.get("subject_prefix", ""))
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}


def _mail_template_delete(self, body: dict) -> dict:
    from redcat.web.web_mail_labels import delete_template
    try:
        return delete_template(body.get("name", ""))
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}


def _mail_template_apply(self, body: dict) -> dict:
    from redcat.web.web_mail_labels import apply_template
    import sqlite3
    from redcat.web.web_data import db_for_table
    message_id = body.get("message_id")
    name = body.get("name")
    if not (message_id and name):
        return {"ok": False, "error": "Нужны message_id и name."}
    table = self._mail_source({})
    db = db_for_table(table)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            f'SELECT message_id, subject, from_name, from_addr, date '
            f'FROM "{table}" WHERE message_id = ?',
            (message_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "error": "Письмо не найдено."}
    try:
        return apply_template(name, dict(row))
    except ValueError as e:
        return {"ok": False, "error": str(e)}


MailRoutes._mail_labels_list = _mail_labels_list
MailRoutes._mail_label_add = _mail_label_add
MailRoutes._mail_label_remove = _mail_label_remove
MailRoutes._mail_labels_for = _mail_labels_for
MailRoutes._mail_templates_list = _mail_templates_list
MailRoutes._mail_template_save = _mail_template_save
MailRoutes._mail_template_delete = _mail_template_delete
MailRoutes._mail_template_apply = _mail_template_apply


# ──────────────────────────────────────────────────────────────
#  Дерево тредов (patch_mail_threads.py)
# ──────────────────────────────────────────────────────────────
def _mail_threads(self, p: dict) -> dict:
    from redcat.web.web_mail_threads import build_threads

    def _int(key, default):
        try:
            return int(self._one(p, key, default) or default)
        except (TypeError, ValueError):
            return default

    return build_threads(
        days=_int("days", 90),
        min_size=_int("min_size", 1),
        only_unseen=str(self._one(p, "unseen", "0")) in ("1", "true", "yes"),
        max_rows=_int("max_rows", 5000),
    )


MailRoutes._mail_threads = _mail_threads


# ──────────────────────────────────────────────────────────────
#  Контакты (patch_mail_contacts.py)
# ──────────────────────────────────────────────────────────────
def _mail_contacts(self, p: dict) -> dict:
    from redcat.web.web_mail_contacts import find, stats
    q = self._one(p, "q") or ""
    try:
        limit = int(self._one(p, "limit", 30) or 30)
    except (TypeError, ValueError):
        limit = 30
    items = find(q, limit=limit)
    return {"items": items, "query": q, "stats": stats()}


MailRoutes._mail_contacts = _mail_contacts


# ──────────────────────────────────────────────────────────────
#  Задачи из писем (patch_mail_tasks.py)
# ──────────────────────────────────────────────────────────────
def _mail_tasks(self, p: dict) -> dict:
    from redcat.web.web_mail_tasks import (
        list_tasks, counts, STATUSES, PRIORITIES,
    )
    items = list_tasks(
        status=self._one(p, "status") or "",
        assignee=self._one(p, "assignee") or "",
        priority=self._one(p, "priority") or "",
        due_before=self._one(p, "due_before") or "",
        query=self._one(p, "q") or "",
        limit=int(self._one(p, "limit", 500) or 500),
    )
    return {"items": items, "counts": counts(),
            "statuses": list(STATUSES), "priorities": list(PRIORITIES)}


def _mail_task_create(self, body: dict) -> dict:
    from redcat.web.web_mail_tasks import create_task
    return create_task(
        message_id=body.get("message_id", ""),
        subject=body.get("subject", ""),
        from_addr=body.get("from_addr", ""),
        title=body.get("title", ""),
        priority=body.get("priority", "normal"),
        due_date=body.get("due_date", ""),
        assignee=body.get("assignee", ""),
        note=body.get("note", ""),
    )


def _mail_task_update(self, body: dict) -> dict:
    from redcat.web.web_mail_tasks import update_task
    tid = body.get("id")
    if not tid:
        return {"ok": False, "error": "нет id"}
    fields = {k: v for k, v in body.items() if k != "id"}
    return update_task(int(tid), **fields)


def _mail_task_delete(self, body: dict) -> dict:
    from redcat.web.web_mail_tasks import delete_task
    tid = body.get("id")
    if not tid:
        return {"ok": False, "error": "нет id"}
    return delete_task(int(tid))


def _mail_tasks_apply(self, body: dict) -> dict:
    from redcat.web.web_mail_task_rules import apply_rules
    return apply_rules(
        days=int(body.get("days") or 30),
        limit=int(body.get("limit") or 500),
    )


def _mail_task_rules_list(self, p: dict) -> dict:
    from redcat.web.web_mail_tasks import list_rules
    return {"items": list_rules()}


def _mail_task_rule_create(self, body: dict) -> dict:
    from redcat.web.web_mail_tasks import create_rule
    return create_rule(
        name=body.get("name", ""),
        match_from=body.get("match_from", ""),
        match_subject=body.get("match_subject", ""),
        match_body=body.get("match_body", ""),
        priority=body.get("priority", "normal"),
        due_days=body.get("due_days") or None,
        assignee=body.get("assignee", ""),
        enabled=bool(body.get("enabled", True)),
    )


def _mail_task_rule_update(self, body: dict) -> dict:
    from redcat.web.web_mail_tasks import update_rule
    rid = body.get("id")
    if not rid:
        return {"ok": False, "error": "нет id"}
    fields = {k: v for k, v in body.items() if k != "id"}
    return update_rule(int(rid), **fields)


def _mail_task_rule_delete(self, body: dict) -> dict:
    from redcat.web.web_mail_tasks import delete_rule
    rid = body.get("id")
    if not rid:
        return {"ok": False, "error": "нет id"}
    return delete_rule(int(rid))


MailRoutes._mail_tasks = _mail_tasks
MailRoutes._mail_task_create = _mail_task_create
MailRoutes._mail_task_update = _mail_task_update
MailRoutes._mail_task_delete = _mail_task_delete
MailRoutes._mail_tasks_apply = _mail_tasks_apply
MailRoutes._mail_task_rules_list = _mail_task_rules_list
MailRoutes._mail_task_rule_create = _mail_task_rule_create
MailRoutes._mail_task_rule_update = _mail_task_rule_update
MailRoutes._mail_task_rule_delete = _mail_task_rule_delete
