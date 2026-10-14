"""web_mail_chat — разбор вопросов о почте и ответы.

Это НЕ языковая модель. Это набор правил: если вопрос попадает в один
из типовых шаблонов («сколько за неделю», «от иван@…», «тема заказ»,
«непрочитанные», «с вложениями», «топ 5 отправителей», «сводка»,
«последние 10») — отвечаем цифрами и списком. Иначе — ищем подстроку.

Такое поведение предсказуемо, работает офлайн и не требует API-ключей.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta

from redcat.web.web_data import db_for_table, load_specs

MAIL_KEY_DEFAULT = "mail_inbox"

HELP_TEXT = (
    "Я понимаю такие вопросы:\n"
    "  • сколько писем / за неделю / за 3 дня / за месяц\n"
    "  • непрочитанные / с вложениями\n"
    "  • от иван@example.com / от иван\n"
    "  • тема заказ / про доставку\n"
    "  • последние 10 писем\n"
    "  • топ 5 отправителей\n"
    "  • сводка / динамика за неделю\n"
    "Или просто напишите слово — найду его в теме или адресе."
)

SUGGESTIONS = [
    "сводка за неделю",
    "сколько писем за сегодня",
    "непрочитанные",
    "с вложениями за месяц",
    "топ 5 отправителей",
    "последние 10",
    "динамика за неделю",
]


# ──────────────────────────────────────────────────────────────
#  Разбор вопроса → intent
# ──────────────────────────────────────────────────────────────
def parse(question: str) -> dict:
    t = (question or "").lower().strip().rstrip("?!.")

    if not t:
        return {"intent": "help"}

    if any(w in t for w in ("привет", "help", "справка", "помощь",
                            "что ты", "что умеешь", "как дела")):
        return {"intent": "help"}

    # окно по времени
    days = 30
    m = re.search(r"за\s+(\d+)\s*(?:дн|день|дня|дней)", t)
    if m:
        days = int(m.group(1))
    elif "сегодня" in t or "за день" in t or "за сутки" in t:
        days = 1
    elif "вчера" in t:
        days = 2
    elif "недел" in t or "7 дн" in t:
        days = 7
    elif "месяц" in t or "30 дн" in t:
        days = 30
    elif "год" in t:
        days = 365

    # последние N
    m = re.search(r"последни[ех]\s+(\d+)", t)
    if m:
        return {"intent": "list", "n": int(m.group(1)), "days": days}

    # непрочитанные
    if "непрочит" in t or "новые" in t or "unseen" in t:
        return {"intent": "list", "unseen": True, "days": days}

    # вложения
    if "вложен" in t or "файл" in t or "attach" in t:
        return {"intent": "list", "attach": True, "days": days}

    # топ отправителей
    if "топ" in t and ("отправител" in t or "адрес" in t or "from" in t):
        m = re.search(r"топ\s+(\d+)", t)
        return {"intent": "top_senders",
                "n": int(m.group(1)) if m else 10, "days": days}

    # сводка / динамика
    if any(w in t for w in ("сводк", "обзор", "статист", "динамик",
                            "график", "по дням")):
        return {"intent": "overview", "days": days}

    # от кого-то
    m = re.search(r"\bот\s+([a-zа-я0-9@._\-+]+)", t)
    if m:
        return {"intent": "list", "from": m.group(1), "days": days}

    # тема
    m = re.search(r"(?:тем[аеу]|про|о)\s+[«\"']?([^»\"']+)", t)
    if m:
        return {"intent": "list", "q": m.group(1).strip(), "days": days}

    # сколько
    if "сколько" in t or "count" in t or "число" in t:
        return {"intent": "count", "days": days}

    # fallback — поиск по подстроке
    return {"intent": "list", "q": question.strip(), "days": days}


# ──────────────────────────────────────────────────────────────
#  Работа с базой
# ──────────────────────────────────────────────────────────────
def _mail_table() -> str:
    for k in load_specs():
        if k.startswith("mail_"):
            if k == MAIL_KEY_DEFAULT:
                return k
    for k in load_specs():
        if k.startswith("mail_"):
            return k
    return MAIL_KEY_DEFAULT


def _open():
    table = _mail_table()
    db = db_for_table(table)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn, table


def _where(days: int, extra_where: list, extra_args: list):
    since = (datetime.now() - timedelta(days=days)).isoformat()
    where = ["date >= ?"]
    args = [since]
    where += extra_where
    args += extra_args
    return " AND ".join(where), args


# ──────────────────────────────────────────────────────────────
#  Ответы
# ──────────────────────────────────────────────────────────────
def _answer_help() -> dict:
    return {"text": HELP_TEXT, "suggestions": SUGGESTIONS}


def _answer_count(days: int) -> dict:
    conn, table = _open()
    try:
        where, args = _where(days, [], [])
        total = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE {where}', args).fetchone()[0]
        unseen = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE {where} AND seen = 0',
            args).fetchone()[0]
        attach = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE {where} AND attachments > 0',
            args).fetchone()[0]
    finally:
        conn.close()
    period = _period_word(days)
    return {
        "text": f"За {period}: {total} писем.\n"
                f"  · непрочитанных: {unseen}\n"
                f"  · с вложениями: {attach}",
    }


def _answer_list(days: int, q=None, from_addr=None,
                 unseen=False, attach=False, n=20) -> dict:
    conn, table = _open()
    try:
        extra_where, extra_args = [], []
        if q:
            extra_where.append(
                "(subject LIKE ? OR from_addr LIKE ? OR from_name LIKE ?)")
            extra_args += [f"%{q}%"] * 3
        if from_addr:
            extra_where.append("(from_addr LIKE ? OR from_name LIKE ?)")
            extra_args += [f"%{from_addr}%"] * 2
        if unseen:
            extra_where.append("seen = 0")
        if attach:
            extra_where.append("attachments > 0")
        where, args = _where(days, extra_where, extra_args)

        total = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE {where}', args).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f'SELECT message_id, date, from_addr, from_name, subject, '
            f'attachments, seen FROM "{table}" WHERE {where} '
            f'ORDER BY date DESC LIMIT ?',
            args + [n]).fetchall()]
    finally:
        conn.close()

    if total == 0:
        return {"text": "Ничего не нашлось по этому запросу."}

    bits = []
    if q: bits.append(f"с «{q}»")
    if from_addr: bits.append(f"от «{from_addr}»")
    if unseen: bits.append("непрочитанных")
    if attach: bits.append("с вложениями")
    what = (" (" + ", ".join(bits) + ")") if bits else ""
    period = _period_word(days)

    head = f"Найдено за {period}{what}: {total}."
    if total > n:
        head += f" Показываю последние {n}."

    return {
        "text": head,
        "rows": [_format_row(r) for r in rows],
    }


def _answer_top_senders(days: int, n: int = 10) -> dict:
    conn, table = _open()
    try:
        where, args = _where(days, [], [])
        rows = [dict(r) for r in conn.execute(
            f'SELECT from_addr, from_name, COUNT(*) AS cnt, '
            f'SUM(CASE WHEN seen=0 THEN 1 ELSE 0 END) AS unseen '
            f'FROM "{table}" WHERE {where} '
            f'GROUP BY from_addr ORDER BY cnt DESC LIMIT ?',
            args + [n]).fetchall()]
    finally:
        conn.close()

    if not rows:
        return {"text": "За этот период писем нет."}

    lines = []
    for i, r in enumerate(rows, 1):
        name = r["from_name"] or r["from_addr"] or "?"
        addr = f" <{r['from_addr']}>" if r["from_name"] and r["from_addr"] else ""
        un = f"  ({r['unseen']} новых)" if r["unseen"] else ""
        lines.append(f"{i:>2}. {name}{addr} — {r['cnt']}{un}")

    period = _period_word(days)
    return {
        "text": f"Топ отправителей за {period}:\n" + "\n".join(lines),
    }


def _answer_overview(days: int) -> dict:
    conn, table = _open()
    try:
        where, args = _where(days, [], [])
        total = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE {where}', args).fetchone()[0]
        unseen = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE {where} AND seen = 0',
            args).fetchone()[0]
        attach = conn.execute(
            f'SELECT COUNT(*) FROM "{table}" WHERE {where} AND attachments > 0',
            args).fetchone()[0]
        top = [dict(r) for r in conn.execute(
            f'SELECT from_addr, from_name, COUNT(*) AS cnt '
            f'FROM "{table}" WHERE {where} '
            f'GROUP BY from_addr ORDER BY cnt DESC LIMIT 5',
            args).fetchall()]
        days_rows = [dict(r) for r in conn.execute(
            f'SELECT substr(date,1,10) AS d, COUNT(*) AS n '
            f'FROM "{table}" WHERE {where} GROUP BY d ORDER BY d',
            args).fetchall()]
    finally:
        conn.close()

    period = _period_word(days)
    lines = [f"Сводка за {period}:",
             f"  · всего: {total}",
             f"  · непрочитанных: {unseen}",
             f"  · с вложениями: {attach}"]

    if top:
        lines.append("")
        lines.append("Чаще всего пишут:")
        for r in top:
            name = r["from_name"] or r["from_addr"] or "?"
            lines.append(f"  · {name} — {r['cnt']}")

    if days_rows:
        lines.append("")
        lines.append("По дням (последние 10 дней с письмами):")
        for r in days_rows[-10:]:
            bar = "▮" * min(40, r["n"])
            lines.append(f"  {r['d']}  {bar} {r['n']}")

    return {"text": "\n".join(lines)}


def _period_word(days: int) -> str:
    if days <= 1: return "сегодня"
    if days == 2: return "вчера и сегодня"
    if days == 7: return "неделю"
    if days == 30: return "месяц"
    if days == 365: return "год"
    return f"{days} дней"


def _format_row(r: dict) -> dict:
    date = (r.get("date") or "")[:16].replace("T", " ")
    subj = r.get("subject") or "(без темы)"
    sender = r.get("from_name") or r.get("from_addr") or "?"
    flags = []
    if not r.get("seen"): flags.append("новое")
    if r.get("attachments"): flags.append(f"{r['attachments']} влож.")
    return {
        "date": date,
        "from": sender,
        "subject": subj,
        "flags": ", ".join(flags),
        "message_id": r.get("message_id"),
    }


# ──────────────────────────────────────────────────────────────
#  Входная точка
# ──────────────────────────────────────────────────────────────
def answer(question: str) -> dict:
    intent = parse(question)
    kind = intent["intent"]

    if kind == "help":
        return _answer_help()
    if kind == "count":
        return _answer_count(intent.get("days", 30))
    if kind == "top_senders":
        return _answer_top_senders(intent.get("days", 30),
                                   intent.get("n", 10))
    if kind == "overview":
        return _answer_overview(intent.get("days", 30))
    if kind == "list":
        return _answer_list(
            days=intent.get("days", 30),
            q=intent.get("q"),
            from_addr=intent.get("from"),
            unseen=intent.get("unseen", False),
            attach=intent.get("attach", False),
            n=intent.get("n", 20))
    return {"text": "Не понял вопрос. Спросите про письма или напишите help."}


def info() -> dict:
    return {"help": HELP_TEXT, "suggestions": SUGGESTIONS}
