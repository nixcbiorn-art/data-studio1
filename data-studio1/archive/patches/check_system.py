"""
Диагностика состояния проекта после серии патчей 1–12.
========================================================
Только читает файлы и импортирует модули. Ничего не меняет.

Что проверяет:
  • какие файлы на месте;
  • какие патчи уже применены (по маркерам в коде);
  • импортируются ли ключевые модули без ошибок;
  • есть ли обязательные реестры (PARSERS, FETCHERS, PAGINATORS);
  • какие поля добавлены в SourceSpec;
  • состояние env: rate limit, circuit breaker;
  • состояние .env (по токену).

Запуск:
    python check_system.py
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
OK, WARN, BAD, INFO = "✅", "⚠️", "❌", "ℹ️"

problems: list[str] = []


def head(title: str) -> None:
    print(f"\n{'─' * 72}\n  {title}\n{'─' * 72}")


def ok(msg: str) -> None:
    print(f"  {OK} {msg}")


def warn(msg: str) -> None:
    print(f"  {WARN} {msg}")


def bad(msg: str) -> None:
    print(f"  {BAD} {msg}")
    problems.append(msg)


def info(msg: str) -> None:
    print(f"  {INFO} {msg}")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _has(path: Path, marker: str) -> bool:
    return marker in _read(path)


# ──────────────────────────────────────────────────────────────
#  1. Файлы проекта
# ──────────────────────────────────────────────────────────────
head("1. Файлы проекта")

REQUIRED = [
    "sources.py", "redcat_scraper.py", "webapp.py", "studio.py",
    "dataops.py", "studio_store.py", "api_guard.py", "completeness.py",
]
OPTIONAL = [
    "rate_limit.py",                # патч 11
    "field_semantics.py",           # патч 7-9
    "field_semantics.json",         # патч 7-9
    "spec_validator.py",            # патч 6
    "browser_fetch.py",
    "hc_aliases.json",
]
missing_required = []
for name in REQUIRED:
    p = BASE / name
    if p.exists():
        ok(f"{name}")
    else:
        bad(f"{name} — НЕ НАЙДЕН")
        missing_required.append(name)

print()
for name in OPTIONAL:
    p = BASE / name
    if p.exists():
        ok(f"{name}")
    else:
        info(f"{name} — нет (модуль необязательный)")

# web/
web = BASE / "web"
if web.exists() and (web / "app.js").exists():
    ok(f"web/app.js")
else:
    bad("web/app.js — НЕ НАЙДЕН")
    missing_required.append("web/app.js")

if missing_required:
    print("\n  ⛔ Без обязательных файлов дальше проверять нечего.")
    sys.exit(1)


# ──────────────────────────────────────────────────────────────
#  2. Какие патчи применены
# ──────────────────────────────────────────────────────────────
head("2. Применённые патчи")

sources_txt = _read(BASE / "sources.py")
scraper_txt = _read(BASE / "redcat_scraper.py")
webapp_txt = _read(BASE / "webapp.py")
appjs_txt = _read(BASE / "web" / "app.js")

PATCHES = [
    ("1. Реестр парсеров (PARSERS)",
     "PARSERS: dict = {}" in sources_txt),
    ("2. parse_payload в scraper",
     "src.parse_payload" in scraper_txt),
    ("3. Реестр fetch-стратегий (FETCHERS)",
     "FETCHERS: dict = {}" in sources_txt),
    ("4. Реестр пагинаций (PAGINATORS)",
     "PAGINATORS: dict = {}" in sources_txt),
    ("5. Браузерные фетчеры",
     "browser_xhr" in sources_txt),
    ("6. Валидатор spec",
     "spec_validator" in webapp_txt and (BASE / "spec_validator.py").exists()),
    ("7. Словарь категорий (field_semantics)",
     (BASE / "field_semantics.py").exists()
     and "suggest_spec_fields" in _read(BASE / "field_semantics.py")),
    ("8. value_hint / derived / checks",
     (BASE / "field_semantics.py").exists()
     and "suggest_derived" in _read(BASE / "field_semantics.py")),
    ("9. Голосование по записям",
     (BASE / "field_semantics.py").exists()
     and "_aggregate_fields" in _read(BASE / "field_semantics.py")),
    ("10. Fix preflight_check_split",
     "_total_of" in scraper_txt and "def preflight_check_split" in scraper_txt
     and "unfiltered_url, session" not in scraper_txt.split(
         "async def preflight_check_split")[1].split("async def _fetch_one_split")[0]
         if "async def preflight_check_split" in scraper_txt else False),
    ("11. Защита от бана (rate_limit + breaker)",
     (BASE / "rate_limit.py").exists()
     and "_RATE_LIMITERS = _rl.HostRateLimiters" in scraper_txt),
    ("12. import threading в scraper",
     "import threading" in scraper_txt),
]

for name, applied in PATCHES:
    if applied:
        ok(name)
    else:
        bad(f"{name} — не применён")


# ──────────────────────────────────────────────────────────────
#  3. Импорт модулей
# ──────────────────────────────────────────────────────────────
head("3. Импорт модулей")

if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

IMPORTS = [
    ("sources",            True),
    ("rate_limit",         (BASE / "rate_limit.py").exists()),
    ("field_semantics",    (BASE / "field_semantics.py").exists()),
    ("spec_validator",     (BASE / "spec_validator.py").exists()),
    ("api_guard",          True),
    ("dataops",            True),
    ("studio_store",       True),
    ("redcat_scraper",     True),
    ("webapp",             True),
]

mod = {}
for name, should in IMPORTS:
    if not should:
        info(f"{name} — файла нет, пропускаю")
        continue
    try:
        m = importlib.import_module(name)
        importlib.reload(m)
        mod[name] = m
        ok(f"{name}")
    except Exception as e:
        bad(f"{name}: {type(e).__name__}: {e}")
        if "--verbose" in sys.argv or "-v" in sys.argv:
            traceback.print_exc()


# ──────────────────────────────────────────────────────────────
#  4. Реестры и поля
# ──────────────────────────────────────────────────────────────
head("4. Реестры и поля SourceSpec")

src_mod = mod.get("sources")
if src_mod is not None:
    for name in ("PARSERS", "FETCHERS", "ASYNC_FETCHERS", "PAGINATORS"):
        if hasattr(src_mod, name):
            items = sorted(getattr(src_mod, name))
            ok(f"{name}: {', '.join(items) or '(пусто)'}")
        else:
            bad(f"{name} отсутствует в sources")

    fields = src_mod.SourceSpec.__dataclass_fields__
    for f in ("parse_strategy", "parse_hint",
              "fetch_strategy", "pagination_strategy", "cursor_path",
              "browser_xhr_pattern",
              "rate_limit_rps", "circuit_breaker_failures",
              "circuit_breaker_cooldown_sec"):
        if f in fields:
            ok(f"SourceSpec.{f}")
        else:
            warn(f"SourceSpec.{f} — нет")
else:
    bad("sources не импортирован — проверки реестров пропущены")


# ──────────────────────────────────────────────────────────────
#  5. Защита от бана
# ──────────────────────────────────────────────────────────────
head("5. Защита от бана")

if "redcat_scraper" in mod:
    rs = mod["redcat_scraper"]
    for name in ("_RATE_LIMITERS", "_CIRCUIT_BREAKERS",
                 "DEFAULT_RATE_RPS", "DEFAULT_CIRCUIT_FAILURES"):
        if hasattr(rs, name):
            value = getattr(rs, name)
            if isinstance(value, (int, float)):
                ok(f"scraper.{name} = {value}")
            else:
                ok(f"scraper.{name}")
        else:
            warn(f"scraper.{name} — нет")

    for fn in ("_breaker_for", "_rps_for",
               "preflight_check_split", "_total_of"):
        if hasattr(rs, fn):
            ok(f"scraper.{fn}()")
        else:
            bad(f"scraper.{fn}() — отсутствует")
else:
    bad("redcat_scraper не импортирован")


# ──────────────────────────────────────────────────────────────
#  6. field_semantics — сколько категорий
# ──────────────────────────────────────────────────────────────
head("6. Словарь категорий")

if "field_semantics" in mod:
    fs = mod["field_semantics"]
    try:
        cats = fs._load()
        ok(f"категорий: {len(cats)}")
        subcats = ", ".join(sorted(cats.keys()))
        info(subcats)
    except Exception as e:
        warn(f"не удалось прочитать словарь: {e}")
else:
    info("field_semantics не импортирован — пропуск")


# ──────────────────────────────────────────────────────────────
#  7. .env
# ──────────────────────────────────────────────────────────────
head("7. .env и окружение")

env_path = BASE / ".env"
if env_path.exists():
    ok(f".env найден ({env_path.stat().st_size} байт)")
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(env_path)
        # смотрим только «чужие» строки
        bad_lines = []
        for i, line in enumerate(_read(env_path).splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if "=" not in s:
                bad_lines.append(i)
                continue
            k = s.split("=", 1)[0].strip()
            if not k or " " in k:
                bad_lines.append(i)
        if bad_lines:
            warn(f"возможно, не разобраны строки: {bad_lines}")
            info("точную причину покажет: python check_env.py")
        else:
            ok(".env разбирается без замечаний")
        tok = (vals.get("REDCAT_TOKEN") or "").strip()
        if tok:
            ok(f"REDCAT_TOKEN задан ({len(tok)} символов)")
        else:
            warn("REDCAT_TOKEN не задан — сбор пойдёт только для внешних источников")
    except ImportError:
        info("python-dotenv не установлен — проверка .env поверхностная")
else:
    warn(".env не найден — токен нужно передать через --token")

for name in ("REDCAT_RATE_LIMIT_RPS", "REDCAT_CIRCUIT_FAILURES",
             "REDCAT_CIRCUIT_COOLDOWN"):
    v = os.environ.get(name)
    if v:
        ok(f"переменная окружения {name}={v}")
    else:
        info(f"{name} — не задана (используется дефолт)")


# ──────────────────────────────────────────────────────────────
#  8. Быстрые функциональные проверки
# ──────────────────────────────────────────────────────────────
head("8. Быстрые функциональные проверки")

failures = []

# suggest_spec на типовом ответе
if "sources" in mod:
    src_mod = mod["sources"]
    try:
        sample = {
            "data": [
                {"id": 1, "name": "A", "price": 100000, "total_area": 50.0,
                 "developer_name": "ФСК", "housing_complex_id": 42},
                {"id": 2, "name": "B", "price": 120000, "total_area": 55.0,
                 "developer_name": "ФСК", "housing_complex_id": 42},
            ],
            "meta": {"total": 2},
        }
        spec = src_mod.suggest_spec(
            url="https://x/", key="t", sample_json=sample)
        if spec.get("id_field") == "id":
            ok(f"suggest_spec.id_field = {spec['id_field']}")
        else:
            failures.append(f"suggest_spec.id_field = {spec.get('id_field')!r}")
        nums = spec.get("numeric_fields") or []
        if "price" in nums and "total_area" in nums:
            ok(f"suggest_spec.numeric_fields = {nums}")
        else:
            failures.append(f"numeric_fields = {nums}")
        if spec.get("derived"):
            ok(f"suggest_spec.derived = {spec['derived']}")
        else:
            warn("suggest_spec.derived — пусто")
    except Exception as e:
        failures.append(f"suggest_spec: {type(e).__name__}: {e}")

# rate_limit — TokenBucket и CircuitBreaker
if "rate_limit" in mod:
    rl = mod["rate_limit"]
    try:
        b = rl.TokenBucket(rate_per_sec=10.0)
        got = sum(1 for _ in range(10) if b.try_consume() == 0.0)
        if got == 10:
            ok(f"TokenBucket: 10 запросов сразу")
        else:
            failures.append(f"TokenBucket отдал {got} из 10")
        cb = rl.CircuitBreaker(failures=3, cooldown_sec=60)
        cb.record_failure("a"); cb.record_failure("b"); cb.record_failure("c")
        if cb.is_open():
            ok("CircuitBreaker открывается после 3 ошибок")
        else:
            failures.append("CircuitBreaker не открылся после 3 ошибок")
        ra = rl.parse_retry_after({"Retry-After": "42"}, 0.0)
        if ra == 42.0:
            ok("parse_retry_after: 42 сек")
        else:
            failures.append(f"parse_retry_after = {ra}")
    except Exception as e:
        failures.append(f"rate_limit: {type(e).__name__}: {e}")

# _total_of на unified и raw
if "redcat_scraper" in mod and "sources" in mod:
    rs = mod["redcat_scraper"]
    s = mod["sources"].SourceSpec(key="t", url="x",
                                  total_path=("meta", "total"))
    try:
        if rs._total_of({"items": [1], "total": 999,
                         "raw": {"meta": {"total": 1}}}, s) == 999:
            ok("_total_of (unified)")
        else:
            failures.append("_total_of unified вернул не то")
        if rs._total_of({"meta": {"total": 123}}, s) == 123:
            ok("_total_of (raw)")
        else:
            failures.append("_total_of raw вернул не то")
    except Exception as e:
        failures.append(f"_total_of: {type(e).__name__}: {e}")

if failures:
    print("\n  Прочие замечания по функциональности:")
    for f in failures:
        warn(f)


# ──────────────────────────────────────────────────────────────
#  Итог
# ──────────────────────────────────────────────────────────────
head("ИТОГ")
if not problems:
    print(f"  {OK} Критических проблем не найдено.")
    print(f"     Проект в рабочем состоянии.")
else:
    print(f"  {BAD} Найдено проблем: {len(problems)}")
    for p in problems:
        print(f"     • {p}")
    print(f"\n  Следующий шаг: применить недостающие патчи (файлы apply_patch_N.py)")
    print(f"  или восстановить файлы из ближайших бэкапов *.bak_*.")

print("=" * 72)
sys.exit(1 if problems else 0)