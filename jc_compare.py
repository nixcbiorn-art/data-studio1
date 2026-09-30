"""
jc_compare.py — построчное сравнение лотов одного ЖК.
========================================================
Запуск:
    python jc_compare.py "Тропарево"
    python jc_compare.py "Деснаречье" --source a101_apartments
    python jc_compare.py "Скай Спутник" --source samolet_apartments
    python jc_compare.py "Веер" --source fsk_apartments --limit 100
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import webapp


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(" ", "").replace("\xa0", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _row_val(row, *candidates):
    for c in candidates:
        if c in row and row[c] not in (None, ""):
            return row[c]
    return None


def load_side(db, table, name_col, like, id_col, price_col,
              area_col, rooms_col):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute(
            f'SELECT * FROM "{table}" WHERE "{name_col}" LIKE ?',
            (f"%{like}%",))]
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({
            "id": r.get(id_col),
            "price": _num(r.get(price_col)),
            "area": _num(r.get(area_col)) if area_col else None,
            "rooms": r.get(rooms_col) if rooms_col else None,
            "raw": r,
        })
    return out


def key_for(row):
    """Ключ сопоставления: комнатность + площадь + цена."""
    r = str(row.get("rooms") if row.get("rooms") is not None else "")
    a = row.get("area")
    p = row.get("price")
    a_s = f"{round(a, 1):.1f}" if a is not None else "?"
    p_s = f"{round(p, -3):.0f}" if p is not None else "?"
    return (r, a_s, p_s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="подстрока названия ЖК")
    ap.add_argument("--source", default="samolet_apartments",
                    help="имя таблицы-источника")
    ap.add_argument("--limit", type=int, default=200,
                    help="сколько лотов показать (по умолчанию 200)")
    args = ap.parse_args()

    specs = webapp.load_specs()
    spec = specs.get(args.source)
    if not spec:
        print(f"❌ Источник {args.source!r} не найден в spec.")
        return 1
    cc = getattr(spec, "cross_check", None) or {}
    if not cc:
        print(f"❌ У источника {args.source!r} нет cross_check.")
        return 1

    on_left = cc.get("on_left")
    on_right = cc.get("on_right")
    right_table = cc.get("with_table") or "apartments"
    metrics = cc.get("metrics") or {}

    # Ищем конкретное имя в левой и правой таблице.
    left_db = webapp.db_for_table(args.source)
    right_db = webapp.db_for_table(right_table)

    # Подбираем колонки id/price/area/rooms в каждой стороне.
    def col_candidates(cols, patterns):
        return [c for c in cols if any(p in c.lower() for p in patterns)]

    def list_cols(db, table):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                return [r[1] for r in conn.execute(
                    f'PRAGMA table_info("{table}")')]
            finally:
                conn.close()
        except sqlite3.Error:
            return []

    left_cols = list_cols(left_db, args.source)
    right_cols = list_cols(right_db, right_table)

    def pick(cols, *patterns):
        for p in patterns:
            for c in cols:
                if p == c.lower() or p in c.lower():
                    return c
        return None

    left_id = pick(left_cols, "id", "_id")
    left_price = pick(left_cols, "discounted_price", "sale_price",
                      "promo_price", "price")
    left_area = pick(left_cols, "area_total", "total_area", "area")
    left_rooms = pick(left_cols, "rooms", "room_count", "bedrooms")

    right_id = pick(right_cols, "id")
    right_price = pick(right_cols, "price")
    right_area = pick(right_cols, "total_area", "area")
    right_rooms = pick(right_cols, "rooms", "room_count", "bedrooms")

    print(f"Источник: {args.source} · "
          f"колонки: {left_id}/{left_price}/{left_area}/{left_rooms}")
    print(f"Redcat:   {right_table} · "
          f"колонки: {right_id}/{right_price}/{right_area}/{right_rooms}")

    left = load_side(left_db, args.source, on_left, args.name,
                     left_id, left_price, left_area, left_rooms)
    right = load_side(right_db, right_table, on_right, args.name,
                      right_id, right_price, right_area, right_rooms)

    print()
    print(f"Источник: {len(left)} лотов")
    print(f"Redcat:   {len(right)} лотов")

    # Индексация.
    left_by_key = {}
    for l in left:
        k = key_for(l)
        left_by_key.setdefault(k, []).append(l)
    right_by_key = {}
    for r in right:
        k = key_for(r)
        right_by_key.setdefault(k, []).append(r)

    all_keys = sorted(set(left_by_key) | set(right_by_key),
                      key=lambda x: (x[0], x[1], x[2]))
    matched = [k for k in all_keys if k in left_by_key and k in right_by_key]
    only_left = [k for k in all_keys if k in left_by_key and k not in right_by_key]
    only_right = [k for k in all_keys if k in right_by_key and k not in left_by_key]

    print(f"  matched:     {len(matched)}")
    print(f"  только у источника: {len(only_left)}")
    print(f"  только у Redcat:    {len(only_right)}")
    print()
    print(f"  Ключ: (комн, площадь, цена_округл_до_1000)")

    def show(key, lrow, rrow, tag):
        l_s = f"{lrow['price']:,.0f}" if lrow and lrow['price'] is not None else "—"
        r_s = f"{rrow['price']:,.0f}" if rrow and rrow['price'] is not None else "—"
        l_a = f"{lrow['area']:.1f}" if lrow and lrow['area'] is not None else "—"
        r_a = f"{rrow['area']:.1f}" if rrow and rrow['area'] is not None else "—"
        l_r = str(lrow['rooms']) if lrow and lrow['rooms'] is not None else "—"
        r_r = str(rrow['rooms']) if rrow and rrow['rooms'] is not None else "—"
        print(f"  [{tag}] "
              f"комн {l_r}/{r_r} · площ {l_a}/{r_a} · "
              f"цена {l_s}/{r_s}".replace(",", " "))

    # Печатаем.
    shown = 0
    print()
    print("── Только у Redcat (right_only) ──")
    for k in only_right[:args.limit]:
        for r in right_by_key[k]:
            show(k, None, r, "RC")
            shown += 1
            if shown > args.limit:
                break
        if shown > args.limit:
            break
    shown = 0
    print()
    print("── Только у источника (left_only) ──")
    for k in only_left[:args.limit]:
        for l in left_by_key[k]:
            show(k, l, None, "SRC")
            shown += 1
            if shown > args.limit:
                break
        if shown > args.limit:
            break

    print()
    print("── Сопоставленные (matched) — первые 30 ──")
    for k in matched[:30]:
        l = left_by_key[k][0]
        r = right_by_key[k][0]
        show(k, l, r, "=")
    return 0


if __name__ == "__main__":
    sys.exit(main())
