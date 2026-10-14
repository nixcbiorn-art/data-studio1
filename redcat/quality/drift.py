"""
Качество данных и дрейф схемы
=============================
Когда источников много и они меняются на стороне API, главная опасность —
тихая деградация: поле переименовали, оно стало приходить пустым, тип
поменялся со строки на число. Агрегаты при этом выглядят нормально, а
выводы уже врут.

Модуль профилирует любой набор записей (ничего не зная о его смысле) и
сравнивает профиль с прошлым запуском.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter


def _type_name(v):
    if v is None or v == "":
        return "empty"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, (list, dict)):
        return "complex"
    s = str(v)
    try:
        float(s.replace("\xa0", "").replace(" ", "").replace(",", "."))
        return "numeric_string"
    except ValueError:
        return "string"


def profile(rows, sample_limit=5000) -> dict:
    """Строит профиль набора записей: заполненность, типы, уникальность."""
    if not rows:
        return {"row_count": 0, "columns": {}}

    sample = rows if len(rows) <= sample_limit else rows[:sample_limit]
    columns = {}
    all_keys = set()
    for r in sample:
        all_keys.update(r.keys())

    for key in sorted(all_keys):
        values = [r.get(key) for r in sample]
        non_empty = [v for v in values if v not in (None, "", [], {})]
        types = Counter(_type_name(v) for v in values)
        try:
            unique = len({v if not isinstance(v, (list, dict)) else str(v) for v in values})
        except TypeError:
            unique = None

        col = {
            "fill_rate": round(100 * len(non_empty) / len(sample), 2),
            "unique": unique,
            "dominant_type": types.most_common(1)[0][0] if types else "empty",
            "types": dict(types),
        }
        nums = []
        for v in non_empty:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                nums.append(float(v))
        if nums:
            col["min"] = min(nums)
            col["max"] = max(nums)
        columns[key] = col

    return {"row_count": len(rows), "sampled": len(sample), "columns": columns}


def compare_profiles(old_profile, new_profile, source, fill_drop_pct=15.0):
    """Сравнивает профили двух запусков. Возвращает список аномалий схемы."""
    issues = []
    if not old_profile or not old_profile.get("columns"):
        return issues

    old_cols = old_profile.get("columns", {})
    new_cols = new_profile.get("columns", {})

    for col in sorted(set(new_cols) - set(old_cols)):
        issues.append({
            "source": source, "kind": "schema_new_field", "severity": "info",
            "entity": col, "metric": col, "value": None, "expected": None,
            "deviation": None,
            "message": f"В данных появилось новое поле «{col}» — API расширился.",
        })

    for col in sorted(set(old_cols) - set(new_cols)):
        issues.append({
            "source": source, "kind": "schema_lost_field", "severity": "critical",
            "entity": col, "metric": col, "value": None, "expected": None,
            "deviation": None,
            "message": f"Поле «{col}» пропало из ответа API. Если оно "
                       f"использовалось в метриках, они станут неверными.",
        })

    for col in sorted(set(old_cols) & set(new_cols)):
        old_c, new_c = old_cols[col], new_cols[col]

        drop = old_c["fill_rate"] - new_c["fill_rate"]
        if drop >= fill_drop_pct:
            issues.append({
                "source": source, "kind": "fill_rate_drop",
                "severity": "critical" if new_c["fill_rate"] < 50 else "warning",
                "entity": col, "metric": col, "value": new_c["fill_rate"],
                "expected": old_c["fill_rate"], "deviation": round(-drop, 2),
                "message": f"Заполненность поля «{col}» упала с "
                           f"{old_c['fill_rate']:.1f}% до {new_c['fill_rate']:.1f}%.",
            })

        if old_c["dominant_type"] != new_c["dominant_type"] and \
                "empty" not in (old_c["dominant_type"], new_c["dominant_type"]):
            issues.append({
                "source": source, "kind": "type_change", "severity": "warning",
                "entity": col, "metric": col, "value": new_c["dominant_type"],
                "expected": old_c["dominant_type"], "deviation": None,
                "message": f"У поля «{col}» изменился тип данных: "
                           f"{old_c['dominant_type']} → {new_c['dominant_type']}.",
            })

    old_n, new_n = old_profile.get("row_count", 0), new_profile.get("row_count", 0)
    if old_n >= 100 and new_n < old_n * 0.5:
        issues.append({
            "source": source, "kind": "volume_drop", "severity": "critical",
            "entity": "", "metric": "row_count", "value": new_n, "expected": old_n,
            "deviation": round(100 * (new_n - old_n) / old_n, 1),
            "message": f"Записей стало {new_n} вместо {old_n} — падение более чем вдвое.",
        })

    return issues


# ──────────────────────────────────────────────────────────────
#  ХРАНЕНИЕ ПРОФИЛЕЙ
# ──────────────────────────────────────────────────────────────
def init(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                source TEXT PRIMARY KEY,
                run_id INTEGER,
                payload TEXT
            )""")
        conn.commit()


def save_profile(db_path, source, run_id, prof):
    init(db_path)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO profiles (source, run_id, payload) VALUES (?,?,?)",
            (source, run_id, json.dumps(prof, ensure_ascii=False, default=str)),
        )
        conn.commit()


def load_profile(db_path, source):
    try:
        init(db_path)
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT payload FROM profiles WHERE source = ?", (source,)
            ).fetchone()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, json.JSONDecodeError) as e:
        logging.warning("Не удалось прочитать профиль %s: %s", source, e)
        return None


# ──────────────────────────────────────────────────────────────
#  ИСТОРИЯ ЗАПОЛНЕННОСТИ ПО ЗАПУСКАМ
# ──────────────────────────────────────────────────────────────
# `profiles` выше нарочно хранит только ОДИН, последний профиль на источник
# (PRIMARY KEY source, INSERT OR REPLACE) — этого достаточно для сравнения
# «этот запуск против прошлого» в compare_profiles(). Для графика динамики
# нужен весь ряд, поэтому ниже отдельные таблицы, куда каждый запуск
# ДОБАВЛЯЕТ строку, а не затирает предыдущую.
#
# Данные не пересчитываются заново: prof здесь — тот же объект, что вернул
# profile() и что уже сохраняется через save_profile(), в нём уже есть
# fill_rate каждой колонки. Поэтому запись истории не делает ни одного
# дополнительного запроса к собранным данным.
_COMPLETENESS_WEIGHTS = {"required": 1.0, "numeric": 0.7, "other": 0.3}


def _field_kind(field, spec):
    if spec:
        if field in (spec.required_fields or []):
            return "required"
        if field in (spec.numeric_fields or []):
            return "numeric"
    return "other"


def init_completeness_history(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS completeness_history (
                source TEXT, run_id INTEGER, ts TEXT,
                overall_pct REAL, fields_total INTEGER,
                PRIMARY KEY (source, run_id)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS field_completeness_history (
                source TEXT, run_id INTEGER, field TEXT,
                fill_rate REAL, kind TEXT,
                PRIMARY KEY (source, run_id, field)
            )""")
        conn.commit()


def save_completeness_history(db_path, source, run_id, ts, prof, spec=None,
                              min_fill=5.0):
    """Пишет взвешенную заполненность и разбивку по полям в историю запусков.

    min_fill исключает из взвешенного процента те же фантомные поля (0% —
    их нет в API вовсе), что и overall_score() в completeness.py — иначе
    график дублировал бы падение из-за колонок, которых никогда не было.
    """
    cols = (prof or {}).get("columns") or {}
    if not cols:
        return
    relevant = {f: c for f, c in cols.items() if c.get("fill_rate", 0) >= min_fill}
    overall_pct = None
    if relevant:
        weights = [_COMPLETENESS_WEIGHTS[_field_kind(f, spec)] for f in relevant]
        filled = [_COMPLETENESS_WEIGHTS[_field_kind(f, spec)] * c["fill_rate"] / 100
                  for f, c in relevant.items()]
        total_w = sum(weights)
        if total_w:
            overall_pct = round(100 * sum(filled) / total_w, 2)

    init_completeness_history(db_path)
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO completeness_history "
                "(source, run_id, ts, overall_pct, fields_total) VALUES (?,?,?,?,?)",
                (source, run_id, ts, overall_pct, len(cols)))
            conn.executemany(
                "INSERT OR REPLACE INTO field_completeness_history "
                "(source, run_id, field, fill_rate, kind) VALUES (?,?,?,?,?)",
                [(source, run_id, f, c.get("fill_rate"), _field_kind(f, spec))
                 for f, c in cols.items()])
            conn.commit()
    except sqlite3.Error as e:
        logging.warning("Не удалось записать историю заполненности %s: %s", source, e)


def prune_completeness_history(db_path, keep_runs=30):
    """Удерживает размер истории: хранит только последние N запусков."""
    try:
        with sqlite3.connect(str(db_path)) as conn:
            run_ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT run_id FROM completeness_history "
                "ORDER BY run_id DESC").fetchall()]
            if len(run_ids) > keep_runs:
                cutoff = run_ids[keep_runs - 1]
                conn.execute("DELETE FROM completeness_history WHERE run_id < ?", (cutoff,))
                conn.execute("DELETE FROM field_completeness_history WHERE run_id < ?", (cutoff,))
                conn.commit()
    except sqlite3.Error as e:
        logging.warning("Не удалось почистить историю заполненности: %s", e)


def load_completeness_history(db_path, source, limit=60):
    """Ряд «взвешенная заполненность по запускам» — для графика динамики."""
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                "SELECT run_id, ts, overall_pct, fields_total "
                "FROM completeness_history WHERE source = ? "
                "ORDER BY run_id DESC LIMIT ?", (source, limit)).fetchall()
        return [{"run_id": r[0], "ts": r[1], "overall_pct": r[2],
                 "fields_total": r[3]} for r in reversed(rows)]
    except sqlite3.Error:
        return []


def load_field_completeness_trend(db_path, source, runs=10, min_drop=5.0, limit=15):
    """Поля, у которых заполненность заметнее всего просела за последние N запусков.

    Сравнивает самый ранний и самый поздний из последних `runs` запусков —
    так провал виден, даже если между ними были промежуточные колебания.
    """
    empty = {"trend": [], "runs_compared": 0, "from_run": None, "to_run": None}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            run_ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT run_id FROM field_completeness_history "
                "WHERE source = ? ORDER BY run_id DESC LIMIT ?",
                (source, runs)).fetchall()]
            if len(run_ids) < 2:
                return empty
            first_run, last_run = min(run_ids), max(run_ids)
            first = dict(conn.execute(
                "SELECT field, fill_rate FROM field_completeness_history "
                "WHERE source = ? AND run_id = ?", (source, first_run)).fetchall())
            last = dict(conn.execute(
                "SELECT field, fill_rate FROM field_completeness_history "
                "WHERE source = ? AND run_id = ?", (source, last_run)).fetchall())
    except sqlite3.Error:
        return empty

    trend = []
    for field, new_rate in last.items():
        old_rate = first.get(field)
        if old_rate is None or new_rate is None:
            continue
        drop = old_rate - new_rate
        if drop >= min_drop:
            trend.append({"field": field, "from": round(old_rate, 1),
                          "to": round(new_rate, 1), "drop": round(drop, 1)})
    trend.sort(key=lambda x: -x["drop"])
    return {"trend": trend[:limit], "runs_compared": len(run_ids),
            "from_run": first_run, "to_run": last_run}
