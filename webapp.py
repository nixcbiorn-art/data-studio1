"""
СЕРВЕР ПРИЛОЖЕНИЯ
=================
Локальное веб-приложение поверх собранных данных. Работает на стандартной
библиотеке Python — ставить ничего не нужно, интернет не требуется.

Про запросы и «никаких PUT в API»
---------------------------------
  • Сторонний API — приложение обращается к нему ИСКЛЮЧИТЕЛЬНО методом GET.
    Это заблокировано на уровне сетевых библиотек (api_guard.py). Для
    источников с fetch_mode="browser" то же правило работает внутри
    Playwright (см. browser_fetch.py::_route_filter).

  • Этот локальный сервер (127.0.0.1) — браузер разговаривает с ним же на
    вашем компьютере. Чтение идёт через GET, сохранение правок — через
    POST на localhost. POST никуда не уходит: он пишет строку в файл
    studio.db на вашем диске.

Две базы данных
---------------
Redcat и внешние источники хранятся раздельно:
  • reports/redcat_data.db     — источники из папки sources/
  • reports/external_data.db   — источники из папки sources_external/
Куда писать, определяется полем external в spec (см. sources.py).
Сервер сам определяет нужную БД по имени таблицы (db_for_table).

Сверка источников
-----------------
Маршрут /api/cross_check?source=<внешний_ключ> сопоставляет таблицу
внешнего источника с другой таблицей (обычно apartments из Redcat) по
правилам из секции cross_check в spec. Плюс подмешивает общий словарь
синонимов из hc_aliases.json, если он есть.

Сервер слушает только 127.0.0.1, то есть недоступен из сети.
"""

from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import socket
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import api_guard
import dataops
import completeness
import field_labels
import hc_aliases
import name_normalizer
import sources as src
import spec_validator
import studio_store as store

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
OUTPUT_DIR = BASE_DIR / "reports"
HISTORY_DIR = BASE_DIR / "history"
SOURCES_DIR = BASE_DIR / "sources"
SOURCES_EXT_DIR = BASE_DIR / "sources_external"
DATA_DB = OUTPUT_DIR / "redcat_data.db"
EXTERNAL_DB = OUTPUT_DIR / "external_data.db"
STATS_DB = HISTORY_DIR / "run_stats.db"
STUDIO_DB = BASE_DIR / "studio.db"
ENV_FILE = BASE_DIR / ".env"

api_guard.install()
api_guard.set_audit_hook(
    lambda m, u, s, ms, note="": store.log_api(STUDIO_DB, m, u, s, ms, note))


# ──────────────────────────────────────────────────────────────
#  ЗАПУСК СБОРЩИКА
# ──────────────────────────────────────────────────────────────
class ScraperRunner:
    """Держит один фоновый запуск сборщика и копит его вывод для интерфейса."""

    def __init__(self):
        self.process = None
        self.lines = deque(maxlen=4000)
        self.counter = 0
        self.started_at = None
        self.finished_at = None
        self.exit_code = None
        self.command = []
        self.lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, args, token=None) -> dict:
        with self.lock:
            if self.running:
                return {"ok": False, "error": "Сбор уже идёт."}
            cmd = [sys.executable, "-u", str(BASE_DIR / "redcat_scraper.py")] + args
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env.pop("REDCAT_TOKEN_ONESHOT", None)
            if token and _clean_token(token):
                env["REDCAT_TOKEN_ONESHOT"] = _clean_token(token)
            self.lines.clear()
            self.counter = 0
            self.command = cmd
            self.started_at = datetime.now().isoformat(timespec="seconds")
            self.finished_at = None
            self.exit_code = None
            self._append("$ python " + " ".join([Path(cmd[2]).name] + cmd[3:]))
            try:
                self.process = subprocess.Popen(
                    cmd, cwd=str(BASE_DIR), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env, text=True,
                    encoding="utf-8", errors="replace", bufsize=1)
            except OSError as e:
                self._append(f"❌ Не удалось запустить: {e}")
                return {"ok": False, "error": str(e)}
            threading.Thread(target=self._pump, daemon=True).start()
            return {"ok": True}

    def _pump(self):
        for line in self.process.stdout:
            self._append(line.rstrip("\n"))
        self.exit_code = self.process.wait()
        self.finished_at = datetime.now().isoformat(timespec="seconds")
        self._append(f"— процесс завершён, код {self.exit_code} —")

    def _append(self, line):
        self.lines.append({"n": self.counter, "text": line})
        self.counter += 1

    def stop(self) -> dict:
        with self.lock:
            if not self.running:
                return {"ok": False, "error": "Сбор не запущен."}
            self.process.terminate()
            self._append("⏹ Остановлено пользователем.")
            return {"ok": True}

    def state(self, since=0) -> dict:
        lines = [ln for ln in list(self.lines) if ln["n"] >= since]
        return {"running": self.running, "started_at": self.started_at,
                "finished_at": self.finished_at, "exit_code": self.exit_code,
                "lines": lines, "next": self.counter}


RUNNER = ScraperRunner()


# ──────────────────────────────────────────────────────────────
#  ТОКЕН
# ──────────────────────────────────────────────────────────────
def read_env_token() -> str:
    if not ENV_FILE.exists():
        return ""
    for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("REDCAT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("REDCAT_TOKEN", "")


def _clean_token(raw: str) -> str:
    t = re.sub(r"\s+", "", raw or "").strip("\"'")
    if t.lower().startswith("bearer"):
        t = t[6:]
    return t.strip("\"'")


def write_env_token(token: str) -> None:
    """LOCAL-ONLY: пишет токен в .env на вашем диске."""
    token = _clean_token(token)
    lines, found = [], False
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("REDCAT_TOKEN="):
                lines.append(f"REDCAT_TOKEN={token}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"REDCAT_TOKEN={token}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def token_status() -> dict:
    import base64
    token = read_env_token()
    if not token or "вставьте" in token:
        return {"state": "missing", "text": "Токен не задан"}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        exp = datetime.fromtimestamp(claims["exp"])
    except Exception:
        return {"state": "unknown", "text": "Токен задан (срок неизвестен)"}
    left = exp - datetime.now()
    if left.total_seconds() <= 0:
        return {"state": "expired",
                "text": f"Истёк {exp:%d.%m.%Y %H:%M}",
                "expires_at": exp.isoformat()}
    hours = left.total_seconds() / 3600
    return {"state": "soon" if hours < 24 else "ok",
            "text": f"Действует до {exp:%d.%m.%Y %H:%M} "
                    f"(осталось {int(hours)} ч)",
            "expires_at": exp.isoformat()}


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
    for path, key, title, url in src.list_source_files(SOURCES_DIR):
        out.append({"file": path.name, "key": key, "title": title, "url": url,
                    "external": False})
    for path, key, title, url in src.list_source_files(SOURCES_EXT_DIR):
        out.append({"file": path.name, "key": key, "title": title, "url": url,
                    "external": True})
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


_CSV_UPLOAD_STATE: dict = {}


def _csv_dedupe_idents(names: list) -> list:
    """Превращает произвольные заголовки CSV в безопасные и уникальные
    имена колонок SQLite: только буквы/цифры/подчёркивание, не с цифры,
    без повторов (второй «Цена» станет «Цена_2»)."""
    used = set()
    out = []
    for i, raw in enumerate(names):
        name = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_]+", "_", str(raw or "").strip())
        name = name.strip("_") or f"col_{i + 1}"
        if name[0].isdigit():
            name = f"c_{name}"
        base, n = name, 2
        while name in used:
            name = f"{base}_{n}"
            n += 1
        used.add(name)
        out.append(name)
    return out


def _csv_sniff_type(sample: list) -> str:
    """INTEGER/REAL/TEXT по образцу значений — чтобы загруженная колонка
    вела себя как собранная через API: сортировалась как число, попадала
    в распределения и выбросы, а не оставалась непрозрачным текстом."""
    if not sample:
        return "TEXT"

    def is_int(v):
        try:
            int(str(v).strip())
            return True
        except (TypeError, ValueError):
            return False

    def is_float(v):
        try:
            float(str(v).strip().replace(",", "."))
            return True
        except (TypeError, ValueError):
            return False

    if all(is_int(v) for v in sample):
        return "INTEGER"
    if all(is_float(v) for v in sample):
        return "REAL"
    return "TEXT"


def _csv_convert(value, sql_type):
    if value is None or value == "":
        return None
    if sql_type == "INTEGER":
        try:
            return int(str(value).strip())
        except ValueError:
            return None
    if sql_type == "REAL":
        try:
            return float(str(value).strip().replace(",", "."))
        except ValueError:
            return None
    return value


def _csv_pad_row(row: list, n: int) -> tuple:
    """Достраивает/обрезает строку до числа колонок — реальные CSV не
    всегда идеально прямоугольны (лишняя или недостающая ячейка в конце)."""
    row = list(row) + [None] * (n - len(row))
    return tuple(row[:n])


def _cross_check_report(source_key: str) -> dict:
    """Собирает отчёт по сверке одного внешнего источника с таблицей-соседом.

    Возвращает словарь с полями:
      source, with_table, on_left, on_right, metrics, agg,
      summary: {счётчики по классам}
      items_by_class: {класс: [записи]}
    """
    specs = load_specs()
    spec = specs.get(source_key)
    if spec is None:
        raise dataops.DataError(f"Источник «{source_key}» не найден")
    cc = getattr(spec, "cross_check", None) or {}
    if not cc:
        raise dataops.DataError(
            f"У источника «{source_key}» не задана секция cross_check в spec.")

    with_table = cc.get("with_table")
    on_left = cc.get("on_left")
    on_right = cc.get("on_right")
    metrics = cc.get("metrics") or {}
    agg = (cc.get("agg") or "median").lower()
    filter_right = cc.get("filter_right") or None
    filter_left = cc.get("filter_left") or None
    normalize_key = bool(cc.get("normalize_key", True))   # по умолчанию ВКЛ
    min_group_size = int(cc.get("min_group_size", 10))
    thresholds = cc.get("thresholds") or {}
    t_ok = float(thresholds.get("ok", 10))
    t_warn = float(thresholds.get("warn", 20))

    # Приоритет синонимов:
    #   1. spec.key_aliases (перекрывает всё — для специфики источника)
    #   2. hc_aliases.json (глобальный словарь, общий для всех источников)
    #   3. нормализованное имя как есть.
    aliases: dict[str, str] = {}

    # Сначала глобальный словарь
    for k, v in hc_aliases._load().items():
        if k and v:
            aliases[k] = v

    # Затем spec.key_aliases — перекрывает глобальные, если совпадает ключ
    raw_aliases = cc.get("key_aliases") or {}
    for k, v in raw_aliases.items():
        nk = _normalize_key(k) if normalize_key else str(k).strip().lower()
        nv = _normalize_key(v) if normalize_key else str(v).strip().lower()
        if nk and nv:
            aliases[nk] = nv

    if not (with_table and on_left and on_right and metrics):
        raise dataops.DataError(
            "cross_check должен содержать with_table, on_left, on_right "
            "и хотя бы одну метрику в metrics")

    left_db = db_for_table(source_key)
    right_db = db_for_table(with_table)

    # ---- читаем левую выборку ----
    left_cols = [on_left] + list(metrics.keys())
    left_filters = []
    if isinstance(filter_left, list):
        left_filters = [f for f in filter_left if f.get("field")]
    elif filter_left and filter_left.get("field"):
        left_filters = [filter_left]
    left_rows = []
    try:
        stream = dataops.iter_all(left_db, source_key,
                                  filters=left_filters, match="AND",
                                  select=left_cols)
        next(stream, None)
        left_rows = list(stream)
    except dataops.DataError as e:
        raise dataops.DataError(f"Левая таблица «{source_key}»: {e}") from e

    # ---- читаем правую выборку (с фильтром) ----
    right_cols = [on_right] + list(metrics.values())
    right_filters = []
    if isinstance(filter_right, list):
        right_filters = [f for f in filter_right if f.get("field")]
    elif filter_right and filter_right.get("field"):
        right_filters = [filter_right]
    right_rows = []
    try:
        stream = dataops.iter_all(right_db, with_table,
                                  filters=right_filters, match="AND",
                                  select=right_cols)
        next(stream, None)
        right_rows = list(stream)
    except dataops.DataError as e:
        raise dataops.DataError(f"Правая таблица «{with_table}»: {e}") from e

    # ---- группируем по нормализованному ключу ----
    def _key(row, col):
        raw = row.get(col)
        if raw is None or raw == "":
            return None
        if normalize_key:
            return _normalize_key(raw, aliases)
        return str(raw).strip().lower()

    left_groups: dict[str, list] = {}
    right_groups: dict[str, list] = {}
    left_display: dict[str, str] = {}
    right_display: dict[str, str] = {}

    for r in left_rows:
        k = _key(r, on_left)
        if k is None:
            continue
        left_groups.setdefault(k, []).append(r)
        left_display.setdefault(k, str(r.get(on_left) or k))
    for r in right_rows:
        k = _key(r, on_right)
        if k is None:
            continue
        right_groups.setdefault(k, []).append(r)
        right_display.setdefault(k, str(r.get(on_right) or k))

    # ---- автосинонимы: Скай Гарден ↔ Sky Garden, Эко Бунино ↔ ЭкоБунино ----
    # Только однозначные пары. Ручные aliases (spec + hc_aliases.json) главнее.
    auto_added = 0
    if normalize_key:
        only_l = [k for k in left_groups if k not in right_groups]
        only_r = [k for k in right_groups if k not in left_groups]
        if only_l and only_r:
            found = name_normalizer.auto_aliases(
                only_l, only_r, existing=aliases,
                left_counts={k: len(left_groups[k]) for k in only_l},
                right_counts={k: len(right_groups[k]) for k in only_r})["aliases"]
            for lk, rk in found.items():
                if rk in right_groups and lk in left_groups:
                    left_groups.setdefault(rk, []).extend(left_groups.pop(lk))
                    left_display.setdefault(rk, left_display.pop(lk, lk))
                    auto_added += 1

    all_keys = sorted(set(left_groups) | set(right_groups))

    items_by_class: dict[str, list] = {
        "critical": [], "warn": [], "ok": [],
        "left_only": [], "right_only": [], "insufficient": [],
    }

    matched_keys = 0
    for k in all_keys:
        lg = left_groups.get(k, [])
        rg = right_groups.get(k, [])
        display = left_display.get(k) or right_display.get(k) or k

        if not lg and rg:
            items_by_class["right_only"].append({
                "key": k, "display": display,
                "left_rows": 0, "right_rows": len(rg),
            })
            continue
        if lg and not rg:
            items_by_class["left_only"].append({
                "key": k, "display": display,
                "left_rows": len(lg), "right_rows": 0,
            })
            continue

        matched_keys += 1
        if min(len(lg), len(rg)) < min_group_size:
            items_by_class["insufficient"].append({
                "key": k, "display": display,
                "left_rows": len(lg), "right_rows": len(rg),
                "metrics": {},
            })
            continue

        per_metric = {}
        worst = 0.0
        for lf, rf in metrics.items():
            lv, ln = _aggregate([r.get(lf) for r in lg], agg)
            rv, rn = _aggregate([r.get(rf) for r in rg], agg)
            diff_abs = None
            diff_pct = None
            if lv is not None and rv is not None and rv != 0:
                diff_abs = round(lv - rv, 2)
                diff_pct = round((lv - rv) / abs(rv) * 100, 1)
                worst = max(worst, abs(diff_pct))
            per_metric[lf] = {
                "left": lv, "left_n": ln,
                "right": rv, "right_n": rn,
                "diff_abs": diff_abs, "diff_pct": diff_pct,
            }

        item = {
            "key": k, "display": display,
            "left_rows": len(lg), "right_rows": len(rg),
            "metrics": per_metric,
            "worst_pct": worst,
        }
        if worst >= t_warn:
            items_by_class["critical"].append(item)
        elif worst >= t_ok:
            items_by_class["warn"].append(item)
        else:
            items_by_class["ok"].append(item)

    for cls in ("critical", "warn"):
        items_by_class[cls].sort(key=lambda x: -x.get("worst_pct", 0))

    summary = {
        "total_keys": len(all_keys),
        "matched": matched_keys,
        "critical": len(items_by_class["critical"]),
        "warn": len(items_by_class["warn"]),
        "ok": len(items_by_class["ok"]),
        "insufficient": len(items_by_class["insufficient"]),
        "left_only": len(items_by_class["left_only"]),
        "right_only": len(items_by_class["right_only"]),
    }

    return {
        "source": source_key,
        "with_table": with_table,
        "on_left": on_left,
        "on_right": on_right,
        "metrics": metrics,
        "agg": agg,
        "normalize_key": normalize_key,
        "min_group_size": min_group_size,
        "thresholds": {"ok": t_ok, "warn": t_warn},
        "filter_right": filter_right,
        "left_label": spec.title or source_key,
        "right_label": ((specs.get(with_table).title
                         if specs.get(with_table) and specs.get(with_table).title
                         else with_table)),
        "left_external": bool(spec.external),
        "right_external": bool(specs.get(with_table).external)
                          if specs.get(with_table) else False,
        "aliases_total": len(aliases),
        "auto_aliases": auto_added,
        "summary": summary,
        "items_by_class": {k: v[:500] for k, v in items_by_class.items()},
    }


# ──────────────────────────────────────────────────────────────
#  МАРШРУТИЗАЦИЯ
# ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = "RedCatStudio"

    def log_message(self, fmt, *args):
        pass

    def _send(self, payload, status=200, content_type="application/json",
              headers=None):
        if isinstance(payload, bytes):
            body = payload
        elif content_type.startswith("application/json"):
            body = json.dumps(_json_safe(payload), ensure_ascii=False,
                              default=str).encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, message, status=400):
        self._send({"error": str(message)}, status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---------- GET ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self._api_get(path[5:], params)
            return self._static(path)
        except dataops.DataError as e:
            return self._error(e)
        except Exception as e:  # noqa: BLE001
            return self._error(f"{type(e).__name__}: {e}", 500)

    def _static(self, path):
        if path in ("/", "/index.html"):
            path = "/index.html"
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            return self._send("404", 404, "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(target.read_bytes(), 200, ctype)

    def _api_get(self, route, p):
        one = lambda k, d=None: p.get(k, [d])[0]  # noqa: E731
        q = parse_query(p)

        if route == "meta":
            return self._send(self._meta())

        if route == "columns":
            table = one("table")
            cols = dataops.columns(db_for_table(table), table)
            cols = field_labels.apply_to_columns(cols)
            computed = store.list_computed(STUDIO_DB, table)
            return self._send({
                "columns": cols, "id_field": id_field_for(table, cols),
                "name_field": name_field_for(table, cols), "computed": computed})

        if route == "query":
            return self._send(self._query(one("table"), q))

        if route == "facets":
            table = one("table")
            return self._send({"values": dataops.facets(
                db_for_table(table), table, one("field"), q.get("filters"),
                q.get("match", "AND"), int(one("limit", 40)))})

        if route == "stats":
            table = one("table")
            return self._send(dataops.column_stats(
                db_for_table(table), table, one("field"),
                q.get("filters"), q.get("match", "AND")))

        if route == "profile":
            table = one("table")
            db = db_for_table(table)
            cols = [c["name"] for c in dataops.columns(db, table)]
            stats = [dataops.column_stats(db, table, c, q.get("filters"),
                                          q.get("match", "AND")) for c in cols]
            for s in stats:
                s["label"] = field_labels.label_for(s["field"])
            return self._send({"columns": stats})

        if route == "pivot":
            table = one("table")
            dims = [d for d in (one("dimensions", "") or "").split(",") if d]
            return self._send(dataops.pivot(
                db_for_table(table), table, dims, one("metric") or None,
                one("agg", "count"), q.get("filters"), q.get("match", "AND"),
                int(one("limit", 100))))

        if route == "crosstab":
            table = one("table")
            return self._send(dataops.crosstab(
                db_for_table(table), table, one("row_field"), one("col_field"),
                one("metric") or None, one("agg", "count"),
                q.get("filters"), q.get("match", "AND")))

        if route == "histogram":
            table = one("table")
            return self._send(dataops.histogram(
                db_for_table(table), table, one("field"), int(one("bins", 20)),
                q.get("filters"), q.get("match", "AND")))

        if route == "duplicates":
            table = one("table")
            fields = [f for f in (one("fields", "") or "").split(",") if f]
            return self._send(dataops.duplicates(
                db_for_table(table), table, fields,
                q.get("filters"), q.get("match", "AND")))

        if route == "outliers":
            table = one("table")
            return self._send(dataops.outliers(
                db_for_table(table), table, one("field"), q.get("filters"),
                q.get("match", "AND"), float(one("threshold", 3.5)),
                group_by=one("group_by") or None))

        if route == "topbottom":
            table = one("table")
            return self._send(dataops.top_bottom(
                db_for_table(table), table, one("field"), int(one("n", 10)),
                q.get("filters"), q.get("match", "AND"),
                one("label_field") or None))

        if route == "group_summary":
            table = one("table")
            return self._send(dataops.group_summary(
                db_for_table(table), table, one("dimension"), one("metric"),
                q.get("filters"), q.get("match", "AND")))

        if route == "correlation":
            table = one("table")
            return self._send(dataops.correlation(
                db_for_table(table), table, one("x"), one("y"),
                q.get("filters"), q.get("match", "AND")))

        if route == "timeseries":
            table = one("table")
            return self._send(dataops.timeseries(
                db_for_table(table), table, one("date_field"),
                one("granularity", "month"), one("metric") or None,
                one("agg", "count"), q.get("filters"), q.get("match", "AND")))

        if route == "pareto":
            table = one("table")
            return self._send(dataops.pareto(
                db_for_table(table), table, one("dimension"),
                one("metric") or None, one("agg", "count"),
                q.get("filters"), q.get("match", "AND")))

        if route == "sql":
            return self._send(dataops.run_sql(DATA_DB, one("sql", "")))

        if route == "search":
            return self._send({
                "results": store.search(STUDIO_DB, one("q", ""),
                                        one("source") or None,
                                        int(one("limit", 100))),
                "meta": store.search_meta(STUDIO_DB)})

        if route == "record":
            return self._send(self._record(one("table"), one("id")))

        if route == "record_series":
            return self._send({"series": record_series(
                one("source"), one("id"), one("field"))})

        if route == "crosschecks":
            import crosschecks
            history = runs_history(limit=1)
            return self._send(crosschecks.run(
                DATA_DB, load_specs(), history[-1] if history else None))

        if route == "cross_check":
            source = one("source")
            if not source:
                return self._error("Укажите ?source=<ключ>")
            try:
                return self._send(_cross_check_report(source))
            except dataops.DataError as e:
                return self._error(e)

        if route == "related":
            return self._send(self._related(one("table"), one("id")))

        if route == "runs":
            return self._send({"runs": runs_history()})

        if route == "anomalies":
            return self._send({"items": anomalies_list(
                one("severity") or None, one("source") or None,
                one("kind") or None)})

        if route == "edits":
            return self._send({"items": store.list_edits(
                STUDIO_DB, one("source") or None)})

        if route == "edit_history":
            return self._send({"items": store.edit_history(
                STUDIO_DB, one("source"), one("id"))})

        if route == "views":
            return self._send({"items": store.list_views(
                STUDIO_DB, one("source") or None)})

        if route == "tags":
            return self._send({"items": store.all_tags(
                STUDIO_DB, one("source") or None)})

        if route == "apilog":
            return self._send({
                "items": store.list_api_log(STUDIO_DB, int(one("limit", 300)),
                                            one("blocked") == "1"),
                "summary": store.api_log_summary(STUDIO_DB),
                "guard": api_guard.status_text(),
                "allowed": sorted(api_guard.ALLOWED_METHODS)})

        if route == "sources":
            items = source_files()
            hint = ""
            if not items:
                parts = []
                if not SOURCES_DIR.exists():
                    parts.append(f"sources/ не найдена ({SOURCES_DIR})")
                else:
                    n1 = len(list(SOURCES_DIR.glob("*.json")))
                    if n1:
                        parts.append(f"в sources/ есть {n1} *.json")
                if not SOURCES_EXT_DIR.exists():
                    parts.append(f"sources_external/ не найдена "
                                 f"({SOURCES_EXT_DIR})")
                else:
                    n2 = len(list(SOURCES_EXT_DIR.glob("*.json")))
                    if n2:
                        parts.append(f"в sources_external/ есть {n2} *.json")
                hint = "; ".join(parts) or "ни одного *.json ни в одной папке"
            return self._send({"items": items,
                               "dir": str(SOURCES_DIR),
                               "dir_ext": str(SOURCES_EXT_DIR),
                               "hint": hint})

        if route == "source":
            name = one("file", "")
            for folder in (SOURCES_DIR, SOURCES_EXT_DIR):
                path = folder / name
                if path.is_file() and path.suffix == ".json":
                    return self._send({"file": path.name,
                                       "content": path.read_text(encoding="utf-8"),
                                       "external": folder == SOURCES_EXT_DIR})
            return self._error("Файл источника не найден.", 404)

        if route == "run":
            return self._send(RUNNER.state(int(one("since", 0))))

        if route == "files":
            return self._send({"items": self._files()})

        if route == "export":
            return self._export(one, q)

        if route == "completeness_fields":
            specs = load_specs()
            table = one("table")
            spec = specs.get(table)
            db = db_for_table(table)
            fields = completeness.completeness_by_field(
                db, table, q.get("filters"), q.get("match", "AND"))
            for f in fields:
                f["kind"] = completeness._kind(f["field"], spec)
                f["label"] = field_labels.label_for(f["field"])
            return self._send({"fields": fields,
                               "overall": completeness.overall_score(fields, spec)})

        if route == "completeness_records":
            table = one("table")
            spec = load_specs().get(table)
            db = db_for_table(table)
            threshold = float(one("threshold", 80))
            rows = completeness.completeness_by_record(
                db, table, spec, q.get("filters"), q.get("match", "AND"))
            for r in rows:
                r["missing_labels"] = [field_labels.label_for(f)
                                       for f in r["missing"]]
            below = [r for r in rows if r["pct"] < threshold]
            incomplete = [r for r in rows if r["pct"] < 100]
            incomplete.sort(key=lambda r: r["pct"])
            return self._send({
                "records": incomplete[:2000],
                "total": len(rows),
                "below": len(below),
                "incomplete": len(incomplete),
                "threshold": threshold,
            })

        if route == "completeness_groups":
            table = one("table")
            spec = load_specs().get(table)
            db = db_for_table(table)
            return self._send({"groups": completeness.completeness_by_group(
                db, table, spec, one("group_field"),
                q.get("filters"), q.get("match", "AND"))})

        return self._error(f"Неизвестный маршрут: /api/{route}", 404)

    # ---------- POST (LOCAL-ONLY: пишет только на ваш диск) ----------
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return self._error("Только /api/*", 404)
        route = parsed.path[5:]
        body = self._body()
        try:
            return self._api_post(route, body)
        except dataops.DataError as e:
            return self._error(e)
        except Exception as e:  # noqa: BLE001
            return self._error(f"{type(e).__name__}: {e}", 500)

    def _api_post(self, route, b):
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
            names = [c["name"] for c in dataops.columns(DATA_DB, b["table"])]
            used = dataops.validate_formula(b["expr"], names)
            store.save_computed(STUDIO_DB, b["table"], b["name"], b["expr"])
            return self._send({"ok": True, "uses": used})

        if route == "computed_delete":
            store.delete_computed(STUDIO_DB, b["table"], b["name"])
            return self._send({"ok": True})

        if route == "search_rebuild":
            specs = load_specs()
            count = store.rebuild_search_index(
                STUDIO_DB, dataops.iter_search_documents(DATA_DB, specs))
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
            name = b["file"]
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
            name = b["file"]
            for folder in (SOURCES_DIR, SOURCES_EXT_DIR):
                path = folder / name
                if path.exists():
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

    # ---------- реализации ----------
    def _meta(self):
        redcat_ok = DATA_DB.exists()
        ext_ok = EXTERNAL_DB.exists()
        redcat_tables = dataops.list_tables(DATA_DB) if redcat_ok else []
        external_tables = ([t for t in dataops.list_tables(EXTERNAL_DB)]
                           if ext_ok else [])
        external_tables = [t for t in external_tables
                           if t["name"] != "comparison_vs_previous"]
        specs = load_specs()
        ext_names = {t["name"] for t in external_tables}
        for t in redcat_tables + external_tables:
            spec = specs.get(t["name"])
            t["title"] = spec.title if spec else ""
            t["external"] = t["name"] in ext_names
        all_tables = redcat_tables + external_tables
        return {
            "tables": all_tables,
            "redcat_tables": redcat_tables,
            "external_tables": external_tables,
            "data_db": str(DATA_DB), "data_ok": redcat_ok,
            "external_db": str(EXTERNAL_DB), "external_ok": ext_ok,
            "studio": store.stats(STUDIO_DB),
            "edit_counts": store.edit_counts(STUDIO_DB),
            "token": token_status(),
            "guard": {"text": api_guard.status_text(),
                      "allowed": sorted(api_guard.ALLOWED_METHODS),
                      "blocked": api_guard.blocked_count()},
            "search": store.search_meta(STUDIO_DB),
            "sources": [{"key": s.key, "title": s.title,
                         "split": bool(s.split_param),
                         "external": bool(s.external),
                         "browser": getattr(s, "fetch_mode", "http") == "browser",
                         "has_cross_check": bool(getattr(s, "cross_check", None))}
                        for s in specs.values()],
            "aliases_total": hc_aliases.count(),
            "runs": len(runs_history(limit=500)),
            "server_time": datetime.now().isoformat(timespec="seconds"),
        }

    def _query(self, table, q):
        db = db_for_table(table)
        cols = dataops.columns(db, table)
        id_field = id_field_for(table, cols)
        exclude = None
        if not q.get("include_hidden"):
            exclude = store.flagged_ids(STUDIO_DB, table, store.FLAG_HIDDEN)
        only_ids = None
        if q.get("only") == "starred":
            only_ids = store.flagged_ids(STUDIO_DB, table, store.FLAG_STAR)
        elif q.get("only") == "edited":
            only_ids = store.edited_ids(STUDIO_DB, table)

        result = dataops.query(
            db, table, q.get("filters"), q.get("match", "AND"),
            q.get("sort"), bool(q.get("desc")), int(q.get("page", 1)),
            int(q.get("page_size", 100)), q.get("select"),
            exclude_ids=exclude, id_field=id_field, only_ids=only_ids)
        result["rows"] = decorate(result["rows"], table, id_field)
        result["id_field"] = id_field
        result["name_field"] = name_field_for(table, cols)

        for c in store.list_computed(STUDIO_DB, table):
            for row in result["rows"]:
                try:
                    row[c["name"]] = dataops.eval_formula(c["expr"], row)
                except dataops.DataError:
                    row[c["name"]] = None
            if c["name"] not in result["columns"]:
                result["columns"].append(c["name"])
        return result

    def _record(self, table, record_id):
        db = db_for_table(table)
        cols = dataops.columns(db, table)
        id_field = id_field_for(table, cols)
        res = dataops.query(db, table,
                            [{"field": id_field, "op": "eq", "value": record_id}],
                            page_size=1)
        if not res["rows"]:
            raise dataops.DataError(f"Запись {record_id} не найдена в «{table}».")
        row = decorate(res["rows"], table, id_field)[0]
        spec = load_specs().get(table)
        return {
            "table": table, "id_field": id_field, "row": row,
            "edits": store.edit_history(STUDIO_DB, table, record_id),
            "numeric_fields": (spec.numeric_fields if spec else
                               [c["name"] for c in cols
                                if any(t in c["type"]
                                       for t in ("INT", "REAL"))][:8]),
            "columns": cols,
        }

    def _upload_csv_batch(self, b) -> dict:
        """Принимает один кусок («пачку») строк из CSV, который клиент шлёт
        отдельным POST-запросом. Файл целиком через веб-сервер на голом
        stdlib не пропустить с комфортом (да и не нужно) — вместо этого
        клиент режет данные на пачки по ~1000 строк и шлёт их по очереди,
        сервер копит их во временной таблице и на последней пачке
        достраивает настоящую таблицу с уже подобранными типами колонок.

        Таблица всегда пишется в external_data.db — «свои» данные логически
        такие же внешние, как fsk_apartments и подобные, и сразу попадают
        в приложение (вкладки «Данные», «Внешние», аналитика) без правки
        кода: список таблиц строится по факту наличия в базе, а не по
        сборщику.
        """
        table_raw = str(b.get("table") or "").strip()
        if not table_raw:
            raise dataops.DataError("Не указано имя таблицы.")
        table = dataops._safe_ident(table_raw)
        if table == "comparison_vs_previous" or table in load_specs():
            raise dataops.DataError(
                f"Имя «{table}» занято источником сбора — выберите другое.")

        seq = int(b.get("seq", 0))
        total = int(b.get("total", 1))
        mode = b.get("mode") if b.get("mode") in ("append", "replace") else "append"
        rows_in = b.get("rows") or []
        tmp_table = f"_csv_upload_tmp_{table}"

        with sqlite3.connect(str(EXTERNAL_DB), timeout=15) as conn:
            conn.execute("PRAGMA journal_mode=WAL")

            if seq == 0:
                if DATA_DB.exists() and table in {
                        t["name"] for t in dataops.list_tables(DATA_DB)}:
                    raise dataops.DataError(
                        f"Таблица «{table}» уже существует как источник сбора "
                        f"Redcat — выберите другое имя.")
                columns = b.get("columns") or []
                if not columns:
                    raise dataops.DataError(
                        "Не переданы названия колонок (columns) — они нужны "
                        "в самой первой пачке (seq=0).")
                columns = _csv_dedupe_idents(columns)
                conn.execute(f'DROP TABLE IF EXISTS "{tmp_table}"')
                cols_sql = ", ".join(f'"{c}" TEXT' for c in columns)
                conn.execute(f'CREATE TABLE "{tmp_table}" ({cols_sql})')
                _CSV_UPLOAD_STATE[table] = {"columns": columns, "inserted": 0}

            state = _CSV_UPLOAD_STATE.get(table)
            if not state:
                raise dataops.DataError(
                    "Загрузка не была начата (или прервалась) — обновите "
                    "страницу и загрузите файл заново с первой пачки.")
            columns = state["columns"]

            if rows_in:
                placeholders = ", ".join("?" for _ in columns)
                col_sql = ", ".join(f'"{c}"' for c in columns)
                sql = f'INSERT INTO "{tmp_table}" ({col_sql}) VALUES ({placeholders})'
                for chunk in dataops._chunks(rows_in, 500):
                    conn.executemany(
                        sql, [_csv_pad_row(r, len(columns)) for r in chunk])
                state["inserted"] += len(rows_in)
            conn.commit()

            finished = seq >= total - 1
            if finished:
                self._finalize_csv_upload(conn, table, tmp_table, columns, mode)
                _CSV_UPLOAD_STATE.pop(table, None)

            inserted_so_far = state["inserted"]

        return {"ok": True, "table": table, "seq": seq, "total": total,
                "inserted_so_far": inserted_so_far, "finished": finished}

    def _finalize_csv_upload(self, conn, table, tmp_table, columns, mode) -> None:
        """Последний шаг загрузки: подбирает тип каждой колонки по всем
        загруженным значениям, создаёт (или дополняет, в режиме «добавить»)
        настоящую таблицу и переносит данные с приведением типов, а не
        текстом как есть — иначе числовые колонки не сортировались бы как
        числа и не участвовали в аналитике (выбросы, распределения)."""
        cur = conn.cursor()
        col_sql_tmp = ", ".join(f'"{c}"' for c in columns)
        cur.execute(f'SELECT {col_sql_tmp} FROM "{tmp_table}"')
        all_rows = cur.fetchall()

        types = []
        for i in range(len(columns)):
            sample = [r[i] for r in all_rows[:500] if r[i] not in (None, "")]
            types.append(_csv_sniff_type(sample))

        final_exists = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone() is not None

        if mode == "replace" or not final_exists:
            cur.execute(f'DROP TABLE IF EXISTS "{table}"')
            cols_sql = ", ".join(f'"{c}" {t}' for c, t in zip(columns, types))
            cur.execute(f'CREATE TABLE "{table}" ({cols_sql})')
        else:
            existing = {r[1] for r in cur.execute(f'PRAGMA table_info("{table}")')}
            for c, t in zip(columns, types):
                if c not in existing:
                    cur.execute(f'ALTER TABLE "{table}" ADD COLUMN "{c}" {t}')

        converted = [tuple(_csv_convert(v, t) for v, t in zip(row, types))
                     for row in all_rows]
        if converted:
            placeholders = ", ".join("?" for _ in columns)
            col_sql = ", ".join(f'"{c}"' for c in columns)
            sql = f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})'
            for chunk in dataops._chunks(converted, 500):
                cur.executemany(sql, chunk)
        cur.execute(f'DROP TABLE "{tmp_table}"')
        conn.commit()

    def _bulk_edit(self, b):
        table, field = b["table"], b["field"]
        db = db_for_table(table)
        cols = dataops.columns(db, table)
        id_field = id_field_for(table, cols)
        mode = b.get("mode", "set")
        limit = int(b.get("limit", 5000))

        stream = dataops.iter_all(db, table, b.get("filters"),
                                  b.get("match", "AND"),
                                  select=[id_field, field], limit=limit)
        next(stream)
        rows = list(stream)

        pairs = []
        for row in rows:
            rid, old = row.get(id_field), row.get(field)
            if mode == "set":
                new = b.get("value")
            elif mode == "replace":
                if old is None:
                    continue
                new = str(old).replace(b.get("find", ""), b.get("value", ""))
            elif mode == "regex":
                if old is None:
                    continue
                try:
                    new = re.sub(b.get("find", ""), b.get("value", ""), str(old))
                except re.error as e:
                    raise dataops.DataError(
                        f"Ошибка в регулярном выражении: {e}") from e
            elif mode == "trim":
                new = str(old).strip() if old is not None else old
            elif mode == "formula":
                new = dataops.eval_formula(b.get("value", ""), row)
            else:
                raise dataops.DataError(f"Неизвестный режим правки: {mode}")
            if str(new) != str(old):
                pairs.append((rid, old, new))

        return store.set_edits_bulk(STUDIO_DB, table, field, pairs,
                                    b.get("author", ""),
                                    b.get("note", "массовая правка"))

    def _probe(self, b):
        import requests
        url = (b.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise dataops.DataError("Укажите полный адрес, начиная с https://")
        params = src.env_params()
        for key, value in params.items():
            url = url.replace("{" + key + "}", str(value))
        token = (b.get("token") or read_env_token()).strip()
        started = time.perf_counter()

        if b.get("fetch_mode") == "browser":
            try:
                import browser_fetch
            except ImportError:
                return {"ok": False,
                        "error": "browser_fetch.py не найден рядом с webapp.py."}
            try:
                with browser_fetch.BrowserFetcher("probe") as fetcher:
                    html = fetcher.fetch(
                        url,
                        wait_for=b.get("browser_wait_for") or "",
                        wait_ms=int(b.get("browser_wait_ms") or 0))
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            ms = round((time.perf_counter() - started) * 1000)
            preview = html[:4000]
            suggestion = src.suggest_spec(
                url, None, b.get("key", ""), b.get("title", ""),
                b.get("pagination_style", "jsonapi"))
            suggestion["fetch_mode"] = "browser"
            if b.get("browser_wait_for"):
                suggestion["browser_wait_for"] = b["browser_wait_for"]
            if b.get("browser_wait_ms"):
                suggestion["browser_wait_ms"] = int(b["browser_wait_ms"])
            validation = spec_validator.validate_on_sample(
                suggestion,
                html.encode("utf-8") if isinstance(html, str) else html)
            return {"ok": True, "status": 200, "ms": ms,
                    "preview": preview, "suggestion": suggestion,
                    "validation": validation}

        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = requests.get(url, headers=headers, timeout=25)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        ms = round((time.perf_counter() - started) * 1000)
        try:
            payload = resp.json()
        except ValueError:
            return {"ok": False, "status": resp.status_code, "ms": ms,
                    "error": "Ответ не является JSON.",
                    "preview": resp.text[:1500]}
        suggestion = src.suggest_spec(
            b.get("url", url), payload, b.get("key", ""), b.get("title", ""),
            b.get("pagination_style", "jsonapi"))
        validation = spec_validator.validate_on_sample(suggestion, resp.content)
        return {"ok": resp.ok, "status": resp.status_code, "ms": ms,
                "preview": json.dumps(payload, ensure_ascii=False)[:4000],
                "suggestion": suggestion,
                "validation": validation}

    def _probe_spec(self, b):
        """Прогоняет готовый spec на одном ответе и возвращает отчёт.

        Полезно, когда черновик уже поправлен руками: пользователь нажимает
        «Проверить spec» и сразу видит, сколько записей извлёк парсер, какие
        поля не сошлись и какие стратегии не зарегистрированы.

        Ничего не сохраняет. Регистрация источника — отдельный маршрут
        source_save / source_save_raw.
        """
        spec_dict = b.get("spec") or {}
        url = (b.get("url") or spec_dict.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise dataops.DataError("Укажите URL для проверки.")

        params = src.env_params()
        for key, value in params.items():
            url = url.replace("{" + key + "}", str(value))
        token = (b.get("token") or read_env_token()).strip()
        fetch_mode = spec_dict.get("fetch_mode") or "http"

        if fetch_mode == "browser":
            try:
                import browser_fetch
            except ImportError:
                return {"ok": False,
                        "error": "browser_fetch.py не найден рядом с webapp.py."}
            spec_obj, _ = spec_validator.to_spec(spec_dict)
            strategy = (spec_obj.fetch_strategy if spec_obj else "") or "browser"
            try:
                with browser_fetch.BrowserFetcher("probe_spec") as fetcher:
                    if strategy == "browser_xhr":
                        captured = fetcher.capture_json_responses(
                            url,
                            (spec_obj.browser_xhr_pattern if spec_obj else "") or "",
                            wait_for=(spec_obj.browser_wait_for if spec_obj else "") or "",
                            wait_ms=int((spec_obj.browser_wait_ms if spec_obj else 0) or 0),
                        )
                        if not captured:
                            return {"ok": False,
                                    "error": "ни одного JSON-XHR не поймано"}
                        raw = json.dumps(captured[0], ensure_ascii=False).encode("utf-8")
                    else:
                        html = fetcher.fetch(
                            url,
                            wait_for=(spec_obj.browser_wait_for if spec_obj else "") or "",
                            wait_ms=int((spec_obj.browser_wait_ms if spec_obj else 0) or 0),
                        )
                        raw = (html.encode("utf-8")
                               if isinstance(html, str) else html)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        else:
            import requests
            headers = {"Accept": "application/json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                resp = requests.get(url, headers=headers, timeout=25)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if resp.status_code >= 400:
                return {"ok": False, "status": resp.status_code,
                        "error": resp.text[:400]}
            raw = resp.content

        validation = spec_validator.validate_on_sample(spec_dict, raw)
        return {"ok": bool(validation.get("parse_ok")),
                "validation": validation}

    def _related(self, table, record_id):
        import crosschecks
        specs = load_specs()
        relations = crosschecks.detect_relations(DATA_DB, specs)
        out = []
        db = db_for_table(table)

        for rel in relations:
            if rel["child"] == table:
                cols = dataops.columns(db, table)
                res = dataops.query(
                    db, table,
                    [{"field": id_field_for(table, cols), "op": "eq",
                      "value": record_id}], page_size=1)
                if not res["rows"]:
                    continue
                value = res["rows"][0].get(rel["child_field"])
                if value is None:
                    continue
                parent_db = db_for_table(rel["parent"])
                parent = dataops.query(
                    parent_db, rel["parent"],
                    [{"field": rel["parent_field"], "op": "eq",
                      "value": value}], page_size=5)
                if parent["rows"]:
                    out.append({
                        "direction": "parent", "table": rel["parent"],
                        "title": f"Справочник «{rel['parent']}»",
                        "count": parent["total"], "rows": parent["rows"][:5],
                        "id_field": rel["parent_field"], "link_value": value})

            if rel["parent"] == table:
                child_db = db_for_table(rel["child"])
                child = dataops.query(
                    child_db, rel["child"],
                    [{"field": rel["child_field"], "op": "eq",
                      "value": record_id}], page_size=5)
                out.append({
                    "direction": "child", "table": rel["child"],
                    "title": f"Связанные записи в «{rel['child']}»",
                    "count": child["total"], "rows": child["rows"][:5],
                    "id_field": rel["child_field"], "link_value": record_id,
                    "filter_field": rel["child_field"]})
        return {"table": table, "id": record_id, "groups": out}

    def _files(self):
        items = []
        for folder in (OUTPUT_DIR, HISTORY_DIR):
            if not folder.exists():
                continue
            for path in sorted(folder.glob("*"),
                               key=lambda x: x.stat().st_mtime if x.is_file() else 0,
                               reverse=True):
                if path.is_file() and path.name != ".gitkeep":
                    items.append({
                        "name": path.name, "folder": folder.name,
                        "size": path.stat().st_size,
                        "modified": datetime.fromtimestamp(
                            path.stat().st_mtime).isoformat(timespec="seconds")})
        return items[:80]

    def _export(self, one, q):
        table = one("table")
        fmt = one("format", "csv")
        what = one("what", "table")
        stamp = datetime.now().strftime("%Y%m%d_%H%M")

        if what == "edits":
            rows = store.list_edits(STUDIO_DB, one("source") or None, limit=100000)
            cols = ["source", "record_id", "field", "old_value", "new_value",
                    "author", "note", "ts"]
            name = f"edits_{stamp}"

        elif what == "sql":
            result = dataops.run_sql(DATA_DB, one("sql", ""))
            rows, cols = result["rows"], result["columns"]
            name = f"sql_{stamp}"

        elif what == "completeness_fields":
            spec = load_specs().get(table)
            db = db_for_table(table)
            fields = completeness.completeness_by_field(
                db, table, q.get("filters"), q.get("match", "AND"))
            for f in fields:
                f["kind"] = completeness._kind(f["field"], spec)
            rows = [{
                "поле": field_labels.label_for(f["field"]),
                "тип": f.get("kind", "other"),
                "всего": f.get("total"),
                "пусто": f.get("empty"),
                "заполнено_%": f.get("fill_rate"),
                "уникальных": f.get("unique"),
                "минимум": f.get("min"),
                "медиана": f.get("median"),
                "максимум": f.get("max"),
            } for f in fields]
            cols = ["поле", "тип", "всего", "пусто", "заполнено_%",
                    "уникальных", "минимум", "медиана", "максимум"]
            name = f"completeness_fields_{table}_{stamp}"

        elif what == "completeness_records":
            spec = load_specs().get(table)
            db = db_for_table(table)
            all_records = completeness.completeness_by_record(
                db, table, spec, q.get("filters"), q.get("match", "AND"))
            incomplete = [r for r in all_records if r["pct"] < 100]
            incomplete.sort(key=lambda r: r["pct"])
            rows = [{
                "запись": r["name"] or r["id"],
                "id": r["id"],
                "%": r["pct"],
                "не хватает полей": ", ".join(
                    field_labels.label_for(f) for f in r["missing"]),
            } for r in incomplete]
            cols = ["запись", "id", "%", "не хватает полей"]
            name = f"completeness_records_{table}_{stamp}"

        elif what == "cross_check":
            source = one("source", "")
            try:
                report = _cross_check_report(source)
            except dataops.DataError as e:
                return self._error(e)
            rows = []
            cls_titles = {"critical": "смотреть руками", "warn": "внимание",
                          "ok": "сходится", "insufficient": "мало данных",
                          "left_only": "только слева",
                          "right_only": "только справа"}
            for cls, items in report["items_by_class"].items():
                for it in items:
                    row = {
                        "класс": cls_titles.get(cls, cls),
                        "название": it.get("display"),
                        "строк слева": it.get("left_rows"),
                        "строк справа": it.get("right_rows"),
                    }
                    for lf, m in (it.get("metrics") or {}).items():
                        row[f"{lf} слева"] = m.get("left")
                        row[f"{lf} справа"] = m.get("right")
                        row[f"{lf} Δ%"] = m.get("diff_pct")
                    rows.append(row)
            cols = list(rows[0].keys()) if rows else ["класс", "название"]
            name = f"cross_check_{source}_{stamp}"

        else:
            db = db_for_table(table)
            stream = dataops.iter_all(db, table, q.get("filters"),
                                      q.get("match", "AND"), q.get("select"),
                                      q.get("sort"), bool(q.get("desc")))
            cols = next(stream)
            rows = list(stream)
            cols_meta = dataops.columns(db, table)
            rows = decorate(rows, table, id_field_for(table, cols_meta))
            name = f"{table}_{stamp}"

        if fmt == "json":
            body = dataops.to_json(rows).encode("utf-8")
            ctype, ext = "application/json; charset=utf-8", "json"
        else:
            body = ("\ufeff" + dataops.to_csv(rows, cols)).encode("utf-8")
            ctype, ext = "text/csv; charset=utf-8", "csv"
        self._send(body, 200, ctype, {
            "Content-Disposition": f'attachment; filename="{name}.{ext}"'})


# ──────────────────────────────────────────────────────────────
def find_free_port(preferred=8765) -> int:
    for port in range(preferred, preferred + 25):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return 0


def serve(port=8765, open_browser=True):
    store.init(STUDIO_DB)
    store.prune_api_log(STUDIO_DB)
    port = find_free_port(port)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"

    print("=" * 60)
    print("  REDCAT STUDIO")
    print("=" * 60)
    print(f"  {api_guard.status_text()}")
    print(f"  Данные:  {DATA_DB if DATA_DB.exists() else 'ещё не собраны'}")
    if EXTERNAL_DB.exists():
        print(f"  Внешние: {EXTERNAL_DB}")
    n_aliases = hc_aliases.count()
    if n_aliases:
        print(f"  Синонимов ЖК: {n_aliases} (hc_aliases.json)")
    print(f"  Правки:  {STUDIO_DB.name} (локально, в API не уходят)")
    print(f"\n  Откройте в браузере:  {url}")
    print("\n  Остановить — Ctrl+C\n")

    if open_browser:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()


if __name__ == "__main__":
    serve()