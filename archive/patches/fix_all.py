"""
fix_all.py — чинит проблемы, найденные check_system.py.

  1. Добавляет `import threading` в redcat_scraper.py (патч 12).
  2. Запускает apply_patch_10.py, если он лежит рядом (патч 10).
  3. Склеивает токен, разорванный на несколько строк в .env.
  4. Проверяет импорт и запускает check_system.py.

Перед любой правкой делается копия *.bak_fix. Значение токена не печатается.
Запуск из папки проекта:  python fix_all.py
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:
            pass

BASE = Path(__file__).resolve().parent


def backup(path: Path) -> Path:
    dst = path.with_name(path.name + ".bak_fix")
    if not dst.exists():
        shutil.copy2(path, dst)
    return dst


# ── 1. import threading ──────────────────────────────────────
def fix_threading() -> None:
    print("1. import threading в redcat_scraper.py")
    path = BASE / "redcat_scraper.py"
    if not path.exists():
        print("   ❌ redcat_scraper.py не найден рядом со скриптом")
        return
    text = path.read_text(encoding="utf-8")
    if re.search(r"^import threading\b", text, re.M):
        print("   ✅ уже есть")
        return
    backup(path)
    for pattern in (r"^import time$", r"^import sys$", r"^import re$"):
        m = re.search(pattern, text, re.M)
        if m:
            if pattern == r"^import time$":
                text = text[:m.start()] + "import threading\n" + text[m.start():]
            else:
                text = text[:m.end()] + "\nimport threading" + text[m.end():]
            break
    else:
        m = re.search(r"^import \w+", text, re.M)
        if not m:
            print("   ❌ не нашёл блок импортов — добавьте `import threading` вручную")
            return
        text = text[:m.start()] + "import threading\n" + text[m.start():]
    path.write_text(text, encoding="utf-8")
    print("   ✅ добавлено (копия: redcat_scraper.py.bak_fix)")


# ── 2. патч 10 ───────────────────────────────────────────────
def run_patch_10() -> None:
    print("2. Патч 10 (preflight_check_split)")
    patch = BASE / "apply_patch_10.py"
    if not patch.exists():
        print("   ⚠️ apply_patch_10.py не найден — пришлите его или свежий "
              "redcat_scraper.py, и я сделаю правку точно")
        return
    res = subprocess.run([sys.executable, str(patch)], cwd=str(BASE),
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    out = (res.stdout or "") + (res.stderr or "")
    for line in out.strip().splitlines()[-8:]:
        print("   | " + line)
    print("   ✅ выполнен" if res.returncode == 0
          else f"   ❌ код {res.returncode} — пришлите вывод выше")


# ── 3. .env ──────────────────────────────────────────────────
KEY_LINE = re.compile(r"^\s*(#|[A-Za-z_][A-Za-z0-9_]*\s*=)")
TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_\-\.\+/=]+$")


def fix_env() -> None:
    print("3. .env")
    path = BASE / ".env"
    if not path.exists():
        print("   ⚠️ .env не найден")
        return
    raw = path.read_bytes()
    if b"\x00" in raw or raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        print("   ❌ файл в UTF-16 — выполните `python set_token.py`")
        return
    lines = raw.decode("utf-8-sig", errors="replace").splitlines()
    out, i, fixed = [], 0, False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("REDCAT_TOKEN="):
            val = line.split("=", 1)[1]
            j = i + 1
            while j < len(lines):
                nxt = lines[j].strip()
                if not nxt or KEY_LINE.match(nxt) or not TOKEN_CHARS.match(nxt):
                    break
                val += nxt
                j += 1
            val = re.sub(r"\s+", "", val).strip("\"'")
            if val.lower().startswith("bearer"):
                val = val[6:]
            if j > i + 1:
                fixed = True
            out.append(f"REDCAT_TOKEN={val}")
            i = j
            continue
        out.append(line)
        i += 1
    # оставшиеся мусорные строки (не KEY=, не комментарий) убираем
    cleaned = [l for l in out if not l.strip() or KEY_LINE.match(l)]
    dropped = len(out) - len(cleaned)
    if not fixed and not dropped:
        print("   ✅ править нечего")
        return
    backup(path)
    path.write_text("\n".join(cleaned) + "\n", encoding="utf-8")
    tok = next((l.split("=", 1)[1] for l in cleaned
                if l.startswith("REDCAT_TOKEN=")), "")
    print(f"   ✅ токен склеен, длина {len(tok)}, частей через точку: "
          f"{tok.count('.') + 1}; удалено мусорных строк: {dropped} "
          f"(копия: .env.bak_fix)")
    if tok.count(".") != 2:
        print("   ⚠️ это не похоже на JWT — вставьте токен заново: "
              "python set_token.py")


# ── 4. проверка ──────────────────────────────────────────────
def verify() -> int:
    print("4. Проверка")
    res = subprocess.run([sys.executable, "-c", "import redcat_scraper"],
                         cwd=str(BASE), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode == 0:
        print("   ✅ redcat_scraper импортируется")
    else:
        print("   ❌ импорт падает:")
        for line in res.stderr.strip().splitlines()[-5:]:
            print("   | " + line)
        return 1
    check = BASE / "check_system.py"
    if check.exists():
        print("   — запускаю check_system.py —\n")
        return subprocess.run([sys.executable, str(check)], cwd=str(BASE)).returncode
    return 0


if __name__ == "__main__":
    fix_threading()
    run_patch_10()
    fix_env()
    sys.exit(verify())
