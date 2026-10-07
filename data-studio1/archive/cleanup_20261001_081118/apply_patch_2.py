"""
cleanup_before_git.py — подготовка проекта к отправке в git.

Что делает:
  1. Создаёт .gitignore (если нет).
  2. Переносит временные скрипты и бэкапы в archive/cleanup_<timestamp>/.
  3. Проверяет на секреты (JWT, токены, пароли) в отслеживаемых файлах.
  4. Показывает, что попадёт в git.

Ничего не удаляет — только переносит в archive/.

Запуск:
    python cleanup_before_git.py
    python cleanup_before_git.py --dry-run
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

HERE = Path(__file__).resolve().parent
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
ARC = HERE / "archive" / f"cleanup_{STAMP}"


# ══════════════════════════════════════════════════════════════
#  1. .gitignore
# ══════════════════════════════════════════════════════════════
GITIGNORE = """# ─── СЕКРЕТЫ ───
.env
.env.local
.env.*.local
*.pem
*.key

# ─── ДАННЫЕ ───
reports/
history/
browser_profiles/
archive/
studio.db
studio.db-shm
studio.db-wal
*.db
*.db-shm
*.db-wal
*.sqlite
*.sqlite3

# ─── TELEGRAM-БОТ ───
tg_bot_state.json
tg_run_state.json
tg_run_settings.json
bot_users.json
bot_pending.json
bot_invites.json
tg_run.log

# ─── ЛОГИ ───
*.log
redcat_scraper.log
run_log.txt

# ─── БЭКАПЫ ПАТЧЕЙ ───
*.bak
*.bak_*

# ─── ВРЕМЕННЫЕ СКРИПТЫ ПАТЧЕЙ ───
apply_patch_*.py
cleanup_repo.py
cleanup_before_git.py

# ─── РАЗОВЫЕ ПРОБЫ ───
probe_*.py
check_*.py
debug_*.py
where_is_*.py
fix_*.py
install_*.py
add_*.py
show_*.py
find_*.py
selftest_all.py

# ─── PYCACHE ───
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
dist/
*.egg-info/
.venv/
venv/
ENV/

# ─── РЕДАКТОРЫ / ОС ───
.vscode/
.idea/
*.swp
*.swo
*~
.DS_Store
Thumbs.db
desktop.ini

# ─── ОТЧЁТЫ ───
crosscheck_hc_*.csv
problem_jc_*.csv
problem_jc_*.md
*_discounts.csv
verification_report.md
"""


# ══════════════════════════════════════════════════════════════
#  Временные файлы для переноса
# ══════════════════════════════════════════════════════════════
TEMP_SCRIPTS = [
    "apply_patch_1.py", "apply_patch_2.py", "apply_patch_10.py",
    "apply_patch_tg_bot.py", "apply_patch_security.py",
    "apply_patch_headers.py", "apply_patch_diagnose_headers.py",
    "cleanup_repo.py",
    "check_headers_live.py", "debug_fetchers.py", "check_patch_places.py",
    "where_is_osnova.py", "find_mr_spec.py", "show_external_new.py",
    "show_mr_state.py", "add_mr_projects_and_object.py",
    "fix_external_new.py", "fix_mr_specs.py", "fix_osnova_types.py",
    "fix_osnova_limits.py", "install_osnova.py",
    "probe_mr.py", "probe_mr_list.py", "probe_limit.py",
    "probe_osnova.py", "probe_osnova_2.py", "selftest_all.py",
    "check_env.py",
]


# ══════════════════════════════════════════════════════════════
#  Паттерны секретов
# ══════════════════════════════════════════════════════════════
SECRET_PATTERNS = [
    (r"REDCAT_TOKEN\s*=\s*['\"]?eyJ[\w\-\.]{20,}", "JWT Redcat"),
    (r"TELEGRAM_BOT_TOKEN\s*=\s*\d+:AA[\w\-]{20,}", "Telegram bot"),
    (r"Authorization:\s*Bearer\s+eyJ[\w\-\.]{40,}", "JWT в коде"),
    (r"api[_-]?key['\"]?\s*[:=]\s*['\"][\w]{20,}", "API ключ"),
    (r"password['\"]?\s*[:=]\s*['\"][^'\"]{6,}", "пароль"),
    (r"secret['\"]?\s*[:=]\s*['\"][^'\"]{10,}", "секрет"),
]

SKIP_DIRS = {"reports", "history", "browser_profiles", "archive",
             "__pycache__", ".git", ".venv", "venv", "node_modules"}
SKIP_EXT = {".db", ".parquet", ".pyc", ".log", ".bak"}


# ══════════════════════════════════════════════════════════════
def step1_gitignore(dry: bool) -> bool:
    print("\n" + "=" * 78)
    print("  [1] .gitignore")
    print("=" * 78)
    gi = HERE / ".gitignore"
    if gi.exists():
        text = gi.read_text(encoding="utf-8", errors="replace")
        if ".env" in text and "reports/" in text:
            print(f"  ℹ️  .gitignore уже есть ({gi.stat().st_size} Б)")
            return True
        print(f"  ⚠️  .gitignore есть, но без ключевых правил")
    if dry:
        print(f"  🔎 будет записан .gitignore ({len(GITIGNORE)} символов)")
        return True
    gi.write_text(GITIGNORE, encoding="utf-8")
    print(f"  ✅ .gitignore записан ({len(GITIGNORE)} символов)")
    return True


# ══════════════════════════════════════════════════════════════
def step2_cleanup(dry: bool) -> int:
    print("\n" + "=" * 78)
    print("  [2] Перенос временных файлов в archive/")
    print("=" * 78)

    to_move = []

    # Явный список
    for name in TEMP_SCRIPTS:
        p = HERE / name
        if p.is_file():
            to_move.append(p)

    # Все *.bak и *.bak_*
    for p in HERE.rglob("*.bak"):
        if "archive" not in p.parts and p.is_file():
            to_move.append(p)
    for p in HERE.rglob("*.bak_*"):
        if "archive" not in p.parts and p.is_file():
            to_move.append(p)

    if not to_move:
        print("  ℹ️  нечего переносить")
        return 0

    # Дубликаты
    seen = set()
    uniq = []
    for p in to_move:
        rp = str(p.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        uniq.append(p)

    print(f"  Найдено {len(uniq)} файлов:")
    for p in uniq[:20]:
        print(f"    {p.relative_to(HERE)}")
    if len(uniq) > 20:
        print(f"    … и ещё {len(uniq) - 20}")

    if dry:
        print(f"\n  🔎 dry-run: перенеслось бы {len(uniq)} файлов в {ARC.name}")
        return len(uniq)

    ARC.mkdir(parents=True, exist_ok=True)
    moved = 0
    for p in uniq:
        try:
            rel = p.relative_to(HERE)
            dst = ARC / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dst))
            moved += 1
        except Exception as e:
            print(f"    ⚠️  {p.name}: {e}")

    print(f"\n  ✅ Перенесено: {moved}")
    print(f"     Куда: {ARC.relative_to(HERE)}/")
    print(f"     (ничего не удалено — можно вернуть)")
    return moved


# ══════════════════════════════════════════════════════════════
def step3_secrets() -> list:
    print("\n" + "=" * 78)
    print("  [3] Проверка на секреты")
    print("=" * 78)

    problems = []
    checked = 0

    for p in HERE.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in SKIP_EXT or ".bak" in p.name:
            continue
        if p.name == ".env":
            problems.append((p, "файл .env — не должен коммититься"))
            continue
        if p.stat().st_size > 500_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        checked += 1
        for rx, label in SECRET_PATTERNS:
            for m in re.finditer(rx, text):
                frag = m.group(0)[:70].replace("\n", " ")
                problems.append((p, f"{label}: {frag}"))

    print(f"  Проверено файлов: {checked}")
    if problems:
        print(f"\n  ⚠️  Найдено {len(problems)} подозрительных мест:")
        seen = set()
        for p, why in problems:
            key = (str(p.relative_to(HERE)), why[:40])
            if key in seen:
                continue
            seen.add(key)
            print(f"    {p.relative_to(HERE)}")
            print(f"      {why}")
    else:
        print("\n  ✅ Секретов не найдено")
    return problems


# ══════════════════════════════════════════════════════════════
def step4_env_check() -> bool:
    print("\n" + "=" * 78)
    print("  [4] .env")
    print("=" * 78)
    env = HERE / ".env"
    gi = HERE / ".gitignore"

    if not env.exists():
        print("  (нет .env)")
        return True

    print(f"  .env существует ({env.stat().st_size} Б)")

    if gi.exists():
        text = gi.read_text(encoding="utf-8", errors="replace")
        if ".env" in text:
            print("  ✅ .env в .gitignore")
            return True
        print("  ❌ .env НЕ в .gitignore — утечёт токен!")
        return False
    print("  ❌ .gitignore нет")
    return False


# ══════════════════════════════════════════════════════════════
def step5_git_status() -> None:
    print("\n" + "=" * 78)
    print("  [5] Что попадёт в git")
    print("=" * 78)

    try:
        r = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(HERE), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30)
        if r.returncode != 0:
            print(f"  ⚠️  git status: код {r.returncode}")
            if r.stderr:
                print(f"  {r.stderr[:400]}")
            return
        lines = [l for l in r.stdout.splitlines() if l.strip()]
        if not lines:
            print("  (git не инициализирован или всё чисто)")
            return
        print(f"  Всего: {len(lines)} файлов/папок\n")
        for line in lines[:50]:
            print(f"  {line}")
        if len(lines) > 50:
            print(f"  … и ещё {len(lines) - 50}")
    except FileNotFoundError:
        print("  ⚠️  git не найден в PATH")
    except Exception as e:
        print(f"  ⚠️  {type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════════════
def step6_final_list() -> None:
    print("\n" + "=" * 78)
    print("  [6] Файлы, которые попадут в коммит")
    print("=" * 78)

    keep = []
    for p in sorted(HERE.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in SKIP_EXT or ".bak" in p.name:
            continue
        if p.name in {".env", ".gitignore", "cleanup_before_git.py"}:
            continue
        if p.name.startswith("apply_patch_") or p.name.startswith("probe_"):
            continue
        if p.name.startswith("fix_") or p.name.startswith("check_"):
            continue
        if p.name.startswith("debug_") or p.name.startswith("show_"):
            continue
        if p.name.startswith("find_") or p.name.startswith("where_is_"):
            continue
        if p.name.startswith("add_") or p.name.startswith("install_"):
            continue
        if p.suffix in {".py", ".json", ".md", ".txt", ".bat", ".html",
                        ".js", ".css", ".yml", ".yaml", ".toml", ".cfg",
                        ".ini"}:
            keep.append(p.relative_to(HERE))

    by_dir = defaultdict(list)
    for p in keep:
        by_dir[str(p.parent)].append(p.name)

    for d in sorted(by_dir):
        print(f"\n  {d}/  ({len(by_dir[d])})")
        for name in sorted(by_dir[d])[:25]:
            print(f"    {name}")
        if len(by_dir[d]) > 25:
            print(f"    … и ещё {len(by_dir[d]) - 25}")

    print(f"\n  Всего файлов: {len(keep)}")


# ══════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Подготовка проекта к git: gitignore + чистка + проверка")
    ap.add_argument("--dry-run", action="store_true",
                    help="ничего не менять, только показать")
    args = ap.parse_args()

    print("=" * 78)
    print(f"  ПОДГОТОВКА К GIT {'' if not args.dry_run else '(DRY-RUN)'}")
    print("=" * 78)
    print(f"  Папка: {HERE}")

    step1_gitignore(args.dry_run)
    moved = step2_cleanup(args.dry_run)
    problems = step3_secrets()
    env_ok = step4_env_check()
    step5_git_status()
    step6_final_list()

    print("\n" + "=" * 78)
    print("  ИТОГ")
    print("=" * 78)
    print(f"  .gitignore:      ✅")
    print(f"  Перенесено:      {moved} файлов в archive/")
    print(f"  .env:            {'✅ в игноре' if env_ok else '❌ НЕ в игноре!'}")
    if problems:
        print(f"  Секреты:         ⚠️  {len(problems)} подозрительных")
    else:
        print(f"  Секреты:         ✅ не найдено")

    print()
    if args.dry_run:
        print("  DRY-RUN. Запусти без --dry-run, чтобы применить.")
    else:
        print("  Готово к git:")
        print("    git init")
        print("    git add .")
        print("    git status        ← проверь глазами")
        print("    git commit -m 'Initial commit'")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())