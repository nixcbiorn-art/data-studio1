"""
Автоматическое сопоставление названий ЖК между источниками.
============================================================
Читает spec'и в sources/ и sources_external/, находит источники с
cross_check, берёт из них пары (левая таблица, правая таблица), собирает
уникальные названия ЖК с обеих сторон, нормализует их и ищет совпадения.

    python match_complex_names.py               # только отчёт, без записи
    python match_complex_names.py --write       # записать новые пары в файл
    python match_complex_names.py --min-score 0.85

Уже существующие в hc_aliases.json пары не перезаписываются.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from difflib import SequenceMatcher
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import dataops
import hc_aliases
import name_normalizer
import sources as src

EXT_DB = BASE / "reports" / "external_data.db"
RED_DB = BASE / "reports" / "redcat_data.db"
OUT_FILE = BASE / "hc_aliases.json"

def sim(a: str, b: str) -> float:
    return name_normalizer.similarity(a, b)[0]


def db_for(table: str) -> Path:
    try:
        names = {t["name"] for t in dataops.list_tables(EXT_DB)}
    except dataops.DataError:
        names = set()
    return EXT_DB if table in names else RED_DB


def unique_names(table: str, column: str) -> set[str]:
    db = db_for(table)
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            rows = conn.execute(
                f'SELECT DISTINCT "{column}" FROM "{table}" '
                f'WHERE "{column}" IS NOT NULL AND "{column}" != \'\''
            ).fetchall()
    except sqlite3.Error as e:
        print(f"  ⚠️ {table}.{column}: {e}")
        return set()
    return {r[0] for r in rows}


def pairs_from_specs() -> list[tuple]:
    """Все пары (left_table, left_col, right_table, right_col) из cross_check."""
    src.load_from_dir(BASE / "sources")
    src.load_from_dir(BASE / "sources_external")
    out = []
    for key, spec in ((s.key, s) for s in src.all_sources()):
        cc = getattr(spec, "cross_check", None) or {}
        if not cc:
            continue
        left_col = cc.get("on_left")
        right_table = cc.get("with_table")
        right_col = cc.get("on_right")
        if left_col and right_table and right_col:
            out.append((key, left_col, right_table, right_col))
    return out


def load_existing_aliases() -> dict:
    if not OUT_FILE.exists():
        return {}
    try:
        return json.loads(OUT_FILE.read_text(encoding="utf-8")).get("aliases", {})
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="записать новые пары в hc_aliases.json")
    ap.add_argument("--min-score", type=float, default=0.85,
                    help="минимальная схожесть для fuzzy-кандидата (0..1)")
    args = ap.parse_args()

    pairs = pairs_from_specs()
    if not pairs:
        print("Не нашлось ни одной пары с cross_check.")
        return 1

    existing = load_existing_aliases()
    print(f"Пар источников: {len(pairs)}  ·  уже в файле: {len(existing)}\n")

    proposed: dict[str, str] = {}

    for left_t, left_c, right_t, right_c in pairs:
        print(f"{'='*70}")
        print(f"  {left_t}.{left_c}  ↔  {right_t}.{right_c}")
        print(f"{'='*70}")

        left_names = unique_names(left_t, left_c)
        right_names = unique_names(right_t, right_c)
        if not left_names or not right_names:
            print(f"  ⚠️ нет данных: слева {len(left_names)}, справа {len(right_names)}\n")
            continue

        left_norm = {hc_aliases.normalize(n): n for n in left_names}
        right_norm = {hc_aliases.normalize(n): n for n in right_names}

        exact = set(left_norm) & set(right_norm)
        only_left = set(left_norm) - set(right_norm)
        only_right = set(right_norm) - set(left_norm)

        print(f"  Слева: {len(left_names)}  ·  Справа: {len(right_names)}  ·  "
              f"совпадает без алиасов: {len(exact)}")

        fuzzy = []
        for lk in only_left:
            if lk in existing:
                # уже есть правило — проверим, что оно на что-то указывает
                continue
            best, score = None, 0.0
            for rk in only_right:
                s = sim(lk, rk)
                if s > score:
                    best, score = rk, s
            if best and score >= args.min_score:
                fuzzy.append((lk, best, score, left_norm[lk], right_norm[best]))

        fuzzy.sort(key=lambda x: -x[2])

        if fuzzy:
            print(f"  Кандидаты на алиас ({len(fuzzy)}):")
            for lk, rk, score, orig_l, orig_r in fuzzy:
                marker = "✅" if score >= 0.95 else "⚠️ " if score >= args.min_score else "  "
                print(f"    {marker} {int(score*100):>3}%  "
                      f"«{orig_l}»  →  «{orig_r}»")
                proposed[lk] = rk
        else:
            print("  Неоднозначных пар не найдено.")
        print()

    if not proposed:
        print("Нечего записывать — всё уже покрыто или совпадений нет.")
        return 0

    new_aliases = {k: v for k, v in proposed.items() if k not in existing}
    print(f"{'='*70}")
    print(f"  Всего предложено: {len(proposed)}  ·  новых: {len(new_aliases)}")
    print(f"{'='*70}")
    for k, v in new_aliases.items():
        print(f"  {k!r}: {v!r},")

    if not args.write:
        print("\n(запустите с --write, чтобы сохранить)")
        return 0

    merged = dict(existing)
    merged.update(new_aliases)
    OUT_FILE.write_text(json.dumps({
        "_comment": "Соответствия названий ЖК между источниками. "
                    "Ключ и значение нормализованы (см. hc_aliases.normalize).",
        "aliases": merged,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ Записано в {OUT_FILE.name}: +{len(new_aliases)} пар.")
    print("   Перезапустите studio.py, чтобы приложение подхватило изменения.")
    return 0


if __name__ == "__main__":
    sys.exit(main())