"""web_mail_contacts_tree — группировка писем по контактам.

Два уровня:
  1. Контакт (from_addr) — кто пишет.
  2. Письма от этого контакта — сортировка по дате.

Внутри контакта опционально группируем по нормализованной теме —
если параметр group_by_subject=1.
"""
from __future__ import annotations

import sqlite3
import re
from datetime import datetime, timedelta

from redcat.web.web_data import db_for_table, load_specs
from redcat.web.web_mail_threads import normalize_subject

MAIL_KEY_DEFAULT = "mail_inbox"


def _mail_table() -> str:
    keys = [k for k in load_specs() if k.startswith("mail_")]
    return MAIL_KEY_DEFAULT if MAIL_KEY_DEFAULT in keys else (keys[0] if keys else MAIL_KEY_DEFAULT)


def _domain(addr: str) -> str:
    if not addr or "@" not in addr:
        return ""
    return addr.split("@", 1)[1].lower()


def build_contacts_tree(days: int = 90,
                         min_count: int = 1,
                         only_unread: bool = False,
                         group_by_subject: bool = False,
                         max_rows: int = 5000) -> dict:
    """Возвращает дерево контактов.

    Формат:
      {
        "window_days": 90,
        "total_messages": N,
        "total_contacts": M,
        "contacts": [
          {
            "email": "ivan@x.ru",
            "name": "Иван Петров",       # если есть
            "domain": "x.ru",
            "count": 12,
            "unseen": 3,
            "last_date": iso,
            "first_date": iso,
            "with_attach": 2,
            "threads": [                 # если group_by_subject
               {"subject": "...", "count": 3, "messages": [...]}
            ],
            "messages": [...]            # всегда (по дате asc)
          },
          ...
        ]
      }
    """
    since = (datetime.now() - timedelta(days=days)).isoformat()
    table = _mail_table()
    db = db_for_table(table)

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            f'SELECT message_id, date, from_addr, from_name, '
            f'subject, attachments, seen '
            f'FROM "{table}" WHERE date >= ? '
            f'AND from_addr IS NOT NULL AND from_addr <> \'\' '
            f'ORDER BY date DESC LIMIT ?',
            (since, max_rows)).fetchall()]
    finally:
        conn.close()

    # Метки
    try:
        from redcat.web.web_mail_labels import labels_for
        labels_map = labels_for(table, [r["message_id"] for r in rows])
    except Exception:
        labels_map = {}

    # Группировка по from_addr
    contacts: dict = {}
    for r in rows:
        addr = (r["from_addr"] or "").lower()
        if not addr:
            continue
        c = contacts.setdefault(addr, {
            "email": addr,
            "name": "",
            "domain": _domain(addr),
            "count": 0,
            "unseen": 0,
            "with_attach": 0,
            "first_date": "",
            "last_date": "",
            "messages": [],
        })
        c["count"] += 1
        if not r["seen"]:
            c["unseen"] += 1
        if (r["attachments"] or 0) > 0:
            c["with_attach"] += 1
        # Имя — самое частое или первое непустое
        if not c["name"] and r.get("from_name"):
            c["name"] = r["from_name"]
        d = r.get("date") or ""
        if not c["first_date"] or d < c["first_date"]:
            c["first_date"] = d
        if not c["last_date"] or d > c["last_date"]:
            c["last_date"] = d
        c["messages"].append({
            "message_id": r["message_id"],
            "date": r.get("date"),
            "subject": r.get("subject"),
            "seen": r.get("seen"),
            "attachments": r.get("attachments") or 0,
            "labels": labels_map.get(r["message_id"], []),
        })

    # Сортировка писем внутри контакта: старые → новые
    for c in contacts.values():
        c["messages"].sort(key=lambda m: m.get("date") or "")

    # Опционально — подгруппы по теме
    if group_by_subject:
        for c in contacts.values():
            threads: dict = {}
            for m in c["messages"]:
                key = normalize_subject(m.get("subject") or "")
                t = threads.setdefault(key, {
                    "subject": m.get("subject") or "(без темы)",
                    "count": 0,
                    "messages": [],
                })
                t["count"] += 1
                t["messages"].append(m)
            c["threads"] = sorted(
                threads.values(),
                key=lambda t: t["messages"][-1].get("date") or "",
                reverse=True)

    # Фильтр и сортировка
    out = []
    for c in contacts.values():
        if c["count"] < min_count:
            continue
        if only_unread and c["unseen"] == 0:
            continue
        out.append(c)
    out.sort(key=lambda c: c["last_date"] or "", reverse=True)

    return {
        "window_days": days,
        "total_messages": len(rows),
        "total_contacts": len(out),
        "group_by_subject": group_by_subject,
        "contacts": out,
    }
