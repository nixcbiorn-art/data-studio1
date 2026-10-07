"""web_data — спецификации, базы, выборки, история запусков и аномалии."""
from __future__ import annotations

import json
import math
import sqlite3
import statistics
import traceback
from redcat.data import dataops
from redcat.sources import name_normalizer
from redcat.sources import registry as src
from redcat.data import studio_store as store
from redcat.web.web_config import DATA_DB, EXTERNAL_DB, SOURCES_DIR, SOURCES_EXT_DIR, STATS_DB, STUDIO_DB


# ──────────────────────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНОЕ
# ──────────────────────────────────────────────────────────────
def load_specs() -> dict:
    try:
        src.load_from_dir(SOURCES_DIR)
        src.load_from_dir(SOURCES_EXT_DIR)
        return {s.key: s for s in src.all_sources()}
    except Exception as e:
        print(f"\n⚠️ load_specs: не удалось загрузить источники")
        print(f"   {type(e).__name__}: {e}")
        traceback.print_exc()
        return {}


_ext_tables_cache: set | None = None


_ext_tables_mtime: float = 0.0


def _external_table_names() -> set:
    """Имена таблиц в external_data.db. Кэш сбрасывается по mtime файла:
    после сборки сервер сразу видит новые таблицы, без перезапуска."""
    global _ext_tables_cache, _ext_tables_mtime
    try:
        current_mtime = EXTERNAL_DB.stat().st_mtime
    except OSError:
        current_mtime = 0.0

    if _ext_tables_cache is None or current_mtime != _ext_tables_mtime:
        try:
            _ext_tables_cache = {t["name"]
                                 for t in dataops.list_tables(EXTERNAL_DB)}
        except dataops.DataError:
            _ext_tables_cache = set()
        _ext_tables_mtime = current_mtime
    return _ext_tables_cache


def db_for_table(table: str):
    """В какой базе искать эту таблицу."""
    return EXTERNAL_DB if table in _external_table_names() else DATA_DB


# ──────────────────────────────────────────────────────────────
#  SQL-КОНСОЛЬ: РАБОТА СО ВСЕМИ БАЗАМИ
# ──────────────────────────────────────────────────────────────
# id → (путь, человекочитаемое название). Используется в /api/sql,
# /api/sql_databases, /api/sql_schema и в экспорте результата SELECT.
def sql_databases() -> list:
    items = []
    for db_id, path, title in (
        ("redcat",   DATA_DB,     "Redcat — собранные данные"),
        ("external", EXTERNAL_DB, "Внешние источники"),
        ("studio",   STUDIO_DB,   "Мои правки, заметки, журнал API"),
        ("stats",    STATS_DB,    "История запусков и аномалии"),
    ):
        if not path.exists():
            continue
        items.append({
            "id": db_id,
            "title": title,
            "path": str(path),
            "name": path.name,
            "size": path.stat().st_size,
        })
    return items


def resolve_sql_db(db_id: str):
    """Имя базы → путь. Понятная ошибка, если базы нет."""
    mapping = {
        "redcat":   DATA_DB,
        "external": EXTERNAL_DB,
        "studio":   STUDIO_DB,
        "stats":    STATS_DB,
    }
    if not db_id:
        db_id = "redcat"
    if db_id not in mapping:
        raise dataops.DataError(
            f"Неизвестная база «{db_id}». Доступные: "
            f"{', '.join(sorted(mapping))}.")
    path = mapping[db_id]
    if not path.exists():
        raise dataops.DataError(
            f"Файл базы не найден: {path.name}. "
            f"Запустите сбор или откройте вкладку, которая её создаёт.")
    return path


def id_field_for(table, cols) -> str:
    spec = load_specs().get(table)
    names = [c["name"] for c in cols]
    if spec and spec.id_field in names:
        return spec.id_field
    if "id" in names:
        return "id"
    return names[0] if names else "rowid"


def name_field_for(table, cols) -> str:
    spec = load_specs().get(table)
    names = [c["name"] for c in cols]
    if spec and spec.name_field in names:
        return spec.name_field
    for c in names:
        if "name" in c.lower() or "title" in c.lower() or "назв" in c.lower():
            return c
    return id_field_for(table, cols)


def decorate(rows, table, id_field):
    ids = [r.get(id_field) for r in rows]
    overlay = store.overlay_for(STUDIO_DB, table, ids)
    notes = store.notes_for(STUDIO_DB, table, ids)
    tags = store.tags_for(STUDIO_DB, table, ids)
    starred = store.flagged_ids(STUDIO_DB, table, store.FLAG_STAR)
    hidden = store.flagged_ids(STUDIO_DB, table, store.FLAG_HIDDEN)
    for row in rows:
        rid = str(row.get(id_field))
        patch = overlay.get(rid)
        if patch:
            row["_original"] = {k: row.get(k) for k in patch}
            for field, value in patch.items():
                row[field] = _retype(row.get(field), value)
            row["_edited"] = list(patch)
        if rid in notes:
            row["_note"] = notes[rid]
        if rid in tags:
            row["_tags"] = tags[rid]
        if rid in starred:
            row["_star"] = True
        if rid in hidden:
            row["_hidden"] = True
    return rows


def _retype(original, value):
    if isinstance(original, (int, float)) and not isinstance(original, bool):
        try:
            num = float(str(value).replace(",", "."))
            return int(num) if isinstance(original, int) and num.is_integer() else num
        except (TypeError, ValueError):
            return value
    return value


def parse_query(params) -> dict:
    raw = params.get("q", ["{}"])[0]
    try:
        q = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        q = {}
    return q if isinstance(q, dict) else {}


def runs_history(limit=200) -> list:
    if not STATS_DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{STATS_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT ?",
                            (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in reversed(rows)]
    except sqlite3.Error:
        return []


def anomalies_list(severity=None, source=None, kind=None, limit=400) -> list:
    if not STATS_DB.exists():
        return []
    sql = "SELECT * FROM anomalies WHERE 1=1"
    args = []
    for column, value in (("severity", severity), ("source", source),
                          ("kind", kind)):
        if value:
            sql += f" AND {column} = ?"
            args.append(value)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    try:
        conn = sqlite3.connect(f"file:{STATS_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def record_series(source, record_id, field) -> list:
    if not STATS_DB.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{STATS_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT run_id, ts, value FROM record_history WHERE source=? "
            "AND record_id=? AND field=? ORDER BY run_id",
            (source, str(record_id), field)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def source_files() -> list:
    out = []
    specs = load_specs()
    for path, key, title, url in src.list_source_files(SOURCES_DIR):
        out.append({"file": path.name, "key": key, "title": title, "url": url,
                    "external": False,
                    "has_cross_check": bool(getattr(specs.get(key),
                                                    "cross_check", None))})
    for path, key, title, url in src.list_source_files(SOURCES_EXT_DIR):
        out.append({"file": path.name, "key": key, "title": title, "url": url,
                    "external": True,
                    "has_cross_check": bool(getattr(specs.get(key),
                                                    "cross_check", None))})
    return out


def _json_safe(obj):
    """Заменяет NaN и ±Infinity на None перед отправкой в браузер.

    В стандарте JSON (RFC 8259) таких чисел нет. Модуль json по умолчанию
    их пишет как есть, но JavaScript-функция JSON.parse на них падает с
    «Unexpected token 'I'», и весь ответ теряется.
    """
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


# ──────────────────────────────────────────────────────────────
#  СВЕРКА ИСТОЧНИКОВ
# ──────────────────────────────────────────────────────────────
def _aggregate(values, agg: str):
    """Считает одну агрегацию. Возвращает (значение, количество_чисел)."""
    nums = []
    for v in values:
        if v is None or isinstance(v, bool):
            continue
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            continue
    if not nums:
        return None, 0
    if agg == "avg":
        return round(statistics.fmean(nums), 2), len(nums)
    if agg == "sum":
        return round(sum(nums), 2), len(nums)
    if agg == "count":
        return len(nums), len(nums)
    if agg == "min":
        return min(nums), len(nums)
    if agg == "max":
        return max(nums), len(nums)
    # дефолт — медиана
    return round(statistics.median(nums), 2), len(nums)


def _normalize_key(raw, aliases: dict | None = None) -> str | None:
    """Нормализует имя ЖК (name_normalizer) и применяет словарь синонимов."""
    return name_normalizer.canon(raw, aliases)
