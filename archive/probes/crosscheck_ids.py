"""
Сравнение состава лотов между внешним источником и Redcat.
============================================================
Показывает, какие лоты есть только в FSK, только в Redcat и общие.

Использование:
    # Простой случай — одинаковые имена ЖК и одинаковый ключ лота:
    python crosscheck_ids.py --ext-table fsk_apartments --complex "Скай"

    # Разные имена ЖК (FSK: «Скай», Redcat: «Sky»):
    python crosscheck_ids.py --ext-table fsk_apartments \\
        --complex "Скай" --complex-right "Sky"

    # Разные ключи лота — сопоставляем по совокупности полей:
    python crosscheck_ids.py --ext-table fsk_apartments \\
        --complex "Скай" --complex-right "Sky" \\
        --key "area_total=total_area,floor=floor,price=price"
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
EXT_DB = BASE / "reports" / "external_data.db"
RED_DB = BASE / "reports" / "redcat_data.db"


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # SQLite-функция lower() опускает только ASCII, для кириллицы бесполезна.
    # Подменяем её на Python-овскую — она умеет Unicode.
    conn.create_function(
        "lower", 1,
        lambda s: s.lower() if isinstance(s, str) else s)
    return conn


def _complex_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Колонки таблицы, в которых может лежать название ЖК."""
    cols = [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    known = ("complex_name", "housing_complex_name", "complex", "zhk",
             "zhk_name", "complex_title", "project_name")
    candidates = [c for c in cols if c in known]
    if not candidates:
        candidates = [c for c in cols if "complex" in c.lower() or "zhk" in c.lower()]
    if not candidates:
        raise SystemExit(
            f'В таблице "{table}" не найдена колонка с названием ЖК. '
            f'Доступные колонки: {", ".join(cols)}')
    return candidates


def load(conn: sqlite3.Connection, table: str, complex_like: str, where: str = ""):
    """Возвращает список строк лотов по этому ЖК.

    LOWER() с обеих сторон — обходит то, что SQLite LIKE регистрозависим
    для кириллицы (COLLATE NOCASE для неё не работает вовсе).
    """
    cols = _complex_columns(conn, table)
    like_clause = " OR ".join(f'LOWER("{c}") LIKE ?' for c in cols)
    sql = f'SELECT * FROM "{table}" WHERE ({like_clause})'
    args = [f"%{complex_like.lower()}%"] * len(cols)
    if where:
        sql += f" AND ({where})"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _parse_key(spec: str) -> list[tuple[str, str]]:
    """Разбирает --key.

    'number'                                → [('number', 'number')]
    'area_total=total_area,floor=floor'     → [('area_total','total_area'),
                                               ('floor','floor')]
    """
    pairs: list[tuple[str, str]] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            left, right = part.split("=", 1)
            pairs.append((left.strip(), right.strip()))
        else:
            pairs.append((part, part))
    if not pairs:
        raise SystemExit("Пустой --key — укажите хотя бы одно поле.")
    return pairs


def _norm(v):
    """Приводит значение ключа к сравнимому виду.

    Главное: 17624779 (int в одной базе) и 17624779.0 (float в другой)
    должны дать одинаковую строку. Иначе совпадения теряются.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return str(int(v)) if float(v).is_integer() else str(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        f = float(s.replace(",", "."))
        return str(int(f)) if f.is_integer() else str(f)
    except ValueError:
        return s


def _index(rows, pairs, side: int):
    """side=0 → левая колонка пары, side=1 → правая."""
    idx: dict[tuple, dict] = {}
    skipped = 0
    collisions = 0
    for r in rows:
        vals = [_norm(r.get(p[side])) for p in pairs]
        if any(v is None for v in vals):
            skipped += 1
            continue
        key = tuple(vals)
        if key in idx:
            collisions += 1   # ключ не уникален — оставляем первый
        else:
            idx[key] = r
    return idx, skipped, collisions


def show_rows(rows, title, limit, fields):
    print(f"\n── {title} ({len(rows)}) ────────────────────────────")
    if not rows:
        return
    for r in rows[:limit]:
        parts = []
        for f in fields:
            v = r.get(f)
            if v in (None, ""):
                continue
            parts.append(f"{f}={v}")
        print("   " + "  ".join(parts))
    if len(rows) > limit:
        print(f"   … ещё {len(rows) - limit}")


def main():
    ap = argparse.ArgumentParser(
        description="Сравнение состава лотов между внешним источником и Redcat.")
    ap.add_argument("--ext-table", default="fsk_apartments")
    ap.add_argument("--complex", required=True,
                    help="часть названия ЖК на левой стороне (внешний источник)")
    ap.add_argument("--complex-right", default=None,
                    help="часть названия ЖК на правой стороне (Redcat). "
                         "Если не задано — используется --complex.")
    ap.add_argument("--key", default="number",
                    help="Поле-ключ лота. Одно имя — используется с обеих сторон. "
                         "Или список пар 'левая=правая' через запятую, например: "
                         "\"area_total=total_area,floor=floor,price=price\"")
    ap.add_argument("--show-only-left", type=int, default=15)
    ap.add_argument("--show-only-right", type=int, default=15)
    ap.add_argument("--show-shared", type=int, default=10)
    ap.add_argument("--filter-left", default="")
    ap.add_argument("--filter-right", default="")
    args = ap.parse_args()

    complex_right = args.complex_right or args.complex
    key_pairs = _parse_key(args.key)

    with open_ro(EXT_DB) as ext, open_ro(RED_DB) as red:
        left = load(ext, args.ext_table, args.complex, args.filter_left)
        right = load(red, "apartments", complex_right, args.filter_right)

    if not left:
        print(f"⚠️  Слева 0 лотов по фильтру «{args.complex}» в {args.ext_table} — "
              f"проверьте написание.")
    if not right:
        print(f"⚠️  Справа 0 лотов по фильтру «{complex_right}» в apartments — "
              f"проверьте написание (возможно, нужно другое — латиница/кириллица).")

    left_index, left_skipped, left_coll = _index(left, key_pairs, 0)
    right_index, right_skipped, right_coll = _index(right, key_pairs, 1)

    only_left = {k: v for k, v in left_index.items() if k not in right_index}
    only_right = {k: v for k, v in right_index.items() if k not in left_index}
    shared = {k: v for k, v in left_index.items() if k in right_index}

    key_desc = ", ".join(f"{l}={r}" if l != r else l for l, r in key_pairs)

    print(f"══════════════════════════════════════════════════════════")
    print(f"  ЖК: слева «{args.complex}» · справа «{complex_right}»")
    print(f"  Ключ сопоставления: {key_desc}")
    print(f"  Левый ({args.ext_table}): {len(left)} лотов"
          + (f"  [{args.filter_left}]" if args.filter_left else ""))
    print(f"  Правый (apartments):      {len(right)} лотов"
          + (f"  [{args.filter_right}]" if args.filter_right else ""))
    print(f"══════════════════════════════════════════════════════════")
    if left_skipped or right_skipped:
        print(f"  Пропущено из-за пустых полей ключа: "
              f"слева {left_skipped}, справа {right_skipped}")
    if left_coll or right_coll:
        print(f"  ⚠️  Ключ не уникален: слева дублей {left_coll}, "
              f"справа {right_coll}. Возможны ложные совпадения — добавьте "
              f"полей в --key (например, ещё и number/id).")
    print(f"  Общих:           {len(shared)}")
    print(f"  Только слева:    {len(only_left)}")
    print(f"  Только справа:   {len(only_right)}")

    left_fields = ["number", "crm_type", "area_total", "price", "floor",
                   "labels", "delivery_date"]
    right_fields = ["id", "rooms", "total_area", "price", "floor",
                    "status", "object_type.name"]

    show_rows(list(only_left.values()),
              f"Только слева ({args.ext_table})",
              args.show_only_left, left_fields)
    show_rows(list(only_right.values()),
              "Только справа (Redcat)",
              args.show_only_right, right_fields)

    if args.show_shared and shared:
        pairs_show = []
        for k in list(shared)[:args.show_shared]:
            pairs_show.append((k, shared[k], right_index[k]))

        print(f"\n── Общие лоты (первые {len(pairs_show)}) ─────────────────")
        print(f"  {'ключ':<30} {'площ.FSK':>10} {'площ.RC':>10} "
              f"{'цена.FSK':>14} {'цена.RC':>14}")
        for k, l, r in pairs_show:
            key_str = " | ".join(str(x) for x in k)
            print(f"  {key_str:<30.30} "
                  f"{l.get('area_total')!s:>10} "
                  f"{r.get('total_area')!s:>10} "
                  f"{l.get('price')!s:>14} "
                  f"{r.get('price')!s:>14}")


if __name__ == "__main__":
    sys.exit(main())