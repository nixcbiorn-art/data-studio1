"""web_mail_contacts — контакты, собранные из писем.

Собирает все уникальные адреса из From/To/Cc писем в mail_inbox,
считает частоту и дату последнего письма. Кэшируется в памяти
на 60 секунд — чтобы не долбить БД на каждое нажатие клавиши.
"""
from __future__ import annotations

import re
import sqlite3
import time
from email.utils import getaddresses

from redcat.web.web_data import db_for_table, load_specs

MAIL_KEY_DEFAULT = "mail_inbox"
CACHE_TTL = 60  # секунд

_cache: dict = {"ts": 0, "contacts": None}


def _mail_table() -> str:
    keys = [k for k in load_specs() if k.startswith("mail_")]
    if MAIL_KEY_DEFAULT in keys:
        return MAIL_KEY_DEFAULT
    return keys[0] if keys else MAIL_KEY_DEFAULT


def _split_addrs(raw: str) -> list[str]:
    """Разбирает строку адресов «Имя <a@b>, c@d; e@f» на список email."""
    if not raw:
        return []
    out = []
    for _, addr in getaddresses([raw]):
        addr = (addr or "").strip().lower()
        if addr and "@" in addr:
            out.append(addr)
    if not out:
        # запасной путь — голый regex
        for m in re.findall(r"[\w.+\-]+@[\w.\-]+", raw):
            out.append(m.lower())
    return out


def _build_contacts() -> list[dict]:
    """Читает все письма и собирает {email: {name, count, last_date}}."""
    table = _mail_table()
    db = db_for_table(table)
    contacts: dict = {}

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Ограничимся 5000 писем — этого достаточно для контактов.
        rows = conn.execute(
            f'SELECT from_addr, from_name, "to" AS to_field, '
            f'date FROM "{table}" '
            f'ORDER BY date DESC LIMIT 5000').fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    for r in rows:
        date = (r["date"] or "")[:10]

        # From
        fa = (r["from_addr"] or "").strip().lower()
        fn = (r["from_name"] or "").strip()
        if fa and "@" in fa:
            c = contacts.setdefault(fa, {"email": fa, "name": "", "count": 0,
                                          "last": "", "kind": "from"})
            c["count"] += 1
            if not c["last"] or date > c["last"]:
                c["last"] = date
            if fn and not c["name"]:
                c["name"] = fn

        # To / Cc
        for addr in _split_addrs(r["to_field"] or ""):
            c = contacts.setdefault(addr, {"email": addr, "name": "",
                                            "count": 0, "last": "",
                                            "kind": "to"})
            c["count"] += 1
            if not c["last"] or date > c["last"]:
                c["last"] = date

    return sorted(contacts.values(),
                  key=lambda x: (-x["count"], x["email"]))


def _get_cached() -> list[dict]:
    now = time.time()
    if (_cache["contacts"] is not None
            and now - _cache["ts"] < CACHE_TTL):
        return _cache["contacts"]
    contacts = _build_contacts()
    _cache["contacts"] = contacts
    _cache["ts"] = now
    return contacts


def invalidate_cache() -> None:
    """Сбросить кэш — вызывается после нового сбора."""
    _cache["ts"] = 0
    _cache["contacts"] = None


def find(query: str = "", limit: int = 30) -> list[dict]:
    """Поиск по подстроке. Пустой запрос — топ по частоте."""
    contacts = _get_cached()
    q = (query or "").strip().lower()
    if not q:
        return contacts[:limit]

    # Приоритет: совпадение с начала email > совпадение имени > подстрока
    starts_with = []
    name_match = []
    contains = []
    for c in contacts:
        email = c["email"]
        name = (c["name"] or "").lower()
        if email.startswith(q):
            starts_with.append(c)
        elif q in name:
            name_match.append(c)
        elif q in email:
            contains.append(c)

    out = (starts_with + name_match + contains)[:limit]
    return out


def stats() -> dict:
    contacts = _get_cached()
    return {
        "total": len(contacts),
        "cached_at": _cache["ts"],
        "top": contacts[:5],
    }
