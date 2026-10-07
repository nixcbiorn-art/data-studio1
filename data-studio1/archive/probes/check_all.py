"""
ПОЛНАЯ ДИАГНОСТИКА RedCat Studio
================================
Один скрипт проверяет всё, что можно проверить без браузера:

  • файлы проекта на месте;
  • содержимое папки sources/ и валидность JSON;
  • загрузку источников через sources.py (как это делает сборщик);
  • load_specs() из webapp.py (как это делает сервер);
  • известные баги фронтенда в app.js (пропущенные fill(...) и т. п.);
  • живой сервер: /api/sources и /api/meta на портах 8765–8769;
  • хвост redcat_scraper.log.

Запуск из папки проекта:

    python check_all.py

Ничего не меняет — только читает и печатает. В конце выдаёт итоговый
вердикт: «всё ок», «вот проблема, вот причина, вот что делать».
"""

from __future__ import annotations

import json
import re
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
SOURCES = BASE / "sources"
WEB = BASE / "web"

problems: list[tuple[str, str]] = []   # (что, что делать)
warnings: list[tuple[str, str]] = []


def ok(msg):    print(f"  ✅ {msg}")
def warn(msg):  print(f"  ⚠️  {msg}")
def bad(msg):   print(f"  ❌ {msg}")
def head(title): print(f"\n{'─' * 72}\n  {title}\n{'─' * 72}")


# ──────────────────────────────────────────────────────────────
#  1. Файлы проекта
# ──────────────────────────────────────────────────────────────
head("1. Файлы проекта")

REQUIRED = [
    "webapp.py", "studio.py", "sources.py", "dataops.py",
    "completeness.py", "redcat_scraper.py", "studio_store.py",
    "api_guard.py", "storage.py", "run_stats.py", "quality.py",
    "anomalies.py", "crosschecks.py", "report_html.py",
]
missing = [name for name in REQUIRED if not (BASE / name).exists()]
if missing:
    for m in missing:
        bad(f"{m} — НЕ НАЙДЕН")
    problems.append((
        f"Не хватает {len(missing)} файлов: {', '.join(missing)}",
        "Скопируйте недостающие файлы в папку проекта."))
else:
    ok(f"Все {len(REQUIRED)} модулей на месте")

# web-папка нужна только для UI
if WEB.exists():
    ok(f"web/ найдена ({len(list(WEB.glob('*')))} файлов)")
else:
    warn("Папка web/ не найдена — интерфейс не загрузится")
    problems.append(("Нет папки web/", "Положите рядом с webapp.py папку web/ "
                                        "с index.html, app.js, style.css."))


# ──────────────────────────────────────────────────────────────
#  2. Папка sources/
# ──────────────────────────────────────────────────────────────
head("2. Папка sources/")

if not SOURCES.exists():
    bad(f"Папка не найдена: {SOURCES}")
    problems.append(("Нет папки sources/",
                     "Создайте папку sources/ рядом с webapp.py и положите туда JSON."))
else:
    files = sorted(SOURCES.glob("*.json"))
    ok(f"Путь: {SOURCES}")
    ok(f"JSON-файлов: {len(files)}")
    for f in files:
        print(f"      📄 {f.name} ({f.stat().st_size} б)")

    for f in files:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            bad(f"{f.name}: сломан JSON — {e}")
            problems.append((f"{f.name}: не разбирается как JSON",
                             f"Строка {e.lineno}, колонка {e.colno}: {e.msg}"))
            continue
        except Exception as e:
            bad(f"{f.name}: {type(e).__name__}: {e}")
            continue

        items = payload if isinstance(payload, list) else [payload]
        keys = [it.get("key", "?") for it in items if isinstance(it, dict)]
        enabled_flags = [it.get("enabled", True) for it in items if isinstance(it, dict)]
        enabled = sum(1 for e in enabled_flags if e)
        ok(f"{f.name}: {len(items)} шт, enabled={enabled}/{len(items)} → {keys}")


# ──────────────────────────────────────────────────────────────
#  3. Загрузка через sources.py
# ──────────────────────────────────────────────────────────────
head("3. Загрузка через sources.py (как это делает сборщик)")

if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

try:
    import sources as src
    ok("sources.py импортирован")
except Exception as e:
    bad(f"sources.py не импортируется: {type(e).__name__}: {e}")
    traceback.print_exc()
    src = None

if src:
    try:
        n = src.load_from_dir(SOURCES)
        ok(f"load_from_dir() → {n} описаний")
    except Exception as e:
        bad(f"load_from_dir() упал: {type(e).__name__}: {e}")
        traceback.print_exc()

    try:
        specs = src.all_sources()
        keys = [s.key for s in specs]
        if specs:
            ok(f"all_sources() → {len(specs)} источников: {keys}")
            for s in specs:
                dep = f"  ← зависит от {s.depends_on}" if s.depends_on else ""
                spl = f"  ⧉ дробится по {s.split_values_from}" if s.split_values_from else ""
                print(f"        • {s.key:20} {s.title or ''}{dep}{spl}")
        else:
            bad("all_sources() → 0 источников")
            problems.append(("all_sources() пуст при непустом sources/",
                             "Запустите: python -c \"import sources as s; "
                             "s.load_from_dir('sources'); print(s.all_sources())\" "
                             "— увидите настоящую ошибку (цикл зависимостей и т.п.)."))
    except Exception as e:
        bad(f"all_sources() упал: {type(e).__name__}: {e}")
        traceback.print_exc()
        problems.append(("all_sources() падает",
                         "Чаще всего это циклическая зависимость. Смотрите трейс выше."))

    try:
        entries = src.list_source_files(SOURCES)
        ok(f"list_source_files() → {len(entries)} записей (вкладка «Источники»)")
    except Exception as e:
        bad(f"list_source_files() упал: {type(e).__name__}: {e}")
        traceback.print_exc()


# ──────────────────────────────────────────────────────────────
#  4. load_specs() из webapp (без глушителя ошибок)
# ──────────────────────────────────────────────────────────────
head("4. load_specs() из webapp.py (как это видит сервер)")

try:
    import webapp
    ok(f"webapp.SOURCES_DIR = {webapp.SOURCES_DIR}")
    ok(f"webapp.DATA_DB     = {webapp.DATA_DB} "
       f"({'есть' if webapp.DATA_DB.exists() else 'НЕТ ФАЙЛА'})")

    # Сначала как в проде — с глушителем
    try:
        specs = webapp.load_specs()
        if specs:
            ok(f"load_specs() → {len(specs)} источников: {list(specs)}")
        else:
            bad("load_specs() → 0 источников (ошибка съедена try/except)")
            # Теперь без глушителя — увидим причину
            try:
                webapp.src.load_from_dir(webapp.SOURCES_DIR)
                result = {s.key: s for s in webapp.src.all_sources()}
                if not result:
                    bad("Без глушителя тоже 0 — смотрите раздел 3 выше.")
                else:
                    ok(f"Без глушителя: {len(result)} источников — "
                       f"значит проблема в самом load_specs()")
            except Exception as e:
                bad(f"Без глушителя падает: {type(e).__name__}: {e}")
                traceback.print_exc()
                problems.append((
                    "load_specs() падает с исключением",
                    "Смотрите трейс выше. Патч в webapp.py:\n"
                    "    def load_specs():\n"
                    "        try:\n"
                    "            src.load_from_dir(SOURCES_DIR)\n"
                    "            return {s.key: s for s in src.all_sources()}\n"
                    "        except Exception as e:\n"
                    "            import traceback; traceback.print_exc()\n"
                    "            return {}"))
    except Exception as e:
        bad(f"load_specs() упал: {type(e).__name__}: {e}")
        traceback.print_exc()
except ImportError as e:
    bad(f"webapp.py не импортируется: {e}")
    problems.append(("webapp.py не импортируется",
                     "Проверьте, что запускаете из папки проекта и что все модули "
                     "из раздела 1 на месте."))
except Exception as e:
    bad(f"webapp.load_specs() упал: {type(e).__name__}: {e}")
    traceback.print_exc()


# ──────────────────────────────────────────────────────────────
#  5. Известные баги фронтенда в app.js
# ──────────────────────────────────────────────────────────────
head("5. Проверка app.js на известные баги фронтенда")

app_js = WEB / "app.js"
if not app_js.exists():
    warn("app.js не найден — пропускаю проверку фронтенда")
else:
    text = app_js.read_text(encoding="utf-8", errors="replace")

    # Баг 1: не наполняется селектор таблиц (был с cTable)
    # Ищем строку с fill(...) и сверяем со списком селекторов в index.html
    html_path = WEB / "index.html"
    if html_path.exists():
        html = html_path.read_text(encoding="utf-8", errors="replace")
        # какие селекторы таблиц есть в разметке
        table_selectors = re.findall(
            r'<select\s+id="([^"]+)"[^>]*onchange="App\.\w+\(\)"', html)
        # какие наполняются в loadMeta()
        meta_block = re.search(r"async loadMeta\(\)\s*{(.+?)\n  },", text, re.S)
        filled = set()
        if meta_block:
            filled = set(re.findall(r"fill\('([^']+)'\)", meta_block.group(1)))
        # какие вообще передаются в fill(...) по всему файлу
        all_filled = set(re.findall(r"fill\('([^']+)'\)", text))

        # из table_selectors фильтруем только те, что вероятно должны
        # наполняться таблицами (cTable, dataTable, anTable, qTable, sqlTable...)
        likely_table_sels = [s for s in table_selectors
                             if s.endswith("Table") or s in
                             ("dataTable", "anTable", "qTable", "cTable")]
        missing_fill = [s for s in likely_table_sels
                        if s not in all_filled and s not in filled]
        if missing_fill:
            bad(f"Селекторы без fill(...): {missing_fill}")
            for s in missing_fill:
                problems.append((
                    f"В app.js нет fill('{s}') — этот селектор останется пустым",
                    f"Найдите строку fill('dataTable'); fill('anTable'); ... и "
                    f"допишите fill('{s}')."))
        else:
            ok(f"Все селекторы таблиц ({likely_table_sels}) наполняются через fill(...)")

    # Баг 2: странные символы или BOM в начале файла
    if text.startswith("\ufeff"):
        warn("app.js начинается с BOM — может ломать парсинг в некоторых браузерах")

    # Баг 3: HTML-комментарии вида <!-- --> недопустимы в JS
    if "<!--" in text:
        bad("В app.js найден HTML-комментарий <!-- ... -->")
        problems.append(("HTML-комментарии в JS",
                         "Удалите <!-- ... --> из app.js — они ломают парсинг."))


# ──────────────────────────────────────────────────────────────
#  6. Живой сервер
# ──────────────────────────────────────────────────────────────
head("6. Живой сервер /api/* (порты 8765–8769)")


def fetch(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}


server_port = None
for port in range(8765, 8770):
    probe = fetch(f"http://127.0.0.1:{port}/api/meta", timeout=1.5)
    if "__error__" not in probe:
        server_port = port
        ok(f"Сервер найден на порту {port}")
        break

if server_port is None:
    warn("Живой сервер не найден ни на одном порту 8765–8769")
    warn("Запустите его в отдельном окне: python studio.py")
else:
    # /api/sources — это вкладка «Источники»
    sources_resp = fetch(f"http://127.0.0.1:{server_port}/api/sources")
    if "__error__" in sources_resp:
        bad(f"/api/sources: {sources_resp['__error__']}")
        problems.append(("/api/sources не отвечает",
                         "Смотрите консоль сервера — там должно быть исключение."))
    else:
        items = sources_resp.get("items") or []
        hint = sources_resp.get("hint") or ""
        print(f"\n  GET /api/sources → {len(items)} записей")
        for it in items:
            print(f"      • {it.get('file','?')} → key={it.get('key','?')!r}")
        if not items:
            bad(f"/api/sources вернул 0 записей")
            if hint:
                print(f"      hint: {hint}")
            print(f"      dir:  {sources_resp.get('dir','?')}")
            problems.append((
                "Вкладка «Источники» пуста при живых источниках",
                "Смотрите hint выше. Чаще всего: не совпадает SOURCES_DIR "
                "в webapp.py и папка, где лежат JSON."))

    # /api/meta — это селектор «источники» на вкладке «Сбор»
    meta_resp = fetch(f"http://127.0.0.1:{server_port}/api/meta")
    if "__error__" in meta_resp:
        bad(f"/api/meta: {meta_resp['__error__']}")
    else:
        srcs = meta_resp.get("sources") or []
        tables = meta_resp.get("tables") or []
        print(f"\n  GET /api/meta → sources: {len(srcs)}, tables: {len(tables)}")
        if srcs:
            print(f"      источники: {[s.get('key') for s in srcs]}")
        else:
            warn("meta.sources пуст — селектор «Сбор» не покажет источники")
        print(f"      data_ok: {meta_resp.get('data_ok')}")
        print(f"      studio:  {meta_resp.get('studio')}")


# ──────────────────────────────────────────────────────────────
#  7. Лог сборщика
# ──────────────────────────────────────────────────────────────
head("7. Хвост redcat_scraper.log")

log = BASE / "redcat_scraper.log"
if log.exists():
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
        for line in lines:
            print(f"  {line}")
    except Exception as e:
        warn(f"Не удалось прочитать лог: {e}")
else:
    ok("Лог отсутствует — сборщик ещё не запускался. Это не ошибка.")


# ──────────────────────────────────────────────────────────────
#  Итог
# ──────────────────────────────────────────────────────────────
head("ИТОГ")

if not problems:
    print("  ✅ Явных проблем на стороне Python и файлов не найдено.\n")
    print("  Что проверить в браузере, если источники всё ещё не видны:\n")
    print("    1. Откройте http://127.0.0.1:%d/ (тот порт, что выше)" % (server_port or 8765))
    print("    2. Нажмите Ctrl+F5 — жёсткое обновление без кеша")
    print("    3. Левое меню → 🔌 Источники. Должно быть 8 записей.")
    print("    4. Если пусто: F12 → Console → пришлите красные строки")
    print("    5. F12 → Network → обновите → клик на запрос `sources` → Response.")
    print("       Там должен быть JSON с непустым \"items\".")
else:
    print(f"  Найдено проблем: {len(problems)}\n")
    for i, (what, do) in enumerate(problems, 1):
        print(f"  {i}. {what}")
        print(f"     → {do}\n")
    print("  Если после правок что-то осталось — перезапустите сервер:")
    print("      Ctrl+C в окне studio.py, затем снова: python studio.py")

print("=" * 72)
sys.exit(1 if problems else 0)