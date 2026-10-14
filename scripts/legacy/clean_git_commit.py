#!/usr/bin/env python3
"""
stage2a_packages.py — Этап 2a: первые модули в пакеты.
========================================================
Переносит 5 чистых модулей в redcat/ и переписывает импорты.
Логика не меняется.

Что переносится:
    dataops.py           → redcat/data/dataops.py
    api_guard.py         → redcat/core/api_guard.py
    rate_limit.py        → redcat/collection/rate_limit.py
    thresholds.py        → redcat/data/thresholds.py
    name_normalizer.py   → redcat/sources/name_normalizer.py

Запуск:
    python stage2a_packages.py            # dry-run
    python stage2a_packages.py --apply    # применить
"""
from __future__ import annotations

from redcat.core import paths
import argparse
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

HERE = paths.ROOT

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass


PACKAGE_DIRS = [
    "redcat", "redcat/core", "redcat/data",
    "redcat/collection", "redcat/sources",
]

INIT_CONTENT = '"""Пакет. Содержимое — в подмодулях."""\n'

MOVES = {
    "dataops.py":         "redcat/data/dataops.py",
    "api_guard.py":       "redcat/core/api_guard.py",
    "rate_limit.py":      "redcat/collection/rate_limit.py",
    "thresholds.py":      "redcat/data/thresholds.py",
    "name_normalizer.py": "redcat/sources/name_normalizer.py",
}


# Порядок: сначала «с as», потом без; сначала import, потом from.
IMPORT_RULES = [
    (re.compile(r'^(\s*)import dataops\s+as\s+(\w+)\s*$'),
     r'\1from redcat.data import dataops as \2'),
    (re.compile(r'^(\s*)import dataops\s*$'),
     r'\1from redcat.data import dataops'),
    (re.compile(r'^(\s*)from dataops import (.+)$'),
     r'\1from redcat.data.dataops import \2'),

    (re.compile(r'^(\s*)import api_guard\s+as\s+(\w+)\s*$'),
     r'\1from redcat.core import api_guard as \2'),
    (re.compile(r'^(\s*)import api_guard\s*$'),
     r'\1from redcat.core import api_guard'),
    (re.compile(r'^(\s*)from api_guard import (.+)$'),
     r'\1from redcat.core.api_guard import \2'),

    (re.compile(r'^(\s*)import rate_limit\s+as\s+(\w+)\s*$'),
     r'\1from redcat.collection import rate_limit as \2'),
    (re.compile(r'^(\s*)import rate_limit\s*$'),
     r'\1from redcat.collection import rate_limit'),
    (re.compile(r'^(\s*)from rate_limit import (.+)$'),
     r'\1from redcat.collection.rate_limit import \2'),

    (re.compile(r'^(\s*)import thresholds\s+as\s+(\w+)\s*$'),
     r'\1from redcat.data import thresholds as \2'),
    (re.compile(r'^(\s*)import thresholds\s*$'),
     r'\1from redcat.data import thresholds'),
    (re.compile(r'^(\s*)from thresholds import (.+)$'),
     r'\1from redcat.data.thresholds import \2'),

    (re.compile(r'^(\s*)import name_normalizer\s+as\s+(\w+)\s*$'),
     r'\1from redcat.sources import name_normalizer as \2'),
    (re.compile(r'^(\s*)import name_normalizer\s*$'),
     r'\1from redcat.sources import name_normalizer'),
    (re.compile(r'^(\s*)from name_normalizer import (.+)$'),
     r'\1from redcat.sources.name_normalizer import \2'),
]


def _read(path: Path) -> tuple[str, str]:
    raw = path.read_bytes().decode("utf-8")
    style = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), style


def _write(path: Path, content: str, style: str) -> None:
    path.write_bytes(content.replace("\n", style).encode("utf-8"))


def _backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    bak = path.with_name(path.name + f".bak_2a_{stamp}")
    shutil.copy2(path, bak)
    return bak


def _all_py_files() -> list[Path]:
    me = Path(__file__).resolve()
    out = []
    for p in HERE.rglob("*.py"):
        if p == me or "__pycache__" in p.parts:
            continue
        rel = p.relative_to(HERE)
        if rel.parts and rel.parts[0] == "redcat":
            continue
        out.append(p)
    return sorted(out)


def _rewrite(content: str) -> tuple[str, list[str]]:
    lines = content.split("\n")
    new_lines = []
    matched = []
    for line in lines:
        new_line = line
        for pat, repl in IMPORT_RULES:
            if pat.match(line):
                candidate = pat.sub(repl, line)
                if candidate != line:
                    matched.append(f"{line.strip()}  →  {candidate.strip()}")
                    new_line = candidate
                break
        new_lines.append(new_line)
    return "\n".join(new_lines), matched


def create_packages(apply: bool) -> list[str]:
    created = []
    for d in PACKAGE_DIRS:
        path = HERE / d
        if not path.exists():
            if apply:
                path.mkdir(parents=True, exist_ok=True)
            created.append(d)
    for d in PACKAGE_DIRS:
        init = HERE / d / "__init__.py"
        if not init.exists():
            if apply:
                init.write_text(INIT_CONTENT, encoding="utf-8")
            created.append(f"{d}/__init__.py")
    return created


def move_modules(apply: bool) -> list[tuple[str, str, str]]:
    results = []
    for src_name, dst_rel in MOVES.items():
        src, dst = HERE / src_name, HERE / dst_rel
        if not src.exists():
            results.append((src_name, dst_rel,
                            "(already)" if dst.exists() else "(NOT FOUND)"))
            continue
        if dst.exists():
            results.append((src_name, dst_rel, "(dst exists)"))
            continue
        bak_name = ""
        if apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            bak = _backup(src)
            bak_name = bak.name
            shutil.move(str(src), str(dst))
        results.append((src_name, dst_rel, bak_name))
    return results


def rewrite_imports(apply: bool) -> list[dict]:
    reports = []
    for path in _all_py_files():
        content, style = _read(path)
        new_content, matched = _rewrite(content)
        if not matched:
            continue
        report = {"file": str(path.relative_to(HERE)), "changes": matched}
        if apply:
            bak = _backup(path)
            _write(path, new_content, style)
            report["backup"] = bak.name
        reports.append(report)
    return reports


def clear_pycache(apply: bool) -> int:
    count = 0
    for pc in HERE.rglob("__pycache__"):
        if "redcat" in pc.parts:
            continue
        if apply:
            shutil.rmtree(pc, ignore_errors=True)
        count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="применить (по умолчанию dry-run)")
    args = ap.parse_args()
    apply = args.apply

    print("=" * 78)
    print("  STAGE 2A: FIRST MODULES TO PACKAGES")
    print("=" * 78)
    print(f"  Каталог: {HERE}")
    print(f"  Режим:   {'ПРИМЕНЕНИЕ' if apply else 'dry-run'}")
    print()

    print("▸ 1. Структура")
    created = create_packages(apply)
    for c in created:
        print(f"    + {c}")
    if not created:
        print("    (уже существует)")
    print()

    print("▸ 2. Перенос модулей")
    for src, dst, bak in move_modules(apply):
        if bak == "(already)":
            print(f"    ↻ {src:<22} → {dst}  (уже)")
        elif bak == "(NOT FOUND)":
            print(f"    ⚠️  {src:<22} не найден")
        elif bak.startswith("("):
            print(f"    ⚠️  {src:<22} {bak}")
        else:
            print(f"    ✅ {src:<22} → {dst}"
                  + (f"  (бэкап: {bak})" if bak else ""))
    print()

    print("▸ 3. Переписывание импортов")
    reports = rewrite_imports(apply)
    if not reports:
        print("    (нечего менять)")
    for r in reports:
        print(f"    • {r['file']}  ({len(r['changes'])} строк)")
        for ch in r["changes"]:
            print(f"        {ch}")
    print()

    print("▸ 4. Очистка __pycache__ в корне")
    n = clear_pycache(apply)
    print(f"    {'удалено' if apply else 'будет удалено'}: {n}")
    print()

    print("=" * 78)
    print("  ПРИМЕНЕНО" if apply else "  DRY-RUN — файлы не менялись")
    print("=" * 78)

    if apply:
        print()
        print("  Проверка:")
        print("     python -m py_compile webapp.py redcat_scraper.py")
        print("     python -c \"from redcat.data import dataops\"")
        print("     python -c \"from redcat.core import api_guard\"")
        print("     python -c \"from redcat.collection import rate_limit\"")
        print("     python -c \"from redcat.data import thresholds\"")
        print("     python -c \"from redcat.sources import name_normalizer\"")
        print("     python -c \"import webapp\"")
        print("     python -c \"import redcat_scraper\"")
        print("     python redcat_scraper.py --list-sources")
        print("     python studio.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())