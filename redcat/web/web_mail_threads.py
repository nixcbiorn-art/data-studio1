"""web_mail_threads — группировка писем в треды по теме.

Нормализация темы:
  • убираем префиксы Re:, RE:, Fwd:, FWD:, FW:, Ответ:, Пересл:
  • убираем кратные префиксы (Re[2]:, Re(2):, Re Re:)
  • убираем квадратные и круглые скобки вида [External], (внешнее)
  • схлопываем пробелы, приводим к нижнему регистру

Группировка:
  • все письма с одной нормализованной темой → один тред
  • внутри треда — сортировка по дате (старые сверху)
  • одиночные письма (тред из 1) могут скрываться — параметр
    min_size
"""
from __future__ import annotations

import re
import sqlite3

from redcat.web.web_data import db_for_table, load_specs

MAIL_KEY_DEFAULT = "mail_inbox"


# ──────────────────────────────────────────────────────────────
#  Нормализация
# ──────────────────────────────────────────────────────────────
# Список префиксов. Может повторяться многократно: "Re: Re: Fwd: ...".
_PREFIX_RE = re.compile(
    r"^\s*(?:(?:re|fwd?|fw|ответ|пересл|otv|vs)\s*(?:\[\d+\]|\(\d+\))?"
    r"\s*[:\-]\s*)+",
    re.I | re.UNICODE)

# Скобочные пометки: [External], (внешнее), [SPAM] и т.п.
_BRACKET_RE = re.compile(r"[\[\(]\s*(?:external|внешн[а-я]*|spam|"
                         r"spam\]|не\s+спам|list|mailing)[\]\)]",
                         re.I | re.UNICODE)

# Несколько пробелов — в один
_SPACES_RE = re.compile(r"\s+")

# Если тема заканчивается на "(Fwd)" / "(Переслано)" — режем и это
_TAIL_RE = re.compile(r"\s*[\[\(]?\s*(?:fwd|переслано|forward)\s*[\]\)]?\s*$",
                       re.I | re.UNICODE)


def normalize_subject(subject: str) -> str:
    """Приводит тему к каноническому виду для группировки.

    Возвращает пустую строку, если тема пустая — такие письма
    собираются в один тред «(без темы)».
    """
    if not subject:
        return ""
    s = str(subject).strip()
    # Снимаем префиксы в цикле — их может быть несколько подряд.
    prev = None
    while prev != s:
        prev = s
        s = _PREFIX_RE.sub("", s)
    # Убираем квадратные пометки
    s = _BRACKET_RE.sub("", s)
    # Убираем хвостовой (Fwd)
    s = _TAIL_RE.sub("", s)
    # Схлопываем пробелы, регистр
    s = _SPACES_RE.sub(" ", s).strip().lower()
    return s


# ──────────────────────────────────────────────────────────────
#  Запрос к базе
# ──────────────────────────────────────────────────────────────
def _mail_table() -> str:
    keys = [k for k in load_specs() if k.startswith("mail_")]
    if MAIL_KEY_DEFAULT in keys:
        return MAIL_KEY_DEFAULT
    return keys[0] if keys else MAIL_KEY_DEFAULT


def build_threads(days: int = 90, min_size: int = 1,
                  only_unseen: bool = False,
                  max_rows: int = 5000) -> dict:
    """Читает письма за окно и группирует по нормализованной теме.

    Параметры:
      days        — окно в днях
      min_size    — минимум писем в треде (2 = только переписки)
      only_unseen — только треды с непрочитанными письмами
      max_rows    — предохранитель от слишком больших выборок

    Возвращает:
      {
        "total_messages": int,
        "total_threads": int,
        "window_days": days,
        "threads": [
          {
            "key": str,              # нормализованная тема
            "subject": str,          # заголовок для UI
            "count": int,
            "unseen": int,
            "last_date": iso,
            "first_date": iso,
            "senders": [...],        # уникальные адреса
            "messages": [ {..}, ..]  # письма, старые → новые
          }, ...
        ]
      }
    """
    from datetime import datetime, timedelta
    since = (datetime.now() - timedelta(days=days)).isoformat()

    table = _mail_table()
    db = db_for_table(table)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            f'SELECT message_id, uid, folder, date, from_addr, from_name, '
            f'"to", subject, size, attachments, seen '
            f'FROM "{table}" WHERE date >= ? '
            f'ORDER BY date DESC LIMIT ?',
            (since, max_rows)).fetchall()]
    finally:
        conn.close()

    # Группировка
    groups: dict = {}
    for r in rows:
        key = normalize_subject(r.get("subject") or "")
        g = groups.setdefault(key, {
            "key": key,
            "subject": r.get("subject") or "(без темы)",
            "messages": [],
            "unseen": 0,
            "senders": set(),
        })
        g["messages"].append(r)
        if not r.get("seen"):
            g["unseen"] += 1
        if r.get("from_addr"):
            g["senders"].add(r["from_addr"])
        # Заголовок треда — берём самую «чистую» тему: без Re: и прочего.
        # Обычно это самое старое письмо в треде.
        # Обновляем: если текущая тема письма не имеет префикса, берём её.
        raw = r.get("subject") or ""
        if raw and normalize_subject(raw) == raw.lower().strip():
            g["subject"] = raw

    # Подсветка меток
    try:
        from redcat.web.web_mail_labels import labels_for
        all_mids = [r["message_id"] for r in rows if r.get("message_id")]
        labels_map = labels_for(table, all_mids)
    except Exception:  # noqa: BLE001
        labels_map = {}

    # Собираем итог
    threads = []
    for key, g in groups.items():
        msgs = g["messages"]
        if len(msgs) < min_size:
            continue
        if only_unseen and g["unseen"] == 0:
            continue

        # Внутри — старые → новые
        msgs.sort(key=lambda r: r.get("date") or "")
        for m in msgs:
            m["labels"] = labels_map.get(m.get("message_id") or "", [])
            m.pop("uid", None)  # не нужно во фронте

        threads.append({
            "key": key or "(без темы)",
            "subject": g["subject"],
            "count": len(msgs),
            "unseen": g["unseen"],
            "first_date": msgs[0].get("date") or "",
            "last_date": msgs[-1].get("date") or "",
            "senders": sorted(g["senders"]),
            "messages": msgs,
        })

    # Сортировка тредов: свежие сверху
    threads.sort(key=lambda t: t.get("last_date") or "", reverse=True)

    return {
        "total_messages": len(rows),
        "total_threads": len(threads),
        "window_days": days,
        "threads": threads,
    }
