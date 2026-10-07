"""
Хранение выгрузок
=================
Excel плохо тянет десятки тысяч строк: файл открывается минутами, фильтры
подвисают, а лист вообще ограничен 1 048 576 строками. Поэтому основной
формат — SQLite и Parquet, а Excel остаётся опцией (--excel) с защитой от
переполнения листа.

Форматы:
  • SQLite (redcat_data.db) — основной. Открывается в DB Browser for SQLite,
    цепляется к Power BI / Excel Power Query / pandas. Умеет SQL и индексы,
    не грузит всё в память.
  • Parquet — колоночный, сжатый (в 5-20 раз меньше xlsx), мгновенно читается
    pandas/Polars. Нужен pyarrow; если его нет, молча падаем на CSV.
  • CSV — универсальный запасной вариант, всегда пишется при отсутствии Parquet.
"""

from __future__ import annotations

import logging
import sqlite3

import pandas as pd

EXCEL_ROW_LIMIT = 1_048_576
EXCEL_SAFE_LIMIT = 200_000  # выше этого Excel уже мучительно медленный


def _df(rows):
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def write_sqlite(db_path, tables: dict, run_id=None) -> None:
    """Пишет таблицы в SQLite, заменяя содержимое, и строит индексы.

    tables: {имя_таблицы: список_словарей}
    """
    with sqlite3.connect(str(db_path)) as conn:
        for name, rows in tables.items():
            df = _df(rows)
            if df.empty:
                continue
            if run_id is not None:
                df = df.assign(run_id=run_id)
            # sqlite не умеет списки/словари в ячейках — приводим к строкам
            for col in df.columns:
                if df[col].map(lambda v: isinstance(v, (list, dict))).any():
                    df[col] = df[col].map(lambda v: str(v) if isinstance(v, (list, dict)) else v)
            df.to_sql(name, conn, if_exists="replace", index=False)

        # Индексы под самые частые фильтры — без них большие таблицы тормозят
        for table, column in [
            ("apartments", "housing_complex_id"),
            ("apartments", "developer_name"),
            ("regulations", "housing_complex_id"),
            ("tariffs", "housing_complex_id"),
            ("housing_complexes", "id"),
        ]:
            try:
                conn.execute(
                    f'CREATE INDEX IF NOT EXISTS idx_{table}_{column} ON {table}("{column}")'
                )
            except sqlite3.Error:
                pass  # таблицы/колонки может не быть — это нормально
        conn.commit()


def write_columnar(out_dir, tables: dict, timestamp: str) -> list:
    """Пишет каждую таблицу в Parquet (или CSV, если pyarrow не установлен)."""
    written = []
    try:
        import pyarrow  # noqa: F401
        engine_ok = True
    except ImportError:
        engine_ok = False
        logging.info("pyarrow не установлен — вместо Parquet пишем CSV.")

    for name, rows in tables.items():
        df = _df(rows)
        if df.empty:
            continue
        if engine_ok:
            path = out_dir / f"{name}_{timestamp}.parquet"
            try:
                df.to_parquet(path, index=False, compression="snappy")
                written.append(path)
                continue
            except (ImportError, ValueError, OSError) as e:
                logging.warning("Не удалось записать Parquet для %s: %s", name, e)
        path = out_dir / f"{name}_{timestamp}.csv"
        df.to_csv(path, index=False, encoding="utf-8-sig")
        written.append(path)
    return written


def write_excel(path, tables: dict) -> tuple:
    """Опциональный Excel. Крупные таблицы обрезает, чтобы файл не вис.

    Возвращает (успех, список_предупреждений).
    """
    warnings = []
    try:
        with pd.ExcelWriter(path, engine="openpyxl") as writer:
            for sheet, rows in tables.items():
                df = _df(rows)
                if df.empty:
                    df = pd.DataFrame([{"Информация": "Нет данных"}])
                if len(df) > EXCEL_SAFE_LIMIT:
                    warnings.append(
                        f"Лист '{sheet}': {len(df)} строк обрезано до {EXCEL_SAFE_LIMIT} — "
                        f"полные данные в SQLite/Parquet."
                    )
                    df = df.head(EXCEL_SAFE_LIMIT)
                df.to_excel(writer, sheet_name=sheet[:31], index=False)
        return True, warnings
    except PermissionError:
        return False, [f"Файл '{path}' открыт в другой программе."]
    except (OSError, ValueError) as e:
        return False, [f"Не удалось записать Excel: {e}"]


# ──────────────────────────────────────────────────────────────
#  ИСТОРИЯ АНОМАЛИЙ
# ──────────────────────────────────────────────────────────────
ANOMALY_COLUMNS = ["run_id", "detected_at", "source", "kind", "severity",
                   "entity", "metric", "value", "expected", "deviation", "message"]


def init_anomalies(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS anomalies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER, detected_at TEXT, source TEXT, kind TEXT,
                severity TEXT, entity TEXT, metric TEXT,
                value REAL, expected REAL, deviation REAL, message TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_anom_run ON anomalies(run_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_anom_sev ON anomalies(severity)")
        conn.commit()


def save_anomalies(db_path, run_id, anomalies, detected_at):
    """Пишет аномалии запуска в накопительную историю."""
    if not anomalies:
        return 0
    init_anomalies(db_path)
    rows = []
    for a in anomalies:
        rows.append((
            run_id, detected_at, a.get("source"), a.get("kind"), a.get("severity"),
            str(a.get("entity") or ""), str(a.get("metric") or ""),
            _as_real(a.get("value")), _as_real(a.get("expected")),
            _as_real(a.get("deviation")), a.get("message"),
        ))
    with sqlite3.connect(str(db_path)) as conn:
        conn.executemany(
            f"INSERT INTO anomalies ({', '.join(ANOMALY_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in ANOMALY_COLUMNS)})", rows)
        conn.commit()
    return len(rows)


def _as_real(v):
    try:
        return float(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


def load_anomaly_counts(db_path, limit=100):
    """Считает аномалии по запускам — для графика динамики."""
    try:
        init_anomalies(db_path)
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("""
                SELECT run_id,
                       SUM(severity='critical') AS critical,
                       SUM(severity='warning')  AS warning,
                       SUM(severity='info')     AS info
                FROM anomalies GROUP BY run_id ORDER BY run_id DESC LIMIT ?
            """, (limit,)).fetchall()
        return {r[0]: {"critical": r[1] or 0, "warning": r[2] or 0, "info": r[3] or 0}
                for r in rows}
    except sqlite3.Error as e:
        logging.warning("Не удалось прочитать историю аномалий: %s", e)
        return {}


def append_record_history(db_path, source, run_id, rows, id_field, numeric_fields, ts):
    """Копит значения числовых полей по каждой записи — история цены лота и т.п.

    Это то, чего не даёт снапшот: снапшот знает только прошлый запуск,
    а здесь остаётся весь ряд, по которому можно строить график по одному
    объекту и считать долгосрочный тренд.
    """
    if not rows or not numeric_fields:
        return 0
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS record_history (
                source TEXT, record_id TEXT, run_id INTEGER, ts TEXT,
                field TEXT, value REAL
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rh ON record_history(source, record_id, field)")
        batch = []
        for r in rows:
            rid = r.get(id_field)
            if rid is None:
                continue
            for fld in numeric_fields:
                v = _as_real(r.get(fld))
                if v is not None:
                    batch.append((source, str(rid), run_id, ts, fld, v))
        if batch:
            conn.executemany("INSERT INTO record_history VALUES (?,?,?,?,?,?)", batch)
        conn.commit()
    return len(batch)


def prune_record_history(db_path, keep_runs=30):
    """Удерживает размер истории записей: хранит последние N запусков."""
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.execute("SELECT DISTINCT run_id FROM record_history ORDER BY run_id DESC")
            run_ids = [r[0] for r in cur.fetchall()]
            if len(run_ids) > keep_runs:
                cutoff = run_ids[keep_runs - 1]
                conn.execute("DELETE FROM record_history WHERE run_id < ?", (cutoff,))
                conn.commit()
    except sqlite3.Error as e:
        logging.warning("Не удалось почистить историю записей: %s", e)
