"""
История запусков и объективные метрики
======================================
Хранит по одной строке на каждый запуск скрапера в SQLite (history/redcat.db),
чтобы можно было строить динамику во времени: сколько квартир/ЖК было, как
менялась средняя цена за м², сколько лотов появлялось и уходило.

Всё считается из фактически собранных данных — никаких оценок «на глаз».
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from datetime import datetime

# Порядок колонок метрик: имя -> тип в SQLite.
# Добавлять новые метрики можно просто дописав сюда — схема мигрирует сама.
RUN_COLUMNS = {
    "started_at": "TEXT",
    "finished_at": "TEXT",
    "duration_sec": "REAL",
    "incomplete": "INTEGER",
    # объёмы
    "regulations_count": "INTEGER",
    "tariffs_count": "INTEGER",
    "hc_count": "INTEGER",
    "apartments_count": "INTEGER",
    "region_total_reported": "INTEGER",
    "coverage_pct": "REAL",
    # изменения относительно прошлого запуска
    "changes_total": "INTEGER",
    "changes_new": "INTEGER",
    "changes_gone": "INTEGER",
    "changes_modified": "INTEGER",
    # ценовые метрики (по квартирам с известной ценой и площадью)
    "apartments_with_price": "INTEGER",
    "price_avg": "REAL",
    "price_median": "REAL",
    "price_min": "REAL",
    "price_max": "REAL",
    "price_per_sqm_avg": "REAL",
    "price_per_sqm_median": "REAL",
    # структура рынка
    "developers_count": "INTEGER",
    "hc_without_contracts": "INTEGER",
}


def _connect(db_path):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path) -> None:
    """Создаёт таблицу runs и доводит её схему до актуальной (мягкая миграция)."""
    with _connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT)")
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
        for col, col_type in RUN_COLUMNS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {col_type}")
        conn.commit()


def _safe_float(value):
    """Аккуратно приводит значение к float. None, если это не число."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        # строки вида "12 500 000" / "12500000.50" / "12,5"
        cleaned = str(value).replace("\xa0", "").replace(" ", "").replace(",", ".")
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def _describe(values):
    """Возвращает (avg, median, min, max) или (None, None, None, None)."""
    if not values:
        return None, None, None, None
    return (
        round(statistics.fmean(values), 2),
        round(statistics.median(values), 2),
        min(values),
        max(values),
    )


def compute_metrics(apartments, hc_list, regulations, tariffs, changes,
                    region_total, incomplete, started_at, finished_at,
                    hc_without_contracts):
    """Считает объективные метрики запуска из уже собранных данных."""
    prices = []
    per_sqm = []

    for a in apartments:
        price = _safe_float(a.get("price")) or _safe_float(a.get("base_price"))
        if price is None or price <= 0:
            continue
        prices.append(price)
        area = _safe_float(a.get("total_area"))
        if area and area > 0:
            per_sqm.append(price / area)

    price_avg, price_median, price_min, price_max = _describe(prices)
    sqm_avg, sqm_median, _, _ = _describe(per_sqm)

    developers = {
        a.get("developer_name") for a in apartments if a.get("developer_name")
    } | {
        hc.get("developer_name") for hc in hc_list if hc.get("developer_name")
    }

    # Изменения разложим по типу: новые / пропавшие / изменённые поля
    new_cnt = sum(1 for c in changes if "Новая запись" in str(c.get("Категория", "")))
    gone_cnt = sum(1 for c in changes if "пропала" in str(c.get("Категория", "")))
    mod_cnt = sum(1 for c in changes if "Изменение поля" in str(c.get("Категория", "")))

    coverage = None
    if region_total:
        coverage = round(100.0 * len(apartments) / region_total, 2)

    return {
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "duration_sec": round((finished_at - started_at).total_seconds(), 1),
        "incomplete": int(bool(incomplete)),
        "regulations_count": len(regulations),
        "tariffs_count": len(tariffs),
        "hc_count": len(hc_list),
        "apartments_count": len(apartments),
        "region_total_reported": region_total,
        "coverage_pct": coverage,
        "changes_total": len(changes),
        "changes_new": new_cnt,
        "changes_gone": gone_cnt,
        "changes_modified": mod_cnt,
        "apartments_with_price": len(prices),
        "price_avg": price_avg,
        "price_median": price_median,
        "price_min": price_min,
        "price_max": price_max,
        "price_per_sqm_avg": round(sqm_avg, 2) if sqm_avg else None,
        "price_per_sqm_median": round(sqm_median, 2) if sqm_median else None,
        "developers_count": len(developers),
        "hc_without_contracts": hc_without_contracts,
    }


def save_run(db_path, metrics: dict) -> int:
    """Пишет метрики запуска в историю. Возвращает run_id."""
    init_db(db_path)
    cols = [c for c in RUN_COLUMNS if c in metrics]
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO runs ({', '.join(cols)}) VALUES ({placeholders})"
    with _connect(db_path) as conn:
        cur = conn.execute(sql, [metrics.get(c) for c in cols])
        conn.commit()
        return cur.lastrowid


def load_history(db_path, limit: int = 100):
    """Читает последние `limit` запусков в хронологическом порядке."""
    try:
        init_db(db_path)
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
    except sqlite3.Error as e:
        logging.warning("Не удалось прочитать историю запусков: %s", e)
        return []


def format_delta(history, field):
    """Человекочитаемая дельта метрики относительно прошлого запуска."""
    if len(history) < 2:
        return ""
    cur, prev = history[-1].get(field), history[-2].get(field)
    if cur is None or prev is None:
        return ""
    diff = cur - prev
    if diff == 0:
        return " (без изменений)"
    sign = "+" if diff > 0 else "−" if diff < 0 else ""
    # Разряды разделяем неразрывным пробелом, а не запятой (русский формат)
    num = f"{abs(diff):,.0f}".replace(",", "\u202f")
    if isinstance(prev, (int, float)) and prev:
        pct = abs(100.0 * diff / prev)
        return f" ({sign}{num}, {sign}{pct:.1f}%)"
    return f" ({sign}{num})"


# ──────────────────────────────────────────────────────────────
#  УНИВЕРСАЛЬНЫЕ МЕТРИКИ (работают с любым источником)
# ──────────────────────────────────────────────────────────────
def _ensure_column(name, col_type="REAL"):
    """Регистрирует новую метрику на лету — схема расширяется сама."""
    if name not in RUN_COLUMNS:
        RUN_COLUMNS[name] = col_type


def compute_generic_metrics(normalized, totals, changes, incomplete,
                            started_at, finished_at, primary, primary_spec,
                            all_specs=None):
    """Считает метрики по ЛЮБОМУ набору источников.

    Для каждого источника пишется <key>_count. Для числовых полей КАЖДОГО
    источника (не только главного) — среднее/медиана/мин/макс. Это даёт
    в обзоре отдельные графики по Redcat и по внешним источникам.
    """
    metrics = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "finished_at": finished_at.isoformat(timespec="seconds"),
        "duration_sec": round((finished_at - started_at).total_seconds(), 1),
        "incomplete": int(bool(incomplete)),
        "changes_total": len(changes),
        "changes_new": sum(1 for c in changes if "Новая запись" in str(c.get("Категория", ""))),
        "changes_gone": sum(1 for c in changes if "пропала" in str(c.get("Категория", ""))),
        "changes_modified": sum(1 for c in changes if "Изменение поля" in str(c.get("Категория", ""))),
        "sources_count": len(normalized),
    }
    _ensure_column("sources_count", "INTEGER")

    # объём по каждому источнику
    for key, rows in normalized.items():
        col = f"{key}_count"
        _ensure_column(col, "INTEGER")
        metrics[col] = len(rows)

    # полнота сбора — по главному источнику
    primary_total = totals.get(primary)
    metrics["region_total_reported"] = primary_total
    metrics["apartments_count"] = len(normalized.get(primary, []))
    if primary_total:
        metrics["coverage_pct"] = round(100.0 * len(normalized[primary]) / primary_total, 2)
    else:
        metrics["coverage_pct"] = None

    # Статистика по числовым полям — по КАЖДОМУ источнику.
    specs = dict(all_specs or {})
    if primary and primary_spec and primary not in specs:
        specs[primary] = primary_spec

    for key, rows in normalized.items():
        spec = specs.get(key)
        if not spec or not rows or not spec.numeric_fields:
            continue
        for fld in spec.numeric_fields:
            values = [_safe_float(r.get(fld)) for r in rows]
            values = [v for v in values if v is not None and v > 0]
            if not values:
                continue
            avg, med, vmin, vmax = _describe(values)
            for suffix, value, ctype in [
                ("avg", avg, "REAL"), ("median", med, "REAL"),
                ("min", vmin, "REAL"), ("max", vmax, "REAL"),
            ]:
                col = f"{key}_{fld}_{suffix}"
                _ensure_column(col, ctype)
                metrics[col] = value

            # Обратная совместимость с прежними графиками цен: для
            # apartments price_* / price_per_sqm_* клались без префикса.
            if key == "apartments":
                if fld == "price":
                    metrics.update({"price_avg": avg, "price_median": med,
                                    "price_min": vmin, "price_max": vmax,
                                    "apartments_with_price": len(values)})
                if fld == "price_per_sqm":
                    metrics.update({"price_per_sqm_avg": avg,
                                    "price_per_sqm_median": med})

        if key == primary:
            for gfld in spec.group_fields:
                col = f"{primary}_{gfld}_unique".replace(".", "_")
                _ensure_column(col, "INTEGER")
                metrics[col] = len({r.get(gfld) for r in rows
                                    if r.get(gfld) is not None})
                if gfld.endswith("developer_name"):
                    metrics["developers_count"] = metrics[col]

    metrics.setdefault("hc_count",
                       len(normalized.get("housing_complexes", [])))
    return metrics
