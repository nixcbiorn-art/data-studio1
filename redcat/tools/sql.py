"""
Быстрый запуск SQL-запросов из командной строки.
================================================
Читает базу только на чтение (mode=ro), так что данные изменить нельзя.

Использование:
    python -m redcat.tools.sql "SELECT COUNT(*) FROM apartments"
    python -m redcat.tools.sql "SELECT crmObjectType, COUNT(*) FROM apartments GROUP BY 1"
    python -m redcat.tools.sql                                  — интерактивный режим
    python -m redcat.tools.sql --db reports/external_data.db "SELECT ..."

В интерактивном режиме:
    • введите запрос и нажмите Enter
    • Ctrl+C или пустая строка — выход
"""

from __future__ import annotations

from redcat.core import paths
import argparse
import sqlite3
import sys
from pathlib import Path

BASE_DIR = paths.ROOT
DEFAULT_DB = BASE_DIR / "reports" / "redcat_data.db"


def open_ro(path: Path) -> sqlite3.Connection:
    if not path.exists():
        print(f"❌ База не найдена: {path}")
        sys.exit(1)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def print_rows(rows, max_cols=20, max_width=40):
    """Печатает результат в виде таблицы. Обрезает широкие ячейки."""
    if not rows:
        print("(пусто)")
        return
    cols = list(rows[0].keys())

    # Считаем ширины колонок
    widths = []
    for c in cols:
        w = len(str(c))
        for r in rows:
            v = r[c]
            if v is None:
                v = "—"
            else:
                v = str(v)
                if len(v) > max_width:
                    v = v[:max_width - 1] + "…"
            w = max(w, len(v))
        widths.append(min(w, max_width))

    # Заголовок
    header = "  ".join(f"{c:<{widths[i]}}" for i, c in enumerate(cols))
    print(header)
    print("-" * len(header))

    # Данные
    for r in rows:
        parts = []
        for i, c in enumerate(cols):
            v = r[c]
            if v is None:
                v = "—"
            else:
                v = str(v)
                if len(v) > widths[i]:
                    v = v[:widths[i] - 1] + "…"
            parts.append(f"{v:<{widths[i]}}")
        print("  ".join(parts))
    print(f"\n({len(rows)} строк)")


def run_one(conn, sql: str, max_rows=200):
    sql = sql.strip().rstrip(";")
    if not sql:
        return
    # Защита: только SELECT / WITH
    low = sql.lstrip().lower()
    if not (low.startswith("select") or low.startswith("with")
            or low.startswith("pragma")):
        print("❌ Разрешены только SELECT / WITH / PRAGMA")
        return
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows)
    except sqlite3.Error as e:
        print(f"❌ SQLite: {e}")
        return
    print_rows(rows)


def main():
    ap = argparse.ArgumentParser(description="SQL-консоль для RedCat")
    ap.add_argument("query", nargs="*", help="SQL-запрос (если пусто — интерактивно)")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="путь к базе")
    args = ap.parse_args()

    conn = open_ro(Path(args.db))
    print(f"📁 {args.db}  (read-only)")

    if args.query:
        run_one(conn, " ".join(args.query))
        return 0

    print("Введите SQL-запрос. Пустая строка — выход.\n")
    try:
        while True:
            sql = input("sql> ").strip()
            if not sql:
                break
            run_one(conn, sql)
            print()
    except (KeyboardInterrupt, EOFError):
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())