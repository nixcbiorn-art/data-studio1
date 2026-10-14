#!/usr/bin/env python3
"""fix_routes_one.py — заменяет _one( на self._one( в роутах.

Тот же баг, что был в _api_stream: _one — метод класса Handler,
а вызывается как глобальная функция.
"""
from __future__ import annotations
import ast
import re
import shutil
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
for c in (ROOT, *ROOT.parents):
    if (c / "redcat" / "web").is_dir():
        ROOT = c
        break


FILES = [
    ROOT / "redcat" / "web" / "web_routes_get.py",
    ROOT / "redcat" / "web" / "web_routes_post.py",
    ROOT / "redcat" / "web" / "web_routes_mail.py",
]

total_changes = 0
for P in FILES:
    if not P.exists():
        print(f"- {P.relative_to(ROOT)} нет")
        continue

    t = P.read_text(encoding="utf-8")
    lines = t.split("\n")
    changed_in_file = 0

    for i, line in enumerate(lines):
        # Пропускаем определение def _one(...) и уже готовые self._one(
        if re.search(r"\bdef\s+_one\s*\(", line):
            continue
        if "self._one(" in line:
            continue
        # Заменяем только вызовы _one( — не трогая вхождения внутри слов
        # вида something._one(
        new_line = re.sub(r"(?<![\w.])_one\(", "self._one(", line)
        if new_line != line:
            lines[i] = new_line
            changed_in_file += 1
            if changed_in_file <= 5:  # печатаем первые 5 для контроля
                print(f"  {P.name}:{i+1}: "
                      f"{line.strip()[:70]} → {new_line.strip()[:70]}")

    if changed_in_file == 0:
        print(f"= {P.relative_to(ROOT)} — менять нечего")
        continue

    new_t = "\n".join(lines)
    try:
        ast.parse(new_t)
    except SyntaxError as e:
        print(f"❌ {P.relative_to(ROOT)}: {e.lineno}: {e.msg}")
        continue

    bak = P.with_suffix(P.suffix + ".bak_selfone")
    if not bak.exists():
        shutil.copy2(P, bak)
        print(f"  ↳ бэкап: {bak.name}")

    P.write_text(new_t, encoding="utf-8")
    print(f"~ {P.relative_to(ROOT)}: заменено {changed_in_file} вызовов")
    total_changes += changed_in_file

print()
if total_changes == 0:
    print("⚠ Ничего не поменялось. Значит _one уже был с self. —")
    print("  или роут написан по-другому.")
    print("  Пришли вывод:")
    print("  Select-String -Path redcat\\web\\web_routes_get.py -Pattern "
          "'mail_contacts_tree' -Context 3,10")
    sys.exit(1)

# Проверка импорта
import subprocess
ok = True
for mod in ("redcat.web.web_routes_get",
            "redcat.web.web_routes_post",
            "redcat.web.web_routes_mail"):
    r = subprocess.run([sys.executable, "-c", f"import {mod}"],
                       cwd=str(ROOT), capture_output=True, text=True)
    if r.returncode == 0:
        print(f"✅ import {mod}")
    else:
        print(f"❌ import {mod}")
        for ln in (r.stderr or "").strip().splitlines()[-3:]:
            print(f"   {ln}")
        ok = False

print()
print("Перезапусти studio.py (Ctrl+C → python studio.py).")
if not ok:
    sys.exit(1)