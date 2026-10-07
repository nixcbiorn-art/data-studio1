"""
ЛОКАЛЬНОЕ ХРАНИЛИЩЕ ПРИЛОЖЕНИЯ (studio.db)
==========================================
Здесь живёт всё, что создаёт пользователь: правки ячеек, заметки, теги,
избранное, скрытые записи, сохранённые фильтры, вычисляемые колонки,
журнал обращений к API и поисковый индекс.

Почему отдельная база, а не та же redcat_data.db
------------------------------------------------
Сборщик пересоздаёт свои таблицы при каждом запуске (`if_exists="replace"`).
Если бы правки лежали там же, следующий сбор стирал бы их подчистую.
Поэтому правки хранятся отдельным «слоем» и накладываются на данные при
чтении — данные обновляются, правки остаются.

Ключевой принцип
----------------
Ни одна функция этого модуля не обращается к сети. Правка означает
«поправить свою копию у себя на диске» и никогда — «отправить изменение
в API». Наружу приложение ходит только GET-ом (см. api_guard.py).
Накопленные правки можно выгрузить списком (CSV/JSON) и передать тому,
кто вносит изменения в первоисточник вручную.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    record_id TEXT NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    author TEXT,
    note TEXT,
    ts TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_edits_key ON edits(source, record_id, field, active);
CREATE INDEX IF NOT EXISTS idx_edits_src ON edits(source, active);

CREATE TABLE IF NOT EXISTS notes (
    source TEXT NOT NULL,
    record_id TEXT NOT NULL,
    note TEXT,
    ts TEXT,
    PRIMARY KEY (source, record_id)
);

CREATE TABLE IF NOT EXISTS tags (
    source TEXT NOT NULL,
    record_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    ts TEXT,
    PRIMARY KEY (source, record_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);

CREATE TABLE IF NOT EXISTS flags (
    source TEXT NOT NULL,
    record_id TEXT NOT NULL,
    flag TEXT NOT NULL,
    ts TEXT,
    PRIMARY KEY (source, record_id, flag)
);
CREATE INDEX IF NOT EXISTS idx_flags_flag ON flags(source, flag);

CREATE TABLE IF NOT EXISTS views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts TEXT
);

CREATE TABLE IF NOT EXISTS computed (
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    expr TEXT NOT NULL,
    ts TEXT,
    PRIMARY KEY (source, name)
);

CREATE TABLE IF NOT EXISTS api_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, method TEXT, url TEXT, status INTEGER, ms REAL, note TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON api_audit(id DESC);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Флаги записи: звезда (следить) и скрытие (не мешать в выдаче)
FLAG_STAR = "star"
FLAG_HIDDEN = "hidden"
VALID_FLAGS = (FLAG_STAR, FLAG_HIDDEN)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init(path) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


# ──────────────────────────────────────────────────────────────
#  ПРАВКИ
# ──────────────────────────────────────────────────────────────
def set_edit(path, source, record_id, field, new_value,
             old_value=None, author="", note="") -> dict:
    """Сохраняет правку одной ячейки. Прошлая правка того же поля гасится.

    Старое значение сохраняется, чтобы правку всегда можно было откатить
    и чтобы в выгрузке было видно «было → стало».
    """
    record_id, field = str(record_id), str(field)
    with connect(path) as conn:
        conn.execute(
            "UPDATE edits SET active = 0 WHERE source=? AND record_id=? "
            "AND field=? AND active=1", (source, record_id, field))
        cur = conn.execute(
            "INSERT INTO edits (source, record_id, field, old_value, new_value,"
            " author, note, ts, active) VALUES (?,?,?,?,?,?,?,?,1)",
            (source, record_id, field, _text(old_value), _text(new_value),
             author or "local", note or "", _now()))
        conn.commit()
        return {"id": cur.lastrowid, "source": source, "record_id": record_id,
                "field": field, "new_value": new_value}


def set_edits_bulk(path, source, field, pairs, author="", note="") -> int:
    """Массовая правка: pairs = [(record_id, old_value, new_value), ...]."""
    if not pairs:
        return 0
    ts = _now()
    with connect(path) as conn:
        conn.executemany(
            "UPDATE edits SET active = 0 WHERE source=? AND record_id=? "
            "AND field=? AND active=1",
            [(source, str(rid), field) for rid, _, _ in pairs])
        conn.executemany(
            "INSERT INTO edits (source, record_id, field, old_value, new_value,"
            " author, note, ts, active) VALUES (?,?,?,?,?,?,?,?,1)",
            [(source, str(rid), field, _text(old), _text(new),
              author or "local", note or "", ts) for rid, old, new in pairs])
        conn.commit()
    return len(pairs)


def revert_edit(path, edit_id) -> None:
    with connect(path) as conn:
        conn.execute("UPDATE edits SET active = 0 WHERE id = ?", (edit_id,))
        conn.commit()


def revert_scope(path, source, record_id=None, field=None) -> int:
    """Откат: вся таблица / одна запись / одно поле записи."""
    sql = "UPDATE edits SET active = 0 WHERE active = 1 AND source = ?"
    args = [source]
    if record_id is not None:
        sql += " AND record_id = ?"
        args.append(str(record_id))
    if field:
        sql += " AND field = ?"
        args.append(field)
    with connect(path) as conn:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.rowcount


def overlay_for(path, source, record_ids) -> dict:
    """{record_id: {field: new_value}} — активные правки для набора записей."""
    ids = [str(r) for r in record_ids if r is not None]
    if not ids:
        return {}
    out: dict = {}
    with connect(path) as conn:
        for chunk in _chunks(ids, 400):
            marks = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"SELECT record_id, field, new_value FROM edits WHERE active=1 "
                f"AND source=? AND record_id IN ({marks})", [source] + chunk)
            for r in rows:
                out.setdefault(r["record_id"], {})[r["field"]] = r["new_value"]
    return out


def edited_ids(path, source) -> list:
    with connect(path) as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT record_id FROM edits WHERE active=1 AND source=?",
            (source,))]


def list_edits(path, source=None, limit=1000) -> list:
    sql = ("SELECT id, source, record_id, field, old_value, new_value, author,"
           " note, ts FROM edits WHERE active=1")
    args = []
    if source:
        sql += " AND source = ?"
        args.append(source)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def edit_history(path, source, record_id) -> list:
    """Полная история правок записи, включая отменённые."""
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM edits WHERE source=? AND record_id=? ORDER BY id DESC",
            (source, str(record_id)))]


def edit_counts(path) -> dict:
    with connect(path) as conn:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT source, COUNT(*) FROM edits WHERE active=1 GROUP BY source")}


# ──────────────────────────────────────────────────────────────
#  ЗАМЕТКИ, ТЕГИ, ФЛАГИ
# ──────────────────────────────────────────────────────────────
def set_note(path, source, record_id, note) -> None:
    with connect(path) as conn:
        if note:
            conn.execute(
                "INSERT OR REPLACE INTO notes (source, record_id, note, ts) "
                "VALUES (?,?,?,?)", (source, str(record_id), note, _now()))
        else:
            conn.execute("DELETE FROM notes WHERE source=? AND record_id=?",
                         (source, str(record_id)))
        conn.commit()


def notes_for(path, source, record_ids) -> dict:
    ids = [str(r) for r in record_ids if r is not None]
    if not ids:
        return {}
    out = {}
    with connect(path) as conn:
        for chunk in _chunks(ids, 400):
            marks = ",".join("?" for _ in chunk)
            for r in conn.execute(
                    f"SELECT record_id, note FROM notes WHERE source=? "
                    f"AND record_id IN ({marks})", [source] + chunk):
                out[r["record_id"]] = r["note"]
    return out


def toggle_tag(path, source, record_id, tag, on=True) -> None:
    with connect(path) as conn:
        if on:
            conn.execute("INSERT OR REPLACE INTO tags VALUES (?,?,?,?)",
                         (source, str(record_id), tag, _now()))
        else:
            conn.execute("DELETE FROM tags WHERE source=? AND record_id=? AND tag=?",
                         (source, str(record_id), tag))
        conn.commit()


def tags_for(path, source, record_ids) -> dict:
    ids = [str(r) for r in record_ids if r is not None]
    if not ids:
        return {}
    out: dict = {}
    with connect(path) as conn:
        for chunk in _chunks(ids, 400):
            marks = ",".join("?" for _ in chunk)
            for r in conn.execute(
                    f"SELECT record_id, tag FROM tags WHERE source=? "
                    f"AND record_id IN ({marks})", [source] + chunk):
                out.setdefault(r["record_id"], []).append(r["tag"])
    return out


def all_tags(path, source=None) -> list:
    sql = "SELECT tag, COUNT(*) AS n FROM tags"
    args = []
    if source:
        sql += " WHERE source = ?"
        args.append(source)
    sql += " GROUP BY tag ORDER BY n DESC"
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def toggle_flag(path, source, record_id, flag, on=True) -> None:
    if flag not in VALID_FLAGS:
        raise ValueError(f"Неизвестный флаг: {flag}")
    with connect(path) as conn:
        if on:
            conn.execute("INSERT OR REPLACE INTO flags VALUES (?,?,?,?)",
                         (source, str(record_id), flag, _now()))
        else:
            conn.execute("DELETE FROM flags WHERE source=? AND record_id=? AND flag=?",
                         (source, str(record_id), flag))
        conn.commit()


def flagged_ids(path, source, flag) -> set:
    with connect(path) as conn:
        return {r[0] for r in conn.execute(
            "SELECT record_id FROM flags WHERE source=? AND flag=?", (source, flag))}


def flag_counts(path) -> dict:
    with connect(path) as conn:
        return {f"{r[0]}::{r[1]}": r[2] for r in conn.execute(
            "SELECT source, flag, COUNT(*) FROM flags GROUP BY source, flag")}


# ──────────────────────────────────────────────────────────────
#  СОХРАНЁННЫЕ ПРЕДСТАВЛЕНИЯ И ВЫЧИСЛЯЕМЫЕ КОЛОНКИ
# ──────────────────────────────────────────────────────────────
def save_view(path, name, source, payload) -> int:
    with connect(path) as conn:
        conn.execute("DELETE FROM views WHERE name=? AND source=?", (name, source))
        cur = conn.execute(
            "INSERT INTO views (name, source, payload, ts) VALUES (?,?,?,?)",
            (name, source, json.dumps(payload, ensure_ascii=False), _now()))
        conn.commit()
        return cur.lastrowid


def list_views(path, source=None) -> list:
    sql = "SELECT id, name, source, payload, ts FROM views"
    args = []
    if source:
        sql += " WHERE source = ?"
        args.append(source)
    sql += " ORDER BY name"
    with connect(path) as conn:
        out = []
        for r in conn.execute(sql, args):
            item = dict(r)
            try:
                item["payload"] = json.loads(item["payload"])
            except json.JSONDecodeError:
                item["payload"] = {}
            out.append(item)
        return out


def delete_view(path, view_id) -> None:
    with connect(path) as conn:
        conn.execute("DELETE FROM views WHERE id = ?", (view_id,))
        conn.commit()


def save_computed(path, source, name, expr) -> None:
    with connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO computed VALUES (?,?,?,?)",
                     (source, name, expr, _now()))
        conn.commit()


def list_computed(path, source=None) -> list:
    sql = "SELECT source, name, expr FROM computed"
    args = []
    if source:
        sql += " WHERE source = ?"
        args.append(source)
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def delete_computed(path, source, name) -> None:
    with connect(path) as conn:
        conn.execute("DELETE FROM computed WHERE source=? AND name=?", (source, name))
        conn.commit()


# ──────────────────────────────────────────────────────────────
#  ЖУРНАЛ ОБРАЩЕНИЙ К API
# ──────────────────────────────────────────────────────────────
def log_api(path, method, url, status, ms, note="") -> None:
    """Пишет строку в журнал. Вызывается из api_guard на каждый запрос."""
    try:
        with connect(path) as conn:
            conn.execute(
                "INSERT INTO api_audit (ts, method, url, status, ms, note) "
                "VALUES (?,?,?,?,?,?)",
                (_now(), method, url[:600], status, ms, note))
            conn.commit()
    except sqlite3.Error:
        pass  # журнал не должен мешать сбору данных


def list_api_log(path, limit=300, only_blocked=False) -> list:
    sql = "SELECT ts, method, url, status, ms, note FROM api_audit"
    if only_blocked:
        sql += " WHERE note <> ''"
    sql += " ORDER BY id DESC LIMIT ?"
    with connect(path) as conn:
        return [dict(r) for r in conn.execute(sql, (limit,))]


def api_log_summary(path) -> dict:
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT method, COUNT(*) n, SUM(note <> '') blocked FROM api_audit "
            "GROUP BY method").fetchall()
    return {
        "by_method": {r["method"]: r["n"] for r in rows},
        "blocked": sum((r["blocked"] or 0) for r in rows),
        "total": sum(r["n"] for r in rows),
    }


def prune_api_log(path, keep=5000) -> None:
    with connect(path) as conn:
        conn.execute(
            "DELETE FROM api_audit WHERE id <= (SELECT MAX(id) - ? FROM api_audit)",
            (keep,))
        conn.commit()


# ──────────────────────────────────────────────────────────────
#  ПОИСКОВЫЙ ИНДЕКС (FTS5)
# ──────────────────────────────────────────────────────────────
def search_available(path) -> bool:
    try:
        with connect(path) as conn:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
            conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.Error:
        return False


def rebuild_search_index(path, documents) -> int:
    """documents — итератор кортежей (source, record_id, title, body)."""
    with connect(path) as conn:
        conn.execute("DROP TABLE IF EXISTS search_idx")
        conn.execute("CREATE VIRTUAL TABLE search_idx USING fts5("
                     "source UNINDEXED, record_id UNINDEXED, title, body, "
                     "tokenize='unicode61 remove_diacritics 2')")
        count = 0
        batch = []
        for doc in documents:
            batch.append(doc)
            if len(batch) >= 1000:
                conn.executemany("INSERT INTO search_idx VALUES (?,?,?,?)", batch)
                count += len(batch)
                batch = []
        if batch:
            conn.executemany("INSERT INTO search_idx VALUES (?,?,?,?)", batch)
            count += len(batch)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('search_built_at', ?)",
                     (_now(),))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('search_docs', ?)",
                     (str(count),))
        conn.commit()
    return count


def search(path, query, source=None, limit=100) -> list:
    """Полнотекстовый поиск по всем таблицам сразу."""
    if not query or not query.strip():
        return []
    sql = ("SELECT source, record_id, title, "
           "snippet(search_idx, 3, '«', '»', ' … ', 12) AS snippet, "
           "bm25(search_idx) AS rank FROM search_idx WHERE search_idx MATCH ?")
    args = [_fts_query(query)]
    if source:
        sql += " AND source = ?"
        args.append(source)
    sql += " ORDER BY rank LIMIT ?"
    args.append(limit)
    try:
        with connect(path) as conn:
            return [dict(r) for r in conn.execute(sql, args)]
    except sqlite3.OperationalError as e:
        return [{"error": f"Поиск недоступен: {e}. Перестройте индекс."}]


def _fts_query(raw: str) -> str:
    """Превращает пользовательский ввод в безопасный запрос FTS5.

    Пользователь пишет «жк солнечный 45», а не синтаксис FTS. Служебные
    символы экранируются, каждое слово получает префиксный поиск — так
    находится «Солнечный» по вводу «солне».
    """
    words = [w for w in raw.replace('"', " ").replace("'", " ").split() if w]
    if not words:
        return '""'
    return " ".join(f'"{w}"*' for w in words)


def search_meta(path) -> dict:
    try:
        with connect(path) as conn:
            rows = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
        return {"built_at": rows.get("search_built_at"),
                "docs": int(rows.get("search_docs") or 0)}
    except sqlite3.Error:
        return {"built_at": None, "docs": 0}


# ──────────────────────────────────────────────────────────────
def _text(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def stats(path) -> dict:
    """Сводка по локальным данным — для шапки приложения."""
    out = {"edits": 0, "notes": 0, "tags": 0, "starred": 0, "hidden": 0, "views": 0}
    try:
        with connect(path) as conn:
            out["edits"] = conn.execute(
                "SELECT COUNT(*) FROM edits WHERE active=1").fetchone()[0]
            out["notes"] = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
            out["tags"] = conn.execute(
                "SELECT COUNT(DISTINCT tag) FROM tags").fetchone()[0]
            out["starred"] = conn.execute(
                "SELECT COUNT(*) FROM flags WHERE flag='star'").fetchone()[0]
            out["hidden"] = conn.execute(
                "SELECT COUNT(*) FROM flags WHERE flag='hidden'").fetchone()[0]
            out["views"] = conn.execute("SELECT COUNT(*) FROM views").fetchone()[0]
    except sqlite3.Error:
        pass
    return out


if __name__ == "__main__":  # быстрый ручной прогон
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "studio.db"
    init(p)
    set_edit(p, "apartments", 1, "price", 100, old_value=90, note="тест")
    print(overlay_for(p, "apartments", [1]), stats(p))
