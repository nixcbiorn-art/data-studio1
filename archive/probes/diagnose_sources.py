"""
Диагностика источников и приложения RedCat Studio
=================================================
Запуск из папки проекта (там, где лежит webapp.py):

    python diagnose_sources.py

Печатает всё, что нужно, чтобы понять, почему источники не видны в
приложении. Ничего не меняет — только читает файлы и, если сервер уже
запущен, спрашивает у него /api/sources и /api/meta.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

# ──────────────────────────────────────────────────────────────
#  Где мы вообще находимся
# ──────────────────────────────────────────────────────────────
BASE = Path(__file__).resolve().parent
print("=" * 72)
print("  ДИАГНОСТИКА ИСТОЧНИКОВ")
print("=" * 72)
print(f"Python:  {sys.version.split()[0]}  ({sys.executable})")
print(f"Папка:   {BASE}")
print(f"cwd:     {Path.cwd()}")
print()

# ──────────────────────────────────────────────────────────────
#  Какие файлы лежат рядом
# ──────────────────────────────────────────────────────────────
print("─" * 72)
print("  1. Файлы проекта")
print("─" * 72)
expected = ["webapp.py", "studio.py", "sources.py", "dataops.py",
            "completeness.py", "redcat_scraper.py", "studio_store.py"]
for name in expected:
    p = BASE / name
    print(f"  {'✅' if p.exists() else '❌'} {name:28} "
          f"{'найден' if p.exists() else 'НЕ НАЙДЕН'}")
print()

# ──────────────────────────────────────────────────────────────
#  Папка sources
# ──────────────────────────────────────────────────────────────
print("─" * 72)
print("  2. Папка sources/")
print("─" * 72)
SOURCES = BASE / "sources"
print(f"  Путь: {SOURCES}")
print(f"  Существует: {'да' if SOURCES.exists() else 'НЕТ'}")
if SOURCES.exists():
    entries = sorted(SOURCES.iterdir())
    print(f"  Файлов внутри: {len(entries)}")
    for e in entries:
        mark = "📄" if e.is_file() else "📁"
        size = f" ({e.stat().st_size} б)" if e.is_file() else ""
        print(f"    {mark} {e.name}{size}")
else:
    print("  ⚠️ Папка отсутствует — источник здесь искать нечего.")
print()

# ──────────────────────────────────────────────────────────────
#  Импорт и загрузка sources.py
# ──────────────────────────────────────────────────────────────
print("─" * 72)
print("  3. Загрузка источников через sources.py")
print("─" * 72)

if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

try:
    import sources as src
    print("  ✅ sources.py импортирован")
except Exception as e:
    print(f"  ❌ Не удалось импортировать sources.py: {type(e).__name__}: {e}")
    traceback.print_exc()
    sys.exit(1)

loaded = 0
try:
    loaded = src.load_from_dir(SOURCES)
    print(f"  load_from_dir() вернул: {loaded}")
except Exception as e:
    print(f"  ❌ load_from_dir() упал: {type(e).__name__}: {e}")
    traceback.print_exc()

try:
    specs = src.all_sources()
    keys = [s.key for s in specs]
    print(f"  all_sources() вернул: {len(specs)} шт → {keys}")
    for s in specs:
        dep = f" → зависит от {s.depends_on}" if s.depends_on else ""
        split = (f" ⧉ дробится по {s.split_values_from}"
                 if s.split_values_from else "")
        print(f"    • {s.key:22} {s.title or ''}{dep}{split}")
except Exception as e:
    print(f"  ❌ all_sources() упал: {type(e).__name__}: {e}")
    traceback.print_exc()

# прямая проверка list_source_files (это то, что видит вкладка «Источники»)
try:
    files = src.list_source_files(SOURCES)
    print(f"\n  list_source_files() (вкладка «Источники»): {len(files)} шт")
    for path, key, title, url in files:
        print(f"    • {path.name:32} key={key!r:22} {title or ''}")
except Exception as e:
    print(f"  ❌ list_source_files() упал: {type(e).__name__}: {e}")
    traceback.print_exc()
print()

# ──────────────────────────────────────────────────────────────
#  Содержимое JSON — есть ли ошибки разбора
# ──────────────────────────────────────────────────────────────
print("─" * 72)
print("  4. Разбор JSON-файлов по отдельности")
print("─" * 72)
if SOURCES.exists():
    for path in sorted(SOURCES.glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            if isinstance(payload, list):
                keys = [item.get("key", "?") for item in payload
                        if isinstance(item, dict)]
                print(f"  ✅ {path.name}: массив из {len(payload)} шт → {keys}")
            elif isinstance(payload, dict):
                print(f"  ✅ {path.name}: объект, key={payload.get('key', '?')!r}")
            else:
                print(f"  ⚠️ {path.name}: не объект и не массив, "
                      f"а {type(payload).__name__}")
        except json.JSONDecodeError as e:
            print(f"  ❌ {path.name}: не разбирается как JSON — {e}")
            # покажем строку с ошибкой
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                if 0 < e.lineno <= len(lines):
                    print(f"       строка {e.lineno}: {lines[e.lineno - 1][:120]}")
            except Exception:
                pass
        except Exception as e:
            print(f"  ❌ {path.name}: {type(e).__name__}: {e}")
print()

# ──────────────────────────────────────────────────────────────
#  load_specs из webapp (та самая функция, которая глотает ошибки)
# ──────────────────────────────────────────────────────────────
print("─" * 72)
print("  5. load_specs() — так, как её вызывает веб-приложение")
print("─" * 72)
try:
    # Осторожно: импорт webapp поднимет api_guard и запустит сетевой
    # перехватчик в текущем процессе. Это безопасно (он только блокирует
    # запись), но может вывести пару строк в лог.
    import webapp
    print(f"  webapp.SOURCES_DIR = {webapp.SOURCES_DIR}")
    print(f"  webapp.OUTPUT_DIR  = {webapp.OUTPUT_DIR}")
    print(f"  webapp.DATA_DB     = {webapp.DATA_DB}  "
          f"({'есть' if webapp.DATA_DB.exists() else 'НЕТ ФАЙЛА'})")
    specs = webapp.load_specs()
    print(f"  load_specs() вернул {len(specs)} источников: {list(specs)}")
    if not specs:
        print("  ⚠️ ПУСТО. Значит, ошибка съедена в try/except внутри load_specs.")
        print("     Ниже — прогон тех же вызовов без глушителя, чтобы увидеть причину:")
        try:
            webapp.src.load_from_dir(webapp.SOURCES_DIR)
            result = {s.key: s for s in webapp.src.all_sources()}
            print(f"     Без глушителя тоже пусто? {len(result) == 0}")
            if not result:
                print("     ⚠️ Да, пусто. Смотрите вывод выше — там уже есть детали.")
        except Exception as e:
            print(f"     ❌ Без глушителя падает: {type(e).__name__}: {e}")
            traceback.print_exc()
except ImportError as e:
    print(f"  ❌ Не удалось импортировать webapp: {e}")
    print("     Убедитесь, что запускаете скрипт из папки проекта.")
except Exception as e:
    print(f"  ❌ webapp.load_specs() упал: {type(e).__name__}: {e}")
    traceback.print_exc()
print()

# ──────────────────────────────────────────────────────────────
#  Хвост лога сборщика
# ──────────────────────────────────────────────────────────────
print("─" * 72)
print("  6. Последние строки redcat_scraper.log")
print("─" * 72)
log = BASE / "redcat_scraper.log"
if log.exists():
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-25:]
        for line in tail:
            print(f"  {line}")
        if not tail:
            print("  (лог пуст)")
    except Exception as e:
        print(f"  ❌ Не удалось прочитать лог: {e}")
else:
    print("  (файла нет — сборщик ни разу не запускался)")
print()

# ──────────────────────────────────────────────────────────────
#  Живой сервер: спросить /api/sources и /api/meta
# ──────────────────────────────────────────────────────────────
print("─" * 72)
print("  7. Живой сервер (если запущен)")
print("─" * 72)


def probe(port: int):
    """Пробует обратиться к серверу на данном порту. Возвращает True, если жив."""
    import urllib.request
    import urllib.error

    for route in ("sources", "meta"):
        url = f"http://127.0.0.1:{port}/api/{route}"
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.URLError as e:
            print(f"  Порт {port}: сервер не отвечает ({e.reason})")
            return False
        except Exception as e:
            print(f"  Порт {port}: ошибка при запросе {url}: {e}")
            return False

        if route == "sources":
            items = data.get("items") or []
            hint = data.get("hint") or ""
            print(f"  GET /api/sources → {len(items)} шт")
            for it in items:
                print(f"    • {it.get('file', '?'):32} key={it.get('key', '?')!r}")
            if not items:
                print(f"    hint: {hint or '(нет)'}")
                print(f"    dir:  {data.get('dir', '?')}")
        else:
            srcs = data.get("sources") or []
            tables = data.get("tables") or []
            print(f"  GET /api/meta → sources: {len(srcs)} шт, "
                  f"tables: {len(tables)} шт")
            if srcs:
                print(f"    источники: {[s.get('key') for s in srcs]}")
            else:
                print(f"    ⚠️ sources пуст — это то, что видит селектор «Сбор»")
            print(f"    data_ok: {data.get('data_ok')}")
    return True


found_any = False
for port in (8765, 8766, 8767, 8768, 8769):
    if probe(port):
        found_any = True
        break
if not found_any:
    print("  Сервер не запущен или слушает другой порт.")
    print("  Запустите его в отдельном окне: python studio.py")
print()

# ──────────────────────────────────────────────────────────────
#  Итог
# ──────────────────────────────────────────────────────────────
print("=" * 72)
print("  ЧТО ДАЛЬШЕ")
print("=" * 72)
print("""
Пришлите целиком вывод этого скрипта. По нему сразу видно:

  • где лежат файлы (раздел 1);
  • что в папке sources/ (раздел 2);
  • сколько источников видят модули сборщика и приложения (разделы 3 и 5);
  • не сломан ли JSON (раздел 4);
  • что пишет лог (раздел 6);
  • что отдаёт живой сервер (раздел 7).

Типичные расхождения:

  • раздел 3 показывает источники, а раздел 5 — нет → ошибка в load_specs()
    (её видно по трейсбеку в разделе 5);
  • раздел 5 показывает источники, а раздел 7 — нет → сервер запущен из
    другой папки (сравните BASE в шапке с webapp.SOURCES_DIR в разделе 5);
  • раздел 7 не отвечает → сервер вообще не запущен, либо слушает не тот порт.
""")