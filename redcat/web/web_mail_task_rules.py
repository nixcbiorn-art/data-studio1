"""web_mail_task_rules — применяет правила к письмам.

Логика: для каждого правила проверяем последние N писем из mail_inbox.
Если письмо подходит и по нему ещё нет задачи — создаём.
Матч по подстроке в from/subject. Body в правилах не проверяем —
тела писем не собираются на этапе сбора (только заголовки).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from redcat.web.web_config import STUDIO_DB
from redcat.web.web_data import db_for_table, load_specs


def _mail_table() -> str:
    keys = [k for k in load_specs() if k.startswith("mail_")]
    return "mail_inbox" if "mail_inbox" in keys else (keys[0] if keys else "mail_inbox")


def apply_rules(days: int = 30, limit: int = 500) -> dict:
    """Применяет все включённые правила к последним письмам.

    Возвращает {created: N, scanned: M, per_rule: {rule_id: count}}.
    Идемпотентно: если задача по message_id уже есть — не создаёт.
    """
    from redcat.web.web_mail_tasks import create_task, list_rules
    rules = [r for r in list_rules() if r.get("enabled")]
    if not rules:
        return {"created": 0, "scanned": 0, "per_rule": {},
                "reason": "нет включённых правил"}

    table = _mail_table()
    db = db_for_table(table)
    since = (datetime.now() - timedelta(days=days)).isoformat()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            f'SELECT message_id, subject, from_addr, date '
            f'FROM "{table}" WHERE date >= ? '
            f'ORDER BY date DESC LIMIT ?',
            (since, limit)).fetchall()]
    finally:
        conn.close()

    # Уже привязанные message_id
    with sqlite3.connect(f"file:{STUDIO_DB}?mode=ro", uri=True) as c2:
        existing = {r[0] for r in c2.execute(
            "SELECT message_id FROM mail_tasks WHERE message_id IS NOT NULL")}

    created_total = 0
    per_rule: dict = {}
    for r in rules:
        rid = r["id"]
        mf = (r.get("match_from") or "").lower()
        ms = (r.get("match_subject") or "").lower()
        pr = r.get("priority") or "normal"
        dd = r.get("due_days")
        asg = r.get("assignee") or ""
        cnt = 0
        for msg in rows:
            mid = msg.get("message_id")
            if not mid or mid in existing:
                continue
            from_addr = (msg.get("from_addr") or "").lower()
            subject = (msg.get("subject") or "").lower()
            if mf and mf not in from_addr:
                continue
            if ms and ms not in subject:
                continue
            # Мэтчим
            due = ""
            if dd:
                try:
                    due = (datetime.now() + timedelta(days=int(dd))
                           ).date().isoformat()
                except (TypeError, ValueError):
                    due = ""
            res = create_task(
                message_id=mid,
                subject=msg.get("subject") or "",
                from_addr=msg.get("from_addr") or "",
                title=msg.get("subject") or "(без темы)",
                priority=pr, due_date=due, assignee=asg,
                note=f"авто-задача по правилу «{r.get('name') or rid}»",
            )
            if res.get("ok") and not res.get("existing"):
                cnt += 1
                existing.add(mid)
        if cnt:
            per_rule[rid] = cnt
            created_total += cnt

    return {"created": created_total, "scanned": len(rows),
            "rules": len(rules), "per_rule": per_rule}
