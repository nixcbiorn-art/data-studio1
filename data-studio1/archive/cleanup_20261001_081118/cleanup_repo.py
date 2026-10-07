"""
cleanup_repo.py — убирает из корня проекта одноразовые патчи и проверки,
чинит hc_aliases.json.
=========================================================================
Ничего не удаляет: файлы переезжают в archive/ (их можно вернуть).
По умолчанию — dry-run: показывает план и ничего не трогает.

    python cleanup_repo.py            # показать план
    python cleanup_repo.py --apply    # выполнить

Что остаётся в корне (их вызывают боевые модули):
  diagnose.py, check_env.py, selftest_readonly.py, export_problem_jc.py,
  source_stats.py, jc_compare.py, scan_sources.py, fill_aliases.py,
  match_complex_names.py, history_dashboard.py, completeness_dashboard.py, ...
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

HERE = Path(__file__).resolve().parent
ARCHIVE = HERE / "archive"

# ── что куда переезжает ───────────────────────────────────────
PATCHES = [  # одноразовые патчи и проверки «применился ли патч»
    "apply_patch_1.py", "apply_patch_10.py", "apply_patch_tg_bot.py",
    "fix_all.py", "find_discount.py",          # find_discount.py на деле fix_lsr_filter
    "check_system.py", "check_preflight.py", "show_preflight.py",
]
PROBES = [  # разовые пробы API и разборы конкретного сбора
    "probe_filter.py", "probe_a101_filter.py", "probe_skygarden.py",
    "probe_fsk_dsk.py", "collect_slugs.py", "show_fields.py",
    "diag.py", "diag_split.py", "diag_urls.py",
    "diagnose_sources.py", "check_all.py",
    "check_dsk.bat", "crosscheck_ids.py",      # crosscheck_ids нужен только check_dsk.bat
]
GLOBS = {  # шаблоны → подпапка
    "apply_patch_*.py": "patches",
    "*.bak": "backups", "*.bak_*": "backups", "*.bak_fix": "backups",
}
TARGETS = {"patches": PATCHES, "probes": PROBES}

CLEAN_ALIASES = {
    "_comment": ("Соответствия названий ЖК между источниками. Ключ и значение "
                 "нормализованы (name_normalizer.normalize). Заполнить "
                 "автоматически: python match_complex_names.py --write"),
    "aliases": {},
}


def plan_moves() -> list[tuple[Path, Path]]:
    moves: dict[Path, Path] = {}
    for sub, names in TARGETS.items():
        for name in names:
            p = HERE / name
            if p.is_file():
                moves[p] = ARCHIVE / sub / name
    for pattern, sub in GLOBS.items():
        for p in HERE.glob(pattern):
            if p.is_file() and p not in moves and p.name != "cleanup_repo.py":
                moves[p] = ARCHIVE / sub / p.name
    return sorted(moves.items(), key=lambda kv: kv[0].name)


def safe_dest(dst: Path) -> Path:
    if not dst.exists():
        return dst
    stamp = datetime.now().strftime("%H%M%S")
    return dst.with_name(f"{dst.stem}.{stamp}{dst.suffix}")


def check_dependencies(moves) -> list[str]:
    """Не импортирует ли оставшийся код то, что мы увозим."""
    moving = {src.stem for src, _ in moves if src.suffix == ".py"}
    moving_paths = {src for src, _ in moves}
    warns = []
    pat = re.compile(r"^\s*(?:import|from)\s+(\w+)", re.M)
    for py in HERE.glob("*.py"):
        if py in moving_paths or py.name == "cleanup_repo.py":
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for mod in set(pat.findall(text)):
            if mod in moving:
                warns.append(f"{py.name} импортирует {mod}.py")
    return warns


# ── hc_aliases.json ───────────────────────────────────────────
def _valid_aliases(path: Path):
    """Возвращает dict алиасов, если файл — корректный JSON, иначе None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("aliases"), dict):
        return None
    return data


def fix_aliases(apply: bool) -> None:
    print("\n── hc_aliases.json ──")
    path = HERE / "hc_aliases.json"
    if not path.exists():
        print("  файла нет — создаю пустой словарь")
        if apply:
            path.write_text(json.dumps(CLEAN_ALIASES, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return

    data = _valid_aliases(path)
    if data is not None:
        print(f"  ✅ файл корректный, пар: {len(data['aliases'])} — не трогаю")
        return

    head = path.read_text(encoding="utf-8", errors="replace").lstrip()[:60].replace("\n", " ")
    print(f"  ❌ не JSON (начинается с: {head!r})")

    # ищем живую копию среди бэкапов
    candidates = sorted(list(HERE.glob("hc_aliases.json.*"))
                        + list((ARCHIVE / "backups").glob("hc_aliases.json*")),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    restored = None
    for c in candidates:
        d = _valid_aliases(c)
        if d and d["aliases"]:
            restored = (c, d)
            break

    broken_copy = ARCHIVE / "backups" / f"hc_aliases.json.broken_{datetime.now():%Y%m%d_%H%M%S}"
    if restored:
        c, d = restored
        print(f"  ♻️  найден рабочий бэкап {c.name}: пар {len(d['aliases'])} — восстановлю из него")
        new_data = d
    else:
        print("  ℹ️  рабочих бэкапов нет — будет пустой словарь")
        print("     Заполнить заново: python match_complex_names.py --write")
        new_data = CLEAN_ALIASES

    if apply:
        broken_copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(broken_copy))
        path.write_text(json.dumps(new_data, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        print(f"  ✅ исправлено; битый файл сохранён: archive/backups/{broken_copy.name}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Очистка корня проекта от патчей")
    ap.add_argument("--apply", action="store_true", help="выполнить (иначе dry-run)")
    args = ap.parse_args()

    moves = plan_moves()
    print("=" * 66)
    print(f"  {'ВЫПОЛНЕНИЕ' if args.apply else 'DRY-RUN (ничего не меняется)'}")
    print("=" * 66)

    if not moves:
        print("  Нечего переносить — корень уже чистый.")
    else:
        print(f"  В archive/ переедет файлов: {len(moves)}\n")
        for src, dst in moves:
            print(f"    {src.name:<34} → {dst.parent.relative_to(HERE)}/")

        warns = check_dependencies(moves)
        if warns:
            print("\n  ⚠️  Остающийся код зависит от переносимых файлов:")
            for w in warns:
                print(f"     • {w}")
            print("     Перенос отменён, пока зависимости не устранены.")
            return 1

        if args.apply:
            for src, dst in moves:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(safe_dest(dst)))
            print(f"\n  ✅ перенесено: {len(moves)}")

    fix_aliases(args.apply)

    if not args.apply:
        print("\nДля выполнения: python cleanup_repo.py --apply")
    else:
        print("\nДальше: перезапустите studio.py — в шапке должно быть "
              "«Синонимов ЖК: N» (если словарь не пуст).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
