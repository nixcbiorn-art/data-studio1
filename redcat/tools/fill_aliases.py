"""
Заполняет hc_aliases.json парами «название_в_FSK» → «название_в_Redcat».

Ничего не меняет в скрапере и в базах. Только читает и пишет JSON-словарь,
который потом использует webapp.py при сверке.

Запуск:
    python -m redcat.tools.fill_aliases
    python -m redcat.tools.fill_aliases --filter "developer_name LIKE '%ФСК%'"
    python -m redcat.tools.fill_aliases --min-score 0.7 --dry-run
"""

from __future__ import annotations

from redcat.core import paths
import argparse
import json
import re
import sqlite3
import sys
from difflib import SequenceMatcher
from pathlib import Path

BASE = paths.ROOT
sys.path.insert(0, str(BASE))

from redcat.sources import name_normalizer
EXT_DB = BASE / "reports" / "external_data.db"
RED_DB = BASE / "reports" / "redcat_data.db"
OUT = paths.CONFIG_DIR / "hc_aliases.json"


def norm(s):
    """Единая нормализация — та же, что использует webapp (name_normalizer)."""
    return name_normalizer.normalize(s) or ""


# Простая транслитерация — чтобы «амбер сити» нашло «amber city»
_TRANS = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def translit(s):
    return "".join(_TRANS.get(ch, ch) for ch in s)


def sim(a, b):
    if not a or not b:
        return 0.0
    return name_normalizer.similarity(a, b)[0]


def names(db, table, col, where=""):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sql = (f'SELECT "{col}" FROM "{table}" '
               f'WHERE "{col}" IS NOT NULL AND "{col}" <> \'\'')
        if where:
            sql += f" AND ({where})"
        sql += f' GROUP BY "{col}" ORDER BY COUNT(*) DESC'
        return [r[0] for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def best_match(left, right_names):
    ln = norm(left)
    if not ln:
        return None, 0.0, ""

    # прямое совпадение
    for r in right_names:
        if norm(r) == ln:
            return r, 1.0, "norm"

    # фонетика (кириллица ↔ латиница)
    lp = name_normalizer.phon_key(ln)
    for r in right_names:
        if name_normalizer.phon_key(r) == lp:
            return r, 0.97, "phon"

    # fuzzy
    best, score = None, 0.0
    for r in right_names:
        s = sim(ln, norm(r))
        if s > score:
            best, score = r, s
    return best, score, "fuzzy"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--left-table", default="fsk_apartments")
    ap.add_argument("--left-col", default="complex_name")
    ap.add_argument("--right-table", default="apartments")
    ap.add_argument("--right-col", default="housing_complex_name")
    ap.add_argument("--filter", default="",
                    help="SQL-фильтр к правой таблице, например "
                         "\"developer_name LIKE '%%ФСК%%'\"")
    ap.add_argument("--min-score", type=float, default=0.7,
                    help="минимальная схожесть, чтобы записать пару (0..1)")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать, но не записывать в файл")
    args = ap.parse_args()

    if not EXT_DB.exists():
        print(f"❌ Нет базы: {EXT_DB}")
        return 1
    if not RED_DB.exists():
        print(f"❌ Нет базы: {RED_DB}")
        return 1

    print(f"📖 Левая:  {args.left_table}.{args.left_col}")
    left = names(EXT_DB, args.left_table, args.left_col)
    print(f"          {len(left)} уникальных названий")

    print(f"📖 Правая: {args.right_table}.{args.right_col}"
          + (f"  (фильтр: {args.filter})" if args.filter else ""))
    right = names(RED_DB, args.right_table, args.right_col, args.filter)
    print(f"          {len(right)} уникальных названий\n")

    # читаем существующие пары, чтобы не затирать ручные правки
    existing = {}
    if OUT.exists():
        try:
            existing = json.loads(OUT.read_text(encoding="utf-8")).get("aliases", {})
        except (json.JSONDecodeError, OSError):
            pass
    if existing:
        print(f"💾 Существующих пар: {len(existing)} — сохраняются\n")

    aliases = dict(existing)
    added = 0
    print(f"{'левый':<30}  {'правый':<30}  {'%':>4}  метод")
    print("-" * 82)
    for lname in left:
        lk = norm(lname)
        if not lk:
            continue
        rname, score, method = best_match(lname, right)
        if not rname or score < args.min_score:
            continue
        rk = norm(rname)
        if lk == rk:
            continue  # уже совпадают без синонима
        # не перезаписываем ручную правку
        if lk in existing:
            continue
        aliases[lk] = rk
        added += 1
        print(f"{lname:<30.30}  {rname:<30.30}  {int(score * 100):>3}%  {method}")

    print(f"\n📊 Добавлено пар: {added}. Всего в файле: {len(aliases)}.")

    if args.dry_run:
        print("(dry-run — файл не изменён)")
        return 0

    OUT.write_text(json.dumps({
        "_comment": "Словарь соответствий названий ЖК. Ключ — как пишет "
                    "внешний источник, значение — как пишет Redcat. "
                    "Оба названия в нижнем регистре, без «ЖК», ё→е.",
        "aliases": aliases,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ Записано: {OUT.name}")
    print("   Теперь перезапустите сервер studio.py — словарь подхватится.")
    return 0


if __name__ == "__main__":
    sys.exit(main())