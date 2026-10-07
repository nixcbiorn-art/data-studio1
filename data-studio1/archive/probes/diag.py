"""
diag.py — что происходило во время последнего сбора.
=====================================================
Только читает базы. Ничего не меняет, в сеть не ходит.

Отвечает на 4 вопроса:
  1. Сколько запросов сделал сбор и сколько это заняло времени.
  2. Какие запросы были медленными, где таймауты и ошибки.
  3. Сколько застройщиков в каждом источнике (tariffs / housing_complexes /
     apartments) — чтобы понять, покрывает ли split все лоты.
  4. Сколько квартир реально собрано и сколько должно быть.
"""
from __future__ import annotations

import sqlite3
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent
STUDIO_DB = BASE / "studio.db"
REDCAT_DB = BASE / "reports" / "redcat_data.db"

SEP = "─" * 74


def open_ro(path: Path):
    if not path.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def fmt(n):
    if n is None:
        return "—"
    if isinstance(n, float):
        return f"{n:,.1f}".replace(",", " ")
    return f"{n:,}".replace(",", " ")


# ──────────────────────────────────────────────────────────────
#  1. Активность последнего сбора
# ──────────────────────────────────────────────────────────────
def section_api_audit() -> None:
    print(f"\n{SEP}\n  1. АКТИВНОСТЬ СБОРА (studio.db → api_audit)\n{SEP}")
    conn = open_ro(STUDIO_DB)
    if conn is None:
        print("  ⚠️  studio.db не найден.")
        return

    row = conn.execute(
        "SELECT MIN(ts) AS first, MAX(ts) AS last, COUNT(*) AS n "
        "FROM api_audit").fetchone()
    if not row or not row["n"]:
        print("  ⚠️  В api_audit пусто.")
        conn.close()
        return

    print(f"  Первый запрос:  {row['first']}")
    print(f"  Последний:      {row['last']}")
    print(f"  Всего записей:  {fmt(row['n'])}")

    # сколько за последний час / последние 30 минут
    for minutes in (60, 30, 10, 5):
        n = conn.execute(
            "SELECT COUNT(*) FROM api_audit "
            "WHERE ts >= datetime('now', ?)", (f"-{minutes} minutes",)
        ).fetchone()[0]
        print(f"  За последние {minutes:>2} мин: {fmt(n)}")

    # ошибки и коды статусов
    print("\n  Статусы ответов:")
    for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM api_audit "
            "GROUP BY status ORDER BY n DESC LIMIT 10"):
        label = r["status"] if r["status"] is not None else "(нет — таймаут)"
        print(f"    {str(label):>20}  {fmt(r['n'])}")

    # по методам
    print("\n  Методы:")
    for r in conn.execute(
            "SELECT method, COUNT(*) AS n FROM api_audit "
            "GROUP BY method ORDER BY n DESC"):
        print(f"    {r['method']:>6}  {fmt(r['n'])}")

    # распределение времени ответа
    print("\n  Распределение по времени ответа:")
    buckets = [
        ("быстрее 0.5 сек", 0, 500),
        ("0.5–1 сек", 500, 1000),
        ("1–2 сек", 1000, 2000),
        ("2–5 сек", 2000, 5000),
        ("5–10 сек", 5000, 10000),
        ("10–20 сек", 10000, 20000),
        ("дольше 20 сек", 20000, 10**9),
    ]
    for label, lo, hi in buckets:
        n = conn.execute(
            "SELECT COUNT(*) FROM api_audit WHERE ms >= ? AND ms < ?",
            (lo, hi)).fetchone()[0]
        if n:
            bar = "█" * min(40, int(n / max(1, row["n"]) * 40))
            print(f"    {label:<18}  {fmt(n):>8}  {bar}")

    # группировка по хостам
    print("\n  По хостам (top-10):")
    for r in conn.execute("""
        SELECT
            CASE
              WHEN url LIKE '%redcat.ai%'  THEN 'redcat'
              WHEN url LIKE '%a101.ru%'    THEN 'a101'
              WHEN url LIKE '%fsk.ru%'     THEN 'fsk'
              WHEN url LIKE '%samolet%'    THEN 'samolet'
              WHEN url LIKE '%jcat.ru%'    THEN 'jcat'
              WHEN url LIKE '%rbi.ru%'     THEN 'rbi'
              WHEN url LIKE '%dsk%'        THEN 'dsk'
              ELSE 'прочие'
            END AS host,
            COUNT(*) AS n,
            ROUND(AVG(ms)) AS avg_ms,
            MAX(ms) AS max_ms,
            SUM(CASE WHEN status IS NULL OR status >= 400 THEN 1 ELSE 0 END) AS errors
        FROM api_audit
        GROUP BY host
        ORDER BY n DESC
        LIMIT 10
    """):
        print(f"    {r['host']:<10}  запросов {fmt(r['n']):>6}  "
              f"ср {fmt(r['avg_ms']):>6} мс  "
              f"макс {fmt(r['max_ms']):>7} мс  "
              f"ошибок {fmt(r['errors'])}")

    # топ медленных
    print("\n  Топ-10 самых медленных запросов:")
    for r in conn.execute("""
        SELECT ts, method, status, ms, substr(url, 1, 100) AS u
        FROM api_audit
        WHERE method = 'GET'
        ORDER BY ms DESC LIMIT 10
    """):
        print(f"    {r['ms']:>7} мс  {r['status'] or '—':>4}  {r['u']}")

    conn.close()


# ──────────────────────────────────────────────────────────────
#  2. Застройщики в разных источниках
# ──────────────────────────────────────────────────────────────
def section_developers() -> None:
    print(f"\n{SEP}\n  2. ЗАСТРОЙЩИКИ В ТРЁХ ИСТОЧНИКАХ\n{SEP}")
    conn = open_ro(REDCAT_DB)
    if conn is None:
        print("  ⚠️  redcat_data.db не найден.")
        return

    def count_distinct(table, column):
        try:
            return conn.execute(
                f'SELECT COUNT(DISTINCT "{column}") FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL').fetchone()[0]
        except sqlite3.Error:
            return None

    # сколько застройщиков в каждой таблице
    d_tariffs = count_distinct("tariffs", "developer.name")
    if d_tariffs is None:
        d_tariffs = count_distinct("tariffs", "provider.name")
    d_hc = count_distinct("housing_complexes", "developer_name")
    d_apt = count_distinct("apartments", "developer_name")

    print(f"  Уникальных застройщиков:")
    print(f"    в tariffs              {fmt(d_tariffs):>6}  ← source сплита")
    print(f"    в housing_complexes    {fmt(d_hc):>6}")
    print(f"    в apartments           {fmt(d_apt):>6}  ← целевая таблица")

    # сколько ЖК принадлежит застройщикам, которых НЕТ в tariffs
    try:
        rows = conn.execute("""
            SELECT COUNT(DISTINCT hc.developer_name) AS missing
            FROM housing_complexes hc
            WHERE hc.developer_name IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM tariffs t
                WHERE t."developer.name" = hc.developer_name
                   OR t."provider.name" = hc.developer_name
              )
        """).fetchone()
        if rows:
            print(f"\n  Застройщиков есть в housing_complexes, но НЕТ в tariffs: "
                  f"{fmt(rows['missing'])}")
    except sqlite3.Error as e:
        print(f"\n  ⚠️  сравнение по застройщикам не сработало: {e}")

    # суммарное количество квартир в apartments
    try:
        total_apt = conn.execute(
            "SELECT COUNT(*) FROM apartments").fetchone()[0]
        print(f"\n  Всего квартир в apartments: {fmt(total_apt)}")
        print(f"  Из них с заполненным developer_name: "
              f"{fmt(conn.execute('SELECT COUNT(*) FROM apartments WHERE developer_name IS NOT NULL').fetchone()[0])}")
    except sqlite3.Error:
        pass

    conn.close()


# ──────────────────────────────────────────────────────────────
#  3. Итоговая сводка
# ──────────────────────────────────────────────────────────────
def section_verdict() -> None:
    print(f"\n{SEP}\n  3. ЧТО ЭТО ЗНАЧИТ\n{SEP}")
    conn = open_ro(STUDIO_DB)
    if conn is None:
        return

    row = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status IS NULL THEN 1 ELSE 0 END) AS no_status,
            SUM(CASE WHEN status >= 500 THEN 1 ELSE 0 END) AS server_errors,
            SUM(CASE WHEN status = 429 THEN 1 ELSE 0 END) AS rate_limited,
            ROUND(AVG(ms)) AS avg_ms,
            MAX(ms) AS max_ms
        FROM api_audit
    """).fetchone()
    conn.close()

    if row and row["total"]:
        print(f"  Всего запросов в журнале: {fmt(row['total'])}")
        print(f"  Без ответа (таймаут):     {fmt(row['no_status'])}")
        print(f"  HTTP 5xx:                 {fmt(row['server_errors'])}")
        print(f"  HTTP 429 (лимит):         {fmt(row['rate_limited'])}")
        print(f"  Среднее время ответа:     {fmt(row['avg_ms'])} мс")
        print(f"  Максимум:                 {fmt(row['max_ms'])} мс")

        print()
        if (row["no_status"] or 0) > 20:
            print("  ⚠️  Много таймаутов. Возможные причины:")
            print("     — API не успевает за REQUEST_TIMEOUT (сейчас 20 сек)")
            print("     — слишком высокая concurrency")
            print("     — сеть/провайдер режет долгие соединения")
        if (row["rate_limited"] or 0) > 0:
            print(f"  ⚠️  Были HTTP 429 — API просит сбавить темп. "
                  f"Снизьте concurrency у apartments.")
        if (row["server_errors"] or 0) > 0:
            print(f"  ⚠️  Были HTTP 5xx — серверные ошибки API. "
                  f"Обычно проходят при повторе.")

    print(f"\n{SEP}")
    print("  Готово. Пришлите вывод целиком — по нему видно, где теряется")
    print("  время и почему собралось меньше, чем заявлено.")
    print(f"{SEP}\n")


if __name__ == "__main__":
    try:
        section_api_audit()
        section_developers()
        section_verdict()
    except Exception as e:  # noqa: BLE001
        print(f"❌ Ошибка: {type(e).__name__}: {e}")
        sys.exit(1)