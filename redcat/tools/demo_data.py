"""
ДЕМО-ДАННЫЕ
===========
Создаёт правдоподобный набор данных, чтобы посмотреть приложение до того,
как появится рабочий токен. Никуда не ходит по сети — всё генерируется
локально.

    python -m redcat.tools.demo_data            # создать (если базы ещё нет)
    python -m redcat.tools.demo_data --force    # пересоздать поверх

⚠️ Данные выдуманные. Не используйте их для выводов о рынке — они нужны
только чтобы пощупать интерфейс: фильтры, поиск, сводные, правки.
"""

from __future__ import annotations

from redcat.core import paths
import argparse
import random
import sqlite3
import sys

# Если вывод перенаправлен в файл (а не в реальную консоль), Python берёт
# кодировку локали ОС (на русской Windows — обычно cp1251), которая не
# умеет печатать часть символов из сообщений ниже — это роняло скрипт с
# UnicodeEncodeError вместо создания демо-данных.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = paths.ROOT
OUTPUT_DIR = BASE_DIR / "reports"
HISTORY_DIR = BASE_DIR / "history"

DEVELOPERS = ["СтройГрад", "Северный Дом", "ПИК-Регион", "Альфа Девелопмент",
              "Монолит-Инвест", "ЖилСтандарт", "Гранд Недвижимость"]
DISTRICTS = ["Центральный", "Северный", "Заречный", "Приморский",
             "Октябрьский", "Солнечный"]
DECOR = ["Без отделки", "Черновая", "Предчистовая", "Чистовая", "С мебелью"]
STATUSES = ["Активен", "Активен", "Активен", "На согласовании"]
PROVIDERS = ["Банк Первый", "Банк Второй", "Ипотечный Дом", "КредитЛайн"]


def build(force=False) -> int:
    OUTPUT_DIR.mkdir(exist_ok=True)
    HISTORY_DIR.mkdir(exist_ok=True)
    db_path = OUTPUT_DIR / "redcat_data.db"
    if db_path.exists() and not force:
        print(f"⚠️ {db_path.name} уже существует. Перезаписать: "
              f"python -m redcat.tools.demo_data --force")
        return 1

    rnd = random.Random(20260916)
    conn = sqlite3.connect(str(db_path))

    # ── Жилые комплексы ──
    hc_rows = []
    for i in range(1, 61):
        dev = rnd.choice(DEVELOPERS)
        hc_rows.append((
            1000 + i, f"ЖК {rnd.choice(['Солнечный','Парковый','Речной','Северный','Высокий','Зелёный','Каскад'])}-{i}",
            dev, rnd.choice(DISTRICTS),
            f"ул. {rnd.choice(['Ленина','Мира','Садовая','Заводская','Полевая'])}, {rnd.randint(1, 120)}",
            f"{rnd.choice(['I','II','III','IV'])} кв. {rnd.choice([2026, 2027, 2028])}",
            rnd.randint(3, 25), round(rnd.uniform(3.2, 9.8) * 1_000_000, -4),
            rnd.choice(["true", "false"]),
            (datetime.now() - timedelta(days=rnd.randint(30, 900))).strftime("%Y-%m-%d"),
        ))
    conn.execute("""CREATE TABLE housing_complexes (
        id INTEGER, name TEXT, developer_name TEXT, district TEXT, address TEXT,
        deadline TEXT, buildings_count INTEGER, min_price_apartments REAL,
        is_active TEXT, created_at TEXT)""")
    conn.executemany("INSERT INTO housing_complexes VALUES (?,?,?,?,?,?,?,?,?,?)", hc_rows)

    # ── Квартиры ──
    apt_rows = []
    apt_id = 500000
    for hc in hc_rows:
        hc_id, hc_name, dev, district = hc[0], hc[1], hc[2], hc[3]
        base_sqm = rnd.uniform(95_000, 240_000)
        for _ in range(rnd.randint(40, 260)):
            apt_id += 1
            rooms = rnd.choice([0, 1, 1, 2, 2, 3, 4])
            area = round(rnd.uniform(24, 42) + rooms * rnd.uniform(11, 20), 1)
            floor = rnd.randint(1, 25)
            sqm = base_sqm * rnd.uniform(0.85, 1.2) * (1 + floor / 400)
            price = round(area * sqm, -3)
            # намеренные «грязные» случаи, чтобы были видны проверки качества
            if rnd.random() < 0.01:
                price = 0
            if rnd.random() < 0.006:
                area = 0
            apt_rows.append((
                apt_id, hc_id, hc_name, dev, district, rooms, area, floor,
                rnd.randint(floor, 25), price,
                round(price / area, 2) if area else None,
                rnd.choice(DECOR), rnd.choice(["Квартира", "Квартира", "Апартаменты", "Студия"]),
                rnd.choice(["В продаже", "В продаже", "Забронирована", "Продана"]),
                (datetime.now() - timedelta(days=rnd.randint(0, 400))).strftime("%Y-%m-%d"),
            ))
    conn.execute("""CREATE TABLE apartments (
        id INTEGER, housing_complex_id INTEGER, housing_complex_name TEXT,
        developer_name TEXT, district TEXT, rooms INTEGER, total_area REAL,
        floor INTEGER, floors_total INTEGER, price REAL, price_per_sqm REAL,
        decoration_type TEXT, object_type TEXT, status TEXT, created_at TEXT)""")
    conn.executemany(
        "INSERT INTO apartments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", apt_rows)

    # ── Регламенты ──
    reg_rows = []
    for i, hc in enumerate(rnd.sample(hc_rows, 45), start=1):
        reg_rows.append((
            7000 + i, hc[0], hc[1], rnd.choice(PROVIDERS), rnd.choice(STATUSES),
            (datetime.now() - timedelta(days=rnd.randint(10, 400))).strftime("%Y-%m-%d"),
            (datetime.now() + timedelta(days=rnd.randint(10, 400))).strftime("%Y-%m-%d"),
            round(rnd.uniform(0.5, 4.5), 2)))
    conn.execute("""CREATE TABLE regulations (
        id INTEGER, housing_complex_id INTEGER, housing_complex_name TEXT,
        provider_name TEXT, status_name TEXT, date_from TEXT, date_to TEXT,
        commission_percent REAL)""")
    conn.executemany("INSERT INTO regulations VALUES (?,?,?,?,?,?,?,?)", reg_rows)

    # ── Тарифные карты ──
    tar_rows = []
    for i, reg in enumerate(reg_rows, start=1):
        tar_rows.append((
            9000 + i, reg[1], reg[2], reg[3], rnd.choice(STATUSES),
            round(rnd.uniform(0.3, 2.0), 2), round(rnd.uniform(2.0, 6.5), 2),
            rnd.choice([12, 24, 36, 60, 120, 240])))
    conn.execute("""CREATE TABLE tariffs (
        id INTEGER, housing_complex_id INTEGER, housing_complex_name TEXT,
        provider_name TEXT, status_name TEXT, min_percent REAL,
        max_percent REAL, payment_term INTEGER)""")
    conn.executemany("INSERT INTO tariffs VALUES (?,?,?,?,?,?,?,?)", tar_rows)

    # ── Лист сравнения с прошлым запуском ──
    conn.execute("""CREATE TABLE comparison_vs_previous (
        "Категория" TEXT, "ID" TEXT, "Название" TEXT, "Поле" TEXT,
        "Было" TEXT, "Стало" TEXT)""")
    changes = []
    for apt in rnd.sample(apt_rows, 60):
        old = apt[9] * rnd.uniform(0.9, 1.1)
        changes.append(("Квартиры и лоты: Изменение поля", str(apt[0]), apt[2],
                        "price", f"{old:.0f}", f"{apt[9]:.0f}"))
    for apt in rnd.sample(apt_rows, 25):
        changes.append(("Квартиры и лоты: Новая запись", str(apt[0]), apt[2], "", "", ""))
    for i in range(12):
        changes.append(("Квартиры и лоты: Запись пропала", str(400000 + i),
                        "ЖК Прошлый", "", "", ""))
    conn.executemany("INSERT INTO comparison_vs_previous VALUES (?,?,?,?,?,?)", changes)

    for table, column in [("apartments", "housing_complex_id"),
                          ("apartments", "developer_name"),
                          ("housing_complexes", "id")]:
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_{table}_{column} '
                     f'ON {table}("{column}")')
    conn.commit()
    conn.close()

    _build_history(rnd, len(apt_rows), len(hc_rows), len(reg_rows), len(tar_rows))

    print(f"✅ Демо-данные созданы: {db_path}")
    print(f"   ЖК: {len(hc_rows)}, квартир: {len(apt_rows)}, "
          f"регламентов: {len(reg_rows)}, тарифов: {len(tar_rows)}")
    print("   Запустите приложение:  python studio.py")
    return 0


def _build_history(rnd, apt_count, hc_count, reg_count, tar_count) -> None:
    """История запусков — чтобы на дашборде были графики динамики."""
    from redcat.data import run_stats
    db = HISTORY_DIR / "run_stats.db"
    run_stats.init_db(db)
    with sqlite3.connect(str(db)) as conn:
        if conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]:
            return
    base_price = 6_800_000
    for day in range(14, 0, -1):
        started = datetime.now() - timedelta(days=day, hours=rnd.randint(0, 3))
        drift = 1 + (14 - day) * 0.004 + rnd.uniform(-0.01, 0.01)
        count = int(apt_count * rnd.uniform(0.93, 1.02))
        run_stats.save_run(db, {
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": (started + timedelta(minutes=rnd.randint(2, 9))
                            ).isoformat(timespec="seconds"),
            "duration_sec": rnd.uniform(120, 540),
            "incomplete": 1 if day == 9 else 0,
            "regulations_count": reg_count + rnd.randint(-2, 2),
            "tariffs_count": tar_count + rnd.randint(-2, 2),
            "hc_count": hc_count + rnd.randint(-1, 1),
            "apartments_count": count,
            "region_total_reported": int(apt_count * 1.05),
            "coverage_pct": round(100 * count / (apt_count * 1.05), 2),
            "changes_total": rnd.randint(40, 180),
            "changes_new": rnd.randint(10, 60),
            "changes_gone": rnd.randint(2, 30),
            "changes_modified": rnd.randint(20, 100),
            "apartments_with_price": int(count * 0.98),
            "price_avg": round(base_price * drift, 2),
            "price_median": round(base_price * drift * 0.94, 2),
            "price_min": 1_900_000, "price_max": 41_000_000,
            "price_per_sqm_avg": round(148_000 * drift, 2),
            "price_per_sqm_median": round(142_000 * drift, 2),
            "developers_count": len(DEVELOPERS),
            "hc_without_contracts": rnd.randint(8, 20),
            "sources_count": 4,
        })


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Генератор демо-данных")
    ap.add_argument("--force", action="store_true", help="перезаписать существующую базу")
    sys.exit(build(ap.parse_args().force))
