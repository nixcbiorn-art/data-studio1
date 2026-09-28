"""
RedCat Scraper — универсальный сборщик данных по API
====================================================
Собирает любые описанные источники, нормализует, сравнивает с прошлым
запуском, ищет аномалии, проверяет качество данных и выгружает всё в
SQLite/Parquet + HTML-дашборд.

Источники описываются декларативно в папках:
  • sources/           — Redcat (ходит с Bearer-токеном)
  • sources_external/  — внешние (свой режим сбора, своя база)

Быстрый старт:
  pip install -r requirements.txt
  python redcat_scraper.py --token "eyJ...ваш_токен"

Полезные флаги:
  --list-sources          показать все зарегистрированные источники
  --only apartments       собрать только указанные (зависимости подтянутся)
  --only fsk_apartments   собирает внешний источник без Redcat-токена
  --excel                 дополнительно записать .xlsx
  --no-anomalies          пропустить поиск аномалий
  --sensitivity 2.5       чувствительность аномалий (меньше = строже)
  --fail-on-critical      выйти с кодом 2 при критичных аномалиях

Токен НИКОГДА не хранится в скрипте: --token или REDCAT_TOKEN в .env.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
from dataclasses import replace as _dc_replace
from datetime import datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────
#  ЗАВИСИМОСТИ
# ──────────────────────────────────────────────────────────────
REQUIRED_PACKAGES = {
    "aiohttp": "aiohttp",
    "requests": "requests",
    "pandas": "pandas",
}


def missing_dependencies() -> list:
    return [pkg for module, pkg in REQUIRED_PACKAGES.items()
            if importlib.util.find_spec(module) is None]


def print_dependency_help(missing) -> None:
    print("❌ Для сбора данных не хватает библиотек: " + ", ".join(missing))
    print("\n   Установите все зависимости одной командой:")
    print("       pip install -r requirements.txt")
    print(f"\n   Или только недостающие:")
    print(f"       pip install {' '.join(missing)}")
    print("\n   ℹ️ Просмотр и анализ уже собранных данных работает без них:")
    print("       python studio.py")


_MISSING = missing_dependencies()

try:
    import requests
except ImportError:
    requests = None

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    import storage
except ImportError:
    storage = None

# Модуль виртуального браузера — необязательная зависимость.
try:
    import browser_fetch
except ImportError:
    browser_fetch = None

import anomalies as anomaly_lib
import api_guard
import rate_limit as _rl
import quality
import report_html
import run_stats
import sources as src

api_guard.install()

_TOKEN_SOURCE = None
_ENV_FILE_USED = None
_SHADOWED_OS_TOKEN = False
_ENV_BAD_LINES = []


def clean_token(raw):
    t = re.sub(r"\s+", "", raw or "").strip("\"'")
    if t.lower().startswith("bearer"):
        t = t[6:]
    return t.strip("\"'")


_os_token_before = clean_token(os.environ.get("REDCAT_TOKEN", ""))
try:
    from dotenv import dotenv_values, find_dotenv

    class _DotenvWarnCatcher(logging.Handler):
        def emit(self, record):
            m = re.search(r"line (\d+)", record.getMessage())
            if m:
                _ENV_BAD_LINES.append(int(m.group(1)))

    _dl = logging.getLogger("dotenv.main")
    _dl.addHandler(_DotenvWarnCatcher())
    _dl.propagate = False

    _env_path = find_dotenv()
    _vals = dotenv_values(_env_path) if _env_path else {}
    for _k, _v in _vals.items():
        if _v is not None and _k not in os.environ:
            os.environ[_k] = _v
    _env_token = clean_token(_vals.get("REDCAT_TOKEN"))
    if _env_token and "вставьте" not in _env_token.lower():
        os.environ["REDCAT_TOKEN"] = _env_token
        _TOKEN_SOURCE, _ENV_FILE_USED = ".env", _env_path
        _SHADOWED_OS_TOKEN = bool(_os_token_before) and _os_token_before != _env_token
    elif os.environ.get("REDCAT_TOKEN"):
        _TOKEN_SOURCE = "окружение ОС"
except ImportError:
    if _os_token_before:
        _TOKEN_SOURCE = "окружение ОС"

# ──────────────────────────────────────────────────────────────
#  ПУТИ
# ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
HISTORY_DIR = BASE_DIR / "history"
OUTPUT_DIR = BASE_DIR / "reports"
SOURCES_DIR = BASE_DIR / "sources"
SOURCES_EXT_DIR = BASE_DIR / "sources_external"
HISTORY_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

DATA_DB = OUTPUT_DIR / "redcat_data.db"
EXTERNAL_DB = OUTPUT_DIR / "external_data.db"
STATS_DB = HISTORY_DIR / "run_stats.db"
DASHBOARD_FILE = OUTPUT_DIR / "dashboard.html"
STUDIO_DB = BASE_DIR / "studio.db"

try:
    import studio_store as _studio_store

    _studio_store.init(STUDIO_DB)
    api_guard.set_audit_hook(
        lambda m, u, s, ms, note="": _studio_store.log_api(STUDIO_DB, m, u, s, ms, note))
except Exception:  # noqa: BLE001
    pass

logging.basicConfig(
    filename=str(BASE_DIR / "redcat_scraper.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)

REQUEST_TIMEOUT = int(os.environ.get("REDCAT_TIMEOUT", "20"))
MAX_RETRIES = int(os.environ.get("REDCAT_MAX_RETRIES", "3"))
RETRY_DELAY_SEC = 2
KEEP_RECORD_HISTORY_RUNS = int(os.environ.get("REDCAT_KEEP_HISTORY_RUNS", "30"))


# ──────────────────────────────────────────────────────────────
#  ГЛОБАЛЬНЫЕ СТРУКТУРЫ ЗАЩИТЫ ОТ БАНА
# ──────────────────────────────────────────────────────────────
DEFAULT_RATE_RPS = float(os.environ.get("REDCAT_RATE_LIMIT_RPS", "0") or "0")
DEFAULT_CIRCUIT_FAILURES = int(os.environ.get("REDCAT_CIRCUIT_FAILURES", "8") or "8")
DEFAULT_CIRCUIT_COOLDOWN = int(os.environ.get("REDCAT_CIRCUIT_COOLDOWN", "300") or "300")

_RATE_LIMITERS = _rl.HostRateLimiters(default_rps=DEFAULT_RATE_RPS)
_CIRCUIT_BREAKERS: dict = {}
_BREAKER_LOCK = threading.Lock()
if DEFAULT_RATE_RPS > 0:
    print(f"🐢 Лимит частоты: {DEFAULT_RATE_RPS:g} запросов/сек на хост "
          f"(можно отключить: REDCAT_RATE_LIMIT_RPS=0 в .env)")


def _breaker_for(spec) -> _rl.CircuitBreaker:
    """Получить (или создать) circuit breaker для источника."""
    key = getattr(spec, "key", "unknown")
    with _BREAKER_LOCK:
        cb = _CIRCUIT_BREAKERS.get(key)
        if cb is None:
            fails = getattr(spec, "circuit_breaker_failures", 0) \
                or DEFAULT_CIRCUIT_FAILURES
            cool = getattr(spec, "circuit_breaker_cooldown_sec", 0) \
                or DEFAULT_CIRCUIT_COOLDOWN
            cb = _rl.CircuitBreaker(failures=fails, cooldown_sec=cool)
            _CIRCUIT_BREAKERS[key] = cb
        return cb


def _rps_for(spec) -> float:
    """Сколько запросов в секунду разрешено этому источнику. 0 = без лимита."""
    rps = float(getattr(spec, "rate_limit_rps", 0.0) or 0.0)
    if rps > 0:
        return rps
    return DEFAULT_RATE_RPS


def circuit_breaker_snapshot() -> dict:
    """Снимок состояния всех breaker'ов — для логов и отладки."""
    with _BREAKER_LOCK:
        return {k: cb.state() for k, cb in _CIRCUIT_BREAKERS.items()}


# ──────────────────────────────────────────────────────────────
#  РЕГИСТРАЦИЯ FETCH-СТРАТЕГИЙ
# ──────────────────────────────────────────────────────────────
# Фетчеры обёрнуты в защиту от бана:
#   • CircuitBreaker — если источник подряд отдаёт ошибки, временно
#     отключаем его (source пропускается, сбор помечается неполным);
#   • TokenBucket — N запросов в секунду на хост (по умолчанию выключено,
#     включается REDCAT_RATE_LIMIT_RPS в .env или rate_limit_rps в spec);
#   • Retry-After — при HTTP 429 читаем заголовок и ждём указанное время.
@src.register_fetcher("http")
def _fetch_http_sync(url, spec, session=None, **kwargs):
    if session is None:
        raise ValueError(
            "http-фетчеру нужна session=requests.Session(). "
            "Передайте её через src.fetch_raw(..., session=session).")

    # Circuit breaker
    cb = _breaker_for(spec)
    if cb.is_open():
        state = cb.state()
        return {"status": None, "body": None, "headers": {},
                "error": (f"circuit breaker открыт ({state['last_reason']}); "
                          f"ещё {state['open_remaining_sec']:.0f} сек до повтора"),
                "error_kind": "circuit_open"}

    # Rate limiter
    rps = _rps_for(spec)
    wait = _RATE_LIMITERS.try_consume(url, rps)
    if wait > 0:
        time.sleep(min(wait, 30.0))

    try:
        resp = session.get(url, timeout=kwargs.get("timeout", REQUEST_TIMEOUT))
        headers = dict(resp.headers)
        status = resp.status_code

        if status == 429:
            ra = _rl.parse_retry_after(headers, default=3.0)
            _RATE_LIMITERS.on_rate_limit(url, ra, rps)
            cb.record_failure("HTTP 429")
            # Небольшая пауза прямо в фетчере, чтобы вызывающий не сделал
            # немедленный retry и не усугубил.
            time.sleep(min(ra, 60.0))
        elif 500 <= status < 600:
            cb.record_failure(f"HTTP {status}")
        elif 200 <= status < 400:
            cb.record_success()
        # 4xx кроме 429 — наша ошибка, breaker не трогаем.

        return {"status": status, "body": resp.content,
                "headers": headers, "error": None}
    except requests.exceptions.RequestException as e:
        cb.record_failure(type(e).__name__)
        return {"status": None, "body": None, "headers": {},
                "error": str(e), "error_kind": "network"}


@src.register_async_fetcher("http")
async def _fetch_http_async(url, spec, session=None, **kwargs):
    if session is None:
        raise ValueError(
            "async http-фетчеру нужна session=aiohttp.ClientSession().")

    cb = _breaker_for(spec)
    if cb.is_open():
        state = cb.state()
        return {"status": None, "body": None, "headers": {},
                "error": (f"circuit breaker открыт ({state['last_reason']}); "
                          f"ещё {state['open_remaining_sec']:.0f} сек до повтора"),
                "error_kind": "circuit_open"}

    rps = _rps_for(spec)
    wait = _RATE_LIMITERS.try_consume(url, rps)
    if wait > 0:
        await asyncio.sleep(min(wait, 30.0))

    try:
        async with session.get(url) as resp:
            body = await resp.read()
            headers = dict(resp.headers)
            status = resp.status

            if status == 429:
                ra = _rl.parse_retry_after(headers, default=3.0)
                _RATE_LIMITERS.on_rate_limit(url, ra, rps)
                cb.record_failure("HTTP 429")
                await asyncio.sleep(min(ra, 60.0))
            elif 500 <= status < 600:
                cb.record_failure(f"HTTP {status}")
            elif 200 <= status < 400:
                cb.record_success()

            return {"status": status, "body": body,
                    "headers": headers, "error": None}
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        cb.record_failure(type(e).__name__)
        return {"status": None, "body": None, "headers": {},
                "error": str(e), "error_kind": "network"}

# ──────────────────────────────────────────────────────────────
#  СБОР ДАННЫХ
# ──────────────────────────────────────────────────────────────
TOTAL_PATHS = (
    ("meta", "total"),
    ("meta", "total_count"),
    ("meta", "count"),
    ("meta", "pagination", "total"),
    ("meta", "pagination", "total_count"),
    ("meta", "page", "total"),
    ("pagination", "total"),
    ("total",),
    ("total_count",),
    ("count",),
)


def find_total(data, spec):
    value = src.dig(data, spec.total_path)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    for path in TOTAL_PATHS:
        value = src.dig(data, path)
        if isinstance(value, int) and not isinstance(value, bool):
            logging.info("[%s] total найден по пути %s (в описании указан %s).",
                         spec.key, path, spec.total_path)
            return value
    return None


# Реализация перенесена в sources.py — доступна как src.set_query_param.
# Алиас сохранён, чтобы не трогать существующие вызовы по всему файлу.
_set_query_param = src.set_query_param


def _next_url(data, spec, current_url, page_number, got_rows, collected, total):
    """Определяет адрес следующей страницы.

    Делегирует в src.compute_next_url — там реестр стратегий пагинации
    (auto / next_link / page_number / offset / cursor / stop). data — это
    raw payload ответа (обычно то, что вернул парсер в поле "raw").
    """
    return src.compute_next_url(data, spec, current_url, page_number,
                                got_rows, collected, total)


def _window_limit_hint(body) -> bool:
    text = (body or "").lower()
    return any(marker in text for marker in (
        "result window", "from + size", "max_result_window", "too_many_buckets",
        "window is too large"))


def _looks_deterministic(body: str) -> bool:
    low = (body or "").lower()
    markers = (
        "sqlstate", "syntax error", "illuminate\\database", "unknown column",
        "undefined column", "no such column", "traceback (most recent call last)",
        "fatal error", "pdoexception", "errorexception", "typeerror:",
        "undefined method", "call to a member function", "division by zero",
        "argumentcounterror", "column not found", "invalid column",
    )
    return any(m in low for m in markers)


def fetch_pages(base_url, session, spec, label="", max_pages=100000,
                treat_as_complete=True):
    """Синхронный обход пагинации. Возвращает (записи, incomplete, total)."""
    url = _set_query_param(
        _set_query_param(base_url, spec.page_size_param, spec.page_size),
        spec.page_number_param,
        0 if "offset" in (spec.page_number_param or "").lower() else 1)
    all_data, page, incomplete, reported_total = [], 1, False, None
    seen_ids, stalled, visited, capped = set(), False, set(), False

    print(f"  📡 [{label}] Загрузка...", end=" ", flush=True)
    while url and page <= max_pages:
        if url in visited:
            logging.warning("[%s] повтор адреса страницы — обход остановлен: %s",
                            label, url)
            break
        visited.add(url)

        data = None
        for attempt in range(1, MAX_RETRIES + 1):
            raw = src.fetch_raw(url, spec, session=session)
            if raw.get("error"):
                if raw.get("error_kind") == "circuit_open":
                    print(f"\n  ⛔ [{label}] {raw['error']}. "
                          f"Источник пропускается, повторите запуск позже.")
                    incomplete = True
                    data = None
                    break
                logging.warning("[%s] стр.%d попытка %d/%d: %s",
                                label, page, attempt, MAX_RETRIES, raw["error"])
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY_SEC * attempt)
                else:
                    incomplete = True
                continue

            status = raw.get("status")
            if status is not None and status >= 400:
                body = (raw.get("body") or b"").decode("utf-8", errors="replace")[:800]
                if _window_limit_hint(body):
                    print(f"\n  ⛔ [{label}] API упёрся в лимит окна пагинации "
                          f"на странице {page} (собрано {len(all_data)}).")
                    logging.error("[%s] лимит окна пагинации: %s", label, body)
                    incomplete = True
                    data = None
                    break

                print(f"\n  ❌ [{label}] стр.{page}: HTTP {status}")
                print(f"     Тело ответа API: {body[:400] or '(пусто)'}")
                logging.error("[%s] стр.%d: HTTP %d — %s",
                             label, page, status, body)

                if _looks_deterministic(body):
                    print(f"  ⛔ Похоже на поломку самого запроса.")
                    incomplete = True
                    data = None
                    break

                if attempt < MAX_RETRIES:
                    print(f"     Повтор {attempt}/{MAX_RETRIES}...")
                    time.sleep(RETRY_DELAY_SEC * attempt)
                    continue
                incomplete = True
                data = None
                break

            try:
                data = src.parse_payload(raw.get("body"), spec)
            except ValueError as e:
                logging.error("[%s] стр.%d: не удалось разобрать ответ: %s",
                              label, page, e)
                print(f"\n  ❌ [{label}] стр.{page}: ответ не разобран: {e}")
                incomplete = True
                data = None
                break
            break
        if data is None:
            break

        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            logging.warning("[%s] стр.%d: в ответе нет списка записей.",
                            label, page)
            break

        page_ids = {str(it.get(spec.id_field)) for it in items
                    if isinstance(it, dict) and it.get(spec.id_field) is not None}
        if page_ids and page > 1 and page_ids <= seen_ids:
            print(f"\n  ⚠️ [{label}] страница {page} повторяет предыдущую — "
                  f"обход остановлен, собрано {len(all_data)}.")
            logging.warning("[%s] пагинация не двигается на странице %d", label, page)
            stalled = incomplete = True
            break
        seen_ids |= page_ids

        all_data.extend(items)
        if page % 10 == 0 or page == 1:
            print(f"[{page}:{len(all_data)}]", end=" ", flush=True)

        if reported_total is None:
            reported_total = data.get("total") if isinstance(data, dict) else None
        if reported_total is None:
            reported_total = find_total(data.get("raw") if isinstance(data, dict) else data,
                                        spec)

        if spec.max_records and len(all_data) >= spec.max_records:
            del all_data[spec.max_records:]
            capped = True
            break

        url = _next_url(data.get("raw") if isinstance(data, dict) else data,
                        spec, url, page, len(items),
                        len(all_data), reported_total)
        if url:
            page += 1
            # Если rate limiter активен — он сам рулит паузами.
            # Иначе оставляем старый предохранитель от долбёжки.
            if _rps_for(spec) <= 0:
                time.sleep(0.15)

    if capped:
        note = f" из {reported_total} по данным API" if reported_total else ""
        print(f"\n  ✂️ [{label}] Срез по настройке max_records: взято первых "
              f"{len(all_data)}{note}.")
        print(f"✅ записей: {len(all_data)} (страниц: {page})")
        if treat_as_complete:
            # ПРИМЕНЯЕМ ПРЕПРОЦЕССОР: единый для всех источников,
            # включая те, что без split_param. Раньше он вызывался
            # только в ветке дробления.
            if getattr(spec, 'preprocess', None) and spec.preprocess in src.PREPROCESSORS:
                all_data = src.PREPROCESSORS[spec.preprocess](
                    {'items': all_data}, None)
            # ДЕДУП: API отдаёт один лот под разными клиент-каналами.
            if getattr(spec, 'id_field', None):
                _seen, _uniq = set(), []
                for _r in all_data:
                    _rid = _r.get(spec.id_field) if isinstance(_r, dict) else None
                    if _rid is None:
                        _uniq.append(_r); continue
                    _k = str(_rid)
                    if _k in _seen: continue
                    _seen.add(_k); _uniq.append(_r)
                all_data = _uniq
            return all_data, False, len(all_data)
        print(f"  ⚠️ [{label}] аварийный срез — запуск помечен неполным.")
        # ПРИМЕНЯЕМ ПРЕПРОЦЕССОР: единый для всех источников,
        # включая те, что без split_param. Раньше он вызывался
        # только в ветке дробления.
        if getattr(spec, 'preprocess', None) and spec.preprocess in src.PREPROCESSORS:
            all_data = src.PREPROCESSORS[spec.preprocess](
                {'items': all_data}, None)
        # ДЕДУП: API отдаёт один лот под разными клиент-каналами.
        if getattr(spec, 'id_field', None):
            _seen, _uniq = set(), []
            for _r in all_data:
                _rid = _r.get(spec.id_field) if isinstance(_r, dict) else None
                if _rid is None:
                    _uniq.append(_r); continue
                _k = str(_rid)
                if _k in _seen: continue
                _seen.add(_k); _uniq.append(_r)
            all_data = _uniq
        return all_data, True, reported_total

    if reported_total is not None and len(all_data) < reported_total and not stalled:
        incomplete = True
        print(f"\n  ⚠️ [{label}] собрано {len(all_data)} из заявленных "
              f"{reported_total} — не хватает {reported_total - len(all_data)}.")

    print(f"{'⚠️ ЧАСТИЧНО' if incomplete else '✅'} записей: {len(all_data)}"
          f" (страниц: {page})")
    # ПРИМЕНЯЕМ ПРЕПРОЦЕССОР: единый для всех источников,
    # включая те, что без split_param. Раньше он вызывался
    # только в ветке дробления.
    if getattr(spec, 'preprocess', None) and spec.preprocess in src.PREPROCESSORS:
        all_data = src.PREPROCESSORS[spec.preprocess](
            {'items': all_data}, None)
    # ДЕДУП: API отдаёт один лот под разными клиент-каналами.
    if getattr(spec, 'id_field', None):
        _seen, _uniq = set(), []
        for _r in all_data:
            _rid = _r.get(spec.id_field) if isinstance(_r, dict) else None
            if _rid is None:
                _uniq.append(_r); continue
            _k = str(_rid)
            if _k in _seen: continue
            _seen.add(_k); _uniq.append(_r)
        all_data = _uniq
    return all_data, incomplete, reported_total


async def _fetch_json(url, session, label, page, max_attempts=None, spec=None):
    status, body_snippet = None, None
    attempts = max_attempts or MAX_RETRIES
    for attempt in range(1, attempts + 1):
        raw = await src.async_fetch_raw(url, spec, session=session)
        if raw.get("error"):
            if raw.get("error_kind") == "circuit_open":
                logging.warning("[%s] стр.%d: %s",
                                label, page, raw["error"])
                return None, None, "circuit_open"
            logging.warning("[%s] стр.%d попытка %d/%d: %s",
                            label, page, attempt, attempts, raw["error"])
            if attempt < attempts:
                await asyncio.sleep(RETRY_DELAY_SEC * attempt)
                continue
            return None, status, body_snippet

        status = raw.get("status")
        if status is not None and status >= 400:
            body_snippet = (raw.get("body") or b"").decode(
                "utf-8", errors="replace")[:500]
            logging.warning("[%s] стр.%d попытка %d/%d: HTTP %d — %s",
                            label, page, attempt, attempts, status, body_snippet)
            if status in (401, 403) or _looks_deterministic(body_snippet):
                return None, status, body_snippet
            if attempt < attempts:
                backoff = RETRY_DELAY_SEC * attempt * (3 if status == 429 else 1)
                await asyncio.sleep(backoff)
            continue

        if spec is None:
            # Резервный путь без стратегии парсинга (совместимость с preflight)
            import json as _json
            try:
                return _json.loads((raw.get("body") or b"").decode(
                    "utf-8", errors="replace")), status, None
            except ValueError as e:
                return None, status, str(e)

        try:
            return src.parse_payload(raw.get("body"), spec), status, None
        except ValueError as e:
            logging.warning("[%s] стр.%d: не удалось разобрать ответ: %s",
                            label, page, e)
            return None, status, str(e)
    return None, status, body_snippet


ES_WINDOW = 10_000


def _get_field(row, field):
    if not isinstance(row, dict) or not field:
        return None
    return src.dig(row, tuple(field.split("."))) if "." in field else row.get(field)


def _norm_name(v):
    return " ".join(str(v).split()).casefold() if v not in (None, "") else None


def _total_of(data, spec):
    """Общее число записей из ответа _fetch_json (unified или сырой JSON)."""
    if not isinstance(data, dict):
        return None
    if "items" in data and "raw" in data:
        total = data.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            return total
        raw = data.get("raw")
        return find_total(raw, spec) if raw is not None else None
    return find_total(data, spec)


def _rows_of(data, spec):
    """Список записей из ответа _fetch_json (unified или сырой JSON)."""
    if not isinstance(data, dict):
        return None
    if "items" in data and "raw" in data:
        items = data.get("items")
    else:
        items = src.dig(data, spec.data_path)
    return items if isinstance(items, list) else None


async def preflight_check_split(base_url, sample_values, token, spec):
    """Быстрая проверка параметра дробления на нескольких значениях.

    Возвращает (ok, текст). ok=True, если фильтр реально фильтрует выдачу.
    """
    if not sample_values:
        return True, None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    sep = "&" if "?" in base_url else "?"
    results = []

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        # 1. Без фильтра: сколько всего записей в регионе.
        unfiltered_url = (f"{base_url}{sep}{spec.page_size_param}=1"
                          f"&{spec.page_number_param}=1")
        base_data, _, _ = await _fetch_json(
            unfiltered_url, session, f"preflight:{spec.key}", 0, spec=spec)
        unfiltered_total = _total_of(base_data, spec)

        # 2. Несколько значений фильтра. Два сбоя подряд — прекращаем.
        failed_in_row = 0
        for value in sample_values:
            url = (f"{base_url}{sep}{spec.split_param}={value}"
                   f"&{spec.page_size_param}={spec.page_size}"
                   f"&{spec.page_number_param}=1")
            data, status, body = await _fetch_json(
                url, session, f"preflight:{spec.key}", 1,
                max_attempts=1, spec=spec)
            rows = _rows_of(data, spec)
            results.append({
                "value": value, "ok": data is not None,
                "status": status, "body": body,
                "rows": len(rows) if rows is not None else None,
                "total": _total_of(data, spec),
            })
            failed_in_row = 0 if data is not None else failed_in_row + 1
            if failed_in_row >= 2:
                break

    responded = [r for r in results if r["ok"]]
    if not responded:
        lines = [f"Проверка на {len(results)} значениях провалилась целиком:"]
        for r in results:
            lines.append(f"    • {spec.split_param}={r['value']} → HTTP {r['status']}"
                         + (f": {r['body']}" if r["body"] else " (без тела ответа)"))
        return False, "\n".join(lines)

    # Фильтр игнорируется: с ним столько же записей, сколько без него.
    if unfiltered_total:
        ignored = [r for r in responded
                   if r["total"] is not None and r["total"] == unfiltered_total]
        if len(ignored) == len(responded):
            return False, (
                f"Фильтр «{spec.split_param}» не влияет на выдачу: и без него, и с\n"
                f"    любым из {len(responded)} проверенных значений API отдаёт одни и те же\n"
                f"    {unfiltered_total} записей.")

    empty = [r for r in responded if not r["rows"]]
    if len(empty) == len(responded):
        return False, (
            f"Все {len(responded)} проверенных значений вернули 0 записей без ошибок.")

    with_data = len(responded) - len(empty)
    return True, (f"фильтр работает: из {len(responded)} проб с данными {with_data}"
                  + (f", всего по региону {unfiltered_total}" if unfiltered_total else ""))


async def _fetch_one_split(value, session, semaphore, spec, base_url):
    label = f"{spec.key}:{value}"
    sep = "&" if "?" in base_url else "?"
    if spec.split_url_template:
        raw_url = spec.split_url_template.replace("{value}", str(value))
        url = _set_query_param(
            _set_query_param(raw_url, spec.page_size_param, spec.page_size),
            spec.page_number_param,
            0 if "offset" in (spec.page_number_param or "").lower() else 1)
    else:
        url = (f"{base_url}{sep}{spec.split_param}={value}"
               f"&{spec.page_size_param}={spec.page_size}"
               f"&{spec.page_number_param}=1")
    rows, page, incomplete, total, last_status = [], 1, False, None, None

    async with semaphore:
        while url:
            data, status, _body = await _fetch_json(
                url, session, label, page,
                max_attempts=2 if page == 1 else None, spec=spec)
            if status is not None:
                last_status = status
            if data is None:
                if page == 1 and status is not None and status >= 500:
                    break
                incomplete = True
                break

            if spec.preprocess and spec.preprocess in src.PREPROCESSORS:
                items = src.PREPROCESSORS[spec.preprocess](data.get("raw"), value)
            else:
                items = data.get("items") or []
            if not isinstance(items, list):
                logging.warning("[%s] стр.%d: в ответе нет списка записей.",
                                label, page)
                break
            rows.extend(items)
            if total is None:
                total = data.get("total")
            if total is None:
                total = find_total(data.get("raw"), spec)
            if len(rows) >= ES_WINDOW and total is not None and total > len(rows):
                url = None
            else:
                url = _next_url(data.get("raw"), spec, url, page,
                                len(items), len(rows), total)
            if url:
                page += 1
                await asyncio.sleep(0.05)

    if total is not None and len(rows) < total:
        logging.warning(
            "[%s] собрано %d из %d — похоже, лимит пагинации достигнут даже для "
            "одного значения split.", label, len(rows), total)
        incomplete = True
    return rows, incomplete, last_status


async def fetch_split_async(base_url, values, token, spec):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
    semaphore = asyncio.Semaphore(spec.concurrency)
    out, any_incomplete, done, failed, empty_ok, suspect = [], False, 0, 0, 0, 0
    status_counts = {}
    total = len(values)

    print(f"  📡 [{spec.key}] дробление по {spec.split_param}: "
          f"{total} значений, параллельность {spec.concurrency}")

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        tasks = [asyncio.create_task(
            _fetch_one_split(v, session, semaphore, spec, base_url)) for v in values]
        for coro in asyncio.as_completed(tasks):
            rows, inc, status = await coro
            out.extend(rows)
            any_incomplete |= inc
            done += 1
            if inc:
                failed += 1
                if status is not None:
                    status_counts[status] = status_counts.get(status, 0) + 1
            elif not rows:
                if status is not None and status >= 500:
                    suspect += 1
                else:
                    empty_ok += 1
            if done % 25 == 0 or done == total:
                print(f"\r  📡 [{spec.key}] {done}/{total} — записей: {len(out)}",
                      end="", flush=True)
    print()

    ok = total - failed - empty_ok - suspect
    print(f"  {'⚠️ ЧАСТИЧНО' if any_incomplete else '✅'} всего: {len(out)} записей. "
          f"Из {total} значений: с данными {ok}, пустых (без ошибки) {empty_ok}, "
          f"HTTP 5xx без данных {suspect}, с ошибкой {failed}.")
    if suspect:
        print(f"  ℹ️ У {suspect} значений сервер ответил HTTP 5xx без данных — "
              f"похоже на ЖК без лотов.")

    if failed:
        detail = ", ".join(f"HTTP {code}: {cnt}" for code, cnt in sorted(status_counts.items()))
        print(f"  ⚠️ Ошибки при обходе ({failed} из {total}): "
              f"{detail or 'без кода статуса — таймаут или обрыв сети'}.")
        if status_counts.get(429):
            print(f"  💡 HTTP 429 = превышен лимит частоты запросов. Уменьшите "
                  f"\"concurrency\" у источника '{spec.key}' в sources/*.json "
                  f"(сейчас {spec.concurrency}).")

    return out, any_incomplete, suspect


def probe_live_values(url, session, spec, parent=None, limit=3):
    by_name = {}
    if spec.split_child_name_field and spec.split_values_name_field and parent:
        for r in parent:
            n, v = _norm_name(_get_field(r, spec.split_values_name_field)), _get_field(r, spec.split_values_field)
            if n and v is not None:
                by_name.setdefault(n, v)
    field = re.sub(r"^filter\[(.+?)\](\[\])*$", r"\1", spec.split_param or "")
    if not by_name and not field:
        return []
    sep = "&" if "?" in url else "?"
    try:
        raw = src.fetch_raw(
            f"{url}{sep}{spec.page_size_param}={spec.page_size}"
            f"&{spec.page_number_param}=1",
            spec, session=session)
        if raw.get("error") or (raw.get("status") or 0) >= 400:
            return []
        parsed = src.parse_payload(raw.get("body"), spec)
        rows = parsed.get("items") or []
    except ValueError:
        return []
    live = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        if by_name:
            v = by_name.get(_norm_name(_get_field(r, spec.split_child_name_field)))
        else:
            v = _get_field(r, field)
        if v is not None and v not in live:
            live.append(v)
            if len(live) >= limit:
                break
    return live


def probe_total(url, session, spec):
    sep = "&" if "?" in url else "?"
    probe_url = f"{url}{sep}{spec.page_size_param}=1&{spec.page_number_param}=1"
    raw = src.fetch_raw(probe_url, spec, session=session)
    if raw.get("error"):
        logging.warning("[%s] не удалось получить total: %s",
                        spec.key, raw["error"])
        print(f"  ⚠️ [{spec.key}] не удалось получить общий total "
              f"({raw['error']}) — сверка полноты сбора будет недоступна.")
        return None

    status = raw.get("status")
    if status is not None and status >= 400:
        body = (raw.get("body") or b"").decode("utf-8", errors="replace")[:800]
        print(f"  ❌ [{spec.key}] пробный запрос вернул HTTP {status}")
        print(f"     Тело ответа API: {body[:400] or '(пусто)'}")
        logging.error("[%s] пробный запрос: HTTP %d — %s",
                      spec.key, status, body)
        return None

    try:
        parsed = src.parse_payload(raw.get("body"), spec)
        total = parsed.get("total")
        if total is None:
            total = find_total(parsed.get("raw"), spec)
    except ValueError as e:
        logging.warning("[%s] ответ не распарсился: %s", spec.key, e)
        return None

    if total is None:
        print(f"  ⚠️ [{spec.key}] в ответе не нашлось общего числа записей — "
              f"сверить полноту сбора будет не с чем.")
    return total


# ──────────────────────────────────────────────────────────────
#  РЕЖИМ БРАУЗЕРА (для источников с fetch_mode="browser")
# ──────────────────────────────────────────────────────────────
def _extract_items_from_html(html, spec):
    """Ищет записи в HTML. Возвращает список dict-ов или None."""
    import re as _re
    m = _re.search(
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, _re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            found = src.dig(data, spec.data_path) if spec.data_path else None
            if isinstance(found, list):
                return found
        except (ValueError, KeyError):
            pass
    for m in _re.finditer(
            r'<script[^>]+type="application/json"[^>]*>(.+?)</script>',
            html, _re.S):
        try:
            data = json.loads(m.group(1))
            found = src.dig(data, spec.data_path) if spec.data_path else None
            if isinstance(found, list):
                return found
        except (ValueError, KeyError):
            continue
    try:
        data = json.loads(html)
        found = src.dig(data, spec.data_path) if spec.data_path else data
        if isinstance(found, list):
            return found
    except ValueError:
        pass
    return None


def _collect_source_browser_sync(spec, base_url):
    """Sync-обход одной вкладкой. Возвращает (rows, incomplete, total)."""
    is_offset = "offset" in (spec.page_number_param or "").lower()
    first_page = 0 if is_offset else 1
    all_rows: list = []
    seen_ids: set = set()
    page = first_page
    total = None
    empty_streak = 0
    max_pages = 1000
    stopped_reason = "?"

    try:
        with browser_fetch.BrowserFetcher(
                spec.key, headless=spec.browser_headless) as fetcher:
            while page - first_page < max_pages:
                url = _set_query_param(
                    _set_query_param(base_url, spec.page_size_param, spec.page_size),
                    spec.page_number_param, page)
                label = f"{spec.key}:p{page}"
                try:
                    data = fetcher.fetch_json(
                        url, wait_for=spec.browser_wait_for,
                        wait_ms=spec.browser_wait_ms)
                except Exception as e:  # noqa: BLE001
                    print(f"\n  ❌ [{label}] ошибка браузера: "
                          f"{type(e).__name__}: {e}")
                    stopped_reason = f"ошибка браузера на p{page}"
                    break

                if not isinstance(data, dict):
                    print(f"\n  ⚠️ [{label}] страница вернула не JSON "
                          f"(вероятно, Qrator-челлендж снова). Останавливаюсь.")
                    stopped_reason = f"не-JSON на p{page} (Qrator?)"
                    break

                if total is None:
                    total = find_total(data, spec)

                items = src.dig(data, spec.data_path)
                if not isinstance(items, list):
                    print(f"\n  ⚠️ [{label}] по пути {spec.data_path} нет списка")
                    stopped_reason = f"нет items на p{page}"
                    break

                fresh = 0
                for r in items:
                    rid = r.get(spec.id_field) if isinstance(r, dict) else None
                    if rid is None or str(rid) not in seen_ids:
                        all_rows.append(r)
                        fresh += 1
                        if rid is not None:
                            seen_ids.add(str(rid))

                print(f"  📡 [{label}] +{fresh} (всего: {len(all_rows)}"
                      + (f" из {total}" if total else "") + ")")

                if not items:
                    empty_streak += 1
                    if empty_streak >= 2:
                        stopped_reason = f"две пустых страницы подряд (p{page})"
                        break
                    page += spec.page_size if is_offset else 1
                    continue
                empty_streak = 0

                if total and len(all_rows) >= total:
                    stopped_reason = f"собрано всё ({len(all_rows)} из {total})"
                    break

                # Шаг всегда на spec.page_size, а не на len(items) — так мы
                # не пропустим куски, если API отдаёт страницы короче.
                page += spec.page_size if is_offset else 1

        print(f"  ℹ️ [{spec.key}] остановлено: {stopped_reason}")
    except Exception as e:  # noqa: BLE001
        print(f"\n  ❌ [{spec.key}] браузер упал: {type(e).__name__}: {e}")
        logging.exception("[%s] browser error", spec.key)
        return all_rows, True, total

    return all_rows, False, total


async def _fetch_pages_browser_async(spec, base_url, concurrency: int = 4):
    """Параллельный обход страниц через async-Playwright.

    Простая параллель по фиксированным offset работает только если
    сервер отдаёт страницы одного размера. У samolet.ru размер плавает
    (36, 30, 20...), поэтому здесь сделано иначе: диапазон [0, total]
    разбивается на `concurrency` сегментов, и внутри каждого обход идёт
    ПОСЛЕДОВАТЕЛЬНО, с offset += фактически полученное число записей.
    Сегменты обрабатываются параллельно, потерь на границах нет.
    """
    if not (getattr(browser_fetch, "_PW_ASYNC_AVAILABLE", False)
            and hasattr(browser_fetch, "AsyncBrowserFetcher")):
        return [], True, None

    is_offset = "offset" in (spec.page_number_param or "").lower()
    first_page = 0 if is_offset else 1

    async with browser_fetch.AsyncBrowserFetcher(
            spec.key, headless=spec.browser_headless) as fetcher:

        # 1. Первая страница — прогрев Qrator + узнаём total
        first_url = _set_query_param(
            _set_query_param(base_url, spec.page_size_param, spec.page_size),
            spec.page_number_param, first_page)
        first_data = await fetcher.fetch_json(
            first_url, wait_ms=spec.browser_wait_ms)
        if first_data is None:
            print(f"  ⚠️ [{spec.key}] первая страница не отдала JSON")
            return [], True, None

        first_items = src.dig(first_data, spec.data_path)
        if not isinstance(first_items, list):
            return [], True, None

        total = find_total(first_data, spec)
        all_rows: list = list(first_items)
        seen_ids = {str(r.get(spec.id_field)) for r in first_items
                    if isinstance(r, dict) and r.get(spec.id_field) is not None}
        print(f"  📡 [{spec.key}:p{first_page}] +{len(first_items)} "
              f"(всего: {len(all_rows)}"
              + (f" из {total}" if total else "") + ")")

        if not total or not is_offset:
            # Без total или без offset — откатываемся на обычный параллельный
            # обход по фиксированному шагу (для источников со стабильной
            # пагинацией он работает нормально).
            print(f"  ℹ️ [{spec.key}] total неизвестен — параллельный обход "
                  f"без гарантий полноты")
            return all_rows, True, total

        # 2. Разбиваем на сегменты и обходим каждый последовательно
        starts = [first_page + i * (total // concurrency)
                  for i in range(concurrency)]
        # первый сегмент уже частично пройден на первой странице
        starts[0] = first_page

        sem = asyncio.Semaphore(concurrency)
        lock = asyncio.Lock()

        async def _segment(start: int, end: int):
            offset = start
            while offset < end:
                url = _set_query_param(
                    _set_query_param(base_url, spec.page_size_param,
                                     spec.page_size),
                    spec.page_number_param, offset)
                data = await fetcher.fetch_json(
                    url, wait_ms=spec.browser_wait_ms)
                if data is None:
                    # пропуск страницы — Qrator или временный сбой
                    offset += spec.page_size
                    continue
                items = src.dig(data, spec.data_path)
                if not isinstance(items, list) or not items:
                    break
                async with lock:
                    for r in items:
                        rid = r.get(spec.id_field) if isinstance(r, dict) else None
                        if rid is not None:
                            k = str(rid)
                            if k in seen_ids:
                                continue
                            seen_ids.add(k)
                        all_rows.append(r)
                # шагаем по фактическому размеру страницы — это главное
                offset += len(items) if is_offset else 1

        async def _one(start, end):
            async with sem:
                await _segment(start, end)

        tasks = []
        for i in range(concurrency):
            s = starts[i]
            e = starts[i + 1] if i + 1 < concurrency else total
            tasks.append(asyncio.create_task(_one(s, e)))

        done = 0
        for fut in asyncio.as_completed(tasks):
            await fut
            done += 1
            print(f"\r  📡 [{spec.key}] сегмент {done}/{concurrency} готов, "
                  f"записей: {len(all_rows)}", end="", flush=True)
        print()

    incomplete = bool(total and len(all_rows) < total * 0.99)
    return all_rows, incomplete, total


def collect_source(spec, session, token, params, collected):
    """Собирает один источник целиком. Возвращает (сырые_записи, incomplete, total).

    Диспетчер:
      • fetch_mode="browser" — уходит в collect_source_browser (Playwright);
      • есть split_param и split_values_from — обходит по дроблению;
      • иначе — обычный HTTP GET с пагинацией.
    """
    if getattr(spec, "fetch_mode", "http") == "browser":
        if getattr(spec, "fetch_strategy", "") == "browser_xhr":
            return _collect_source_browser_xhr(spec, params)
        return collect_source_browser(spec, session, token, params, collected)

    url = spec.resolved_url(params)

    # Дробление запроса по значениям поля (обход лимита окна пагинации).
    if spec.split_param and spec.split_values_from:
        parent = collected.get(spec.split_values_from)
        if not parent:
            logging.error("[%s] источник-родитель '%s' пуст — пропуск.",
                          spec.key, spec.split_values_from)
            print(f"  ⚠️ [{spec.key}] нет данных родителя "
                  f"'{spec.split_values_from}' — пропускаем.")
            return [], True, None

        values = {_get_field(r, spec.split_values_field) for r in parent
                  if _get_field(r, spec.split_values_field) is not None}
        values |= set(spec.split_values_extra or ())
        values = sorted(values, key=str)

        total = probe_total(url, session, spec)
        if total is not None:
            print(f"  ℹ️ [{spec.key}] API заявляет всего: {total}")

        live = probe_live_values(url, session, spec, parent)
        sample = live[:3] or values[:min(5, len(values))]
        if live:
            print(f"  ℹ️ [{spec.key}] Проверочные значения: "
                  f"{', '.join(str(v) for v in live[:3])}")
        print(f"  🔎 [{spec.key}] Проверяю параметр дробления '{spec.split_param}' "
              f"(несколько быстрых запросов)...")
        ok, diag = asyncio.run(preflight_check_split(url, sample, token, spec))
        if not ok:
            print(f"  ❌ [{spec.key}] Предпроверка '{spec.split_param}' не пройдена:")
            print(f"     {diag}")
            if spec.max_records:
                print(f"  ↩️ [{spec.key}] Дробление недоступно — перехожу на срез: "
                      f"первые {spec.max_records} записей.")
                logging.warning("[%s] дробление недоступно, собираю срез "
                                "max_records=%s", spec.key, spec.max_records)
                spec.collected_as_slice = True
                return fetch_pages(url, session, spec, spec.key,
                                   treat_as_complete=False)
            print(f"  ⛔ Сбор источника '{spec.key}' остановлен.")
            logging.error("[%s] preflight провален:\n%s", spec.key, diag)
            return [], True, total
        if diag:
            print(f"  ✅ [{spec.key}] предпроверка пройдена — {diag}")

        rows, inc, suspect = asyncio.run(fetch_split_async(url, values, token, spec))

        # ДЕДУП: API отдаёт одну и ту же квартиру под разными каналами
        # (FSK / FSK_APP / DSK / DSK_ADVERTISE). В split-режиме каждая
        # копия приходит отдельно, нужно схлопнуть по id_field.
        _dedup_removed = 0
        if getattr(spec, "id_field", None) and rows:
            _seen, _uniq = set(), []
            for _r in rows:
                _rid = _r.get(spec.id_field) if isinstance(_r, dict) else None
                if _rid is None:
                    _uniq.append(_r)
                    continue
                _k = str(_rid)
                if _k in _seen:
                    continue
                _seen.add(_k)
                _uniq.append(_r)
            _dedup_removed = len(rows) - len(_uniq)
            if _dedup_removed:
                print(f"  🧹 [{spec.key}] убрано дублей по {spec.id_field}: "
                      f"{_dedup_removed}")
            rows = _uniq

        if suspect and total is None:
            inc = True
        elif suspect and total is not None and len(rows) >= total:
            pass
        elif suspect:
            inc = True

        # total от API считает копии по каналам; после дедупа rows меньше
        # total — это нормально, а не потеря данных.
        if total is not None and len(rows) < total and not _dedup_removed:
            share = 100.0 * len(rows) / total
            print(f"  собрано {len(rows)} из {total} ({share:.1f}%)")
            if share < 80:
                inc = True

        return rows, inc, total

    # Обычный HTTP GET с пагинацией.
    return fetch_pages(url, session, spec, spec.key)



def _collect_source_browser_xhr(spec, params):
    """Сбор источника через перехват XHR.

    Открывает страницу, слушает XHR/fetch, берёт первый JSON-ответ,
    подходящий под spec.browser_xhr_pattern, парсит его стратегией
    spec.parse_strategy и возвращает записи. Постраничный обход тут
    не делается: если источник пагинируется, обычно все данные приходят
    одним XHR-ответом (или несколькими — тогда нужна отдельная стратегия
    обхода, которая добавится позже).
    """
    try:
        base_url = spec.resolved_url(params)
    except ValueError as e:
        print(f"  ❌ [{spec.key}] {e}")
        return [], True, None

    if browser_fetch is None:
        print(f"  ❌ [{spec.key}] fetch_mode=browser, но browser_fetch.py "
              f"не найден или Playwright не установлен.")
        return [], True, None

    mode = "видимое окно" if not spec.browser_headless else "headless"
    print(f"  🌐 [{spec.key}] browser_xhr ({mode}); "
          f"паттерн: {spec.browser_xhr_pattern or '(любой JSON)'}")

    raw = src.fetch_raw(base_url, spec)   # fetcher открывается/закрывается сам
    if raw.get("error"):
        print(f"  ❌ [{spec.key}] XHR: {raw['error']}")
        return [], True, None

    try:
        parsed = src.parse_payload(raw.get("body"), spec)
    except ValueError as e:
        print(f"  ❌ [{spec.key}] ответ XHR не разобран: {e}")
        return [], True, None

    items = parsed.get("items") or []
    total = parsed.get("total")
    print(f"  ✅ [{spec.key}] XHR: записей {len(items)}"
          + (f" из {total}" if total else ""))
    incomplete = bool(total and len(items) < total)
    return items, incomplete, total


def collect_source_browser(spec, session, token, params, collected):
    """Сбор источника через Playwright.

    Если concurrency > 1 — параллельный async-обход; Qrator-челлендж
    проходится один раз, cookie общая. При concurrency == 1 — sync-обход
    одной вкладкой.
    """
    if browser_fetch is None:
        print(f"  ❌ [{spec.key}] fetch_mode=browser, но browser_fetch.py "
              f"не найден или Playwright не установлен.")
        return [], True, None

    try:
        base_url = spec.resolved_url(params)
    except ValueError as e:
        print(f"  ❌ [{spec.key}] {e}")
        return [], True, None

    concurrency = max(1, int(getattr(spec, "concurrency", 1) or 1))
    mode = "видимое окно" if not spec.browser_headless else "headless"
    print(f"  🌐 [{spec.key}] браузер ({mode}), параллельность {concurrency}")
    logging.info("[%s] browser mode: %s, concurrency=%d",
                 spec.key, base_url, concurrency)

    has_async = bool(getattr(browser_fetch, "_PW_ASYNC_AVAILABLE", False))
    has_async_fetcher = hasattr(browser_fetch, "AsyncBrowserFetcher")

    if concurrency <= 1 or not (has_async and has_async_fetcher):
        if not (has_async and has_async_fetcher):
            print(f"  ℹ️ [{spec.key}] async Playwright недоступен — "
                  f"обход пойдёт по одной странице (sync). Для параллельного "
                  f"обхода добавьте AsyncBrowserFetcher в browser_fetch.py.")
        return _collect_source_browser_sync(spec, base_url)

    try:
        rows, incomplete, total = asyncio.run(
            _fetch_pages_browser_async(spec, base_url, concurrency=concurrency))
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ [{spec.key}] async-обход упал: {type(e).__name__}: {e}")
        logging.exception("[%s] async browser error", spec.key)
        return [], True, None

    print(f"  ✅ [{spec.key}] всего собрано: {len(rows)}"
          + (f" из {total}" if total else ""))
    return rows, incomplete, total


# ──────────────────────────────────────────────────────────────
#  СНАПШОТЫ И СРАВНЕНИЕ
# ──────────────────────────────────────────────────────────────
def load_snapshot(name):
    path = HISTORY_DIR / f"{name}.json"
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logging.warning("Снапшот %s не прочитан: %s", name, e)
    return {}


def save_snapshot(name, snapshot):
    with open(HISTORY_DIR / f"{name}.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, default=str)


def compare_snapshots(old, new_rows, spec):
    label = spec.title or spec.key
    new_snap = {str(r[spec.id_field]): r for r in new_rows
                if r.get(spec.id_field) is not None}
    old_ids, new_ids = set(old), set(new_snap)
    changes = []

    for rid in sorted(new_ids - old_ids):
        changes.append({"Категория": f"{label}: Новая запись", "ID": rid,
                        "Название": new_snap[rid].get(spec.name_field),
                        "Поле": "", "Было": "", "Стало": ""})
    for rid in sorted(old_ids - new_ids):
        changes.append({"Категория": f"{label}: Запись пропала", "ID": rid,
                        "Название": old[rid].get(spec.name_field),
                        "Поле": "", "Было": "", "Стало": ""})
    for rid in sorted(old_ids & new_ids):
        for fld in spec.track_fields:
            o, n = old[rid].get(fld), new_snap[rid].get(fld)
            if str(o) != str(n):
                changes.append({"Категория": f"{label}: Изменение поля", "ID": rid,
                                "Название": new_snap[rid].get(spec.name_field),
                                "Поле": fld, "Было": o, "Стало": n})
    return changes, new_snap


# ──────────────────────────────────────────────────────────────
#  ТОКЕН
# ──────────────────────────────────────────────────────────────
def decode_jwt_payload(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None


def resolve_token(cli_token):
    oneshot = os.environ.get("REDCAT_TOKEN_ONESHOT", "").strip()
    token = clean_token(cli_token or oneshot or os.environ.get("REDCAT_TOKEN", ""))
    if cli_token:
        source = "--token"
    elif oneshot:
        source = "разовый токен (веб-форма)"
    else:
        source = _TOKEN_SOURCE or "REDCAT_TOKEN"
    if not token:
        print("❌ ОШИБКА: токен не передан.")
        print('   python redcat_scraper.py --token "eyJ..."  либо REDCAT_TOKEN в .env')
        sys.exit(1)

    where = f"{source}: {_ENV_FILE_USED}" if source == ".env" and _ENV_FILE_USED else source
    print(f"🔑 Источник токена — {where}")
    if _ENV_BAD_LINES:
        lines_txt = ", ".join(str(n) for n in sorted(set(_ENV_BAD_LINES)))
        print(f"⚠️ В .env не разобраны строки: {lines_txt} — они игнорируются.")
        print("   Что именно не так: python check_env.py")
    if token.count(".") != 2:
        print("⚠️ Токен не похож на JWT (нужны три части через точку).")
    if _SHADOWED_OS_TOKEN and source == ".env":
        print("⚠️ В окружении ОС задан другой REDCAT_TOKEN — он игнорируется.")

    claims = decode_jwt_payload(token)
    if claims and "exp" in claims:
        try:
            exp = datetime.fromtimestamp(claims["exp"])
            if exp < datetime.now():
                print(f"❌ Срок действия токена истёк {exp:%d.%m.%Y %H:%M}.")
                sys.exit(1)
            left = exp - datetime.now()
            print(f"🔑 Токен действителен до {exp:%d.%m.%Y %H:%M} (осталось {left}).")
            if left.total_seconds() < 24 * 3600:
                print("⚠️ Токен скоро истечёт — обновите его в .env.")
        except (OSError, OverflowError, ValueError):
            pass
    return token


# ──────────────────────────────────────────────────────────────
#  MAIN
# ──────────────────────────────────────────────────────────────
def build_arg_parser():
    p = argparse.ArgumentParser(description="RedCat Scraper — универсальный сборщик по API")
    p.add_argument("--token", default=None, help="Токен на этот запуск (перекрывает .env)")
    p.add_argument("--only", nargs="+", metavar="SOURCE",
                   help="Собрать только эти источники (зависимости подтянутся)")
    p.add_argument("--list-sources", action="store_true",
                   help="Показать зарегистрированные источники и выйти")
    p.add_argument("--excel", action="store_true",
                   help="Дополнительно записать .xlsx")
    p.add_argument("--no-anomalies", action="store_true", help="Пропустить поиск аномалий")
    p.add_argument("--sensitivity", type=float, default=anomaly_lib.DEFAULT_Z,
                   help=f"Порог z-score для аномалий (по умолчанию {anomaly_lib.DEFAULT_Z})")
    p.add_argument("--concurrency", type=int, default=None,
                   help="Переопределить параллельность дробящихся источников")
    p.add_argument("--diagnose", nargs="*", metavar="SOURCE",
                   help="Проверить источники запрос за запросом")
    p.add_argument("--fail-on-critical", action="store_true",
                   help="Выйти с кодом 2 при критичных аномалиях")
    return p


def run_diagnostics(keys, cli_token, params) -> int:
    import diagnose as diag

    token = resolve_token(cli_token)
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}",
                            "Accept": "application/json"})

    specs = src.all_sources(only=keys or None)
    if not specs:
        print("❌ Нет источников для проверки.")
        return 1

    print("\n🩺 ДИАГНОСТИКА ИСТОЧНИКОВ")
    print(f"   Проверяю: {', '.join(s.key for s in specs)}")

    parents, reports = {}, []
    for spec in specs:
        if spec.split_values_from and spec.split_values_from not in parents:
            parent = src.get(spec.split_values_from)
            if parent:
                try:
                    url = parent.resolved_url(params)
                    sep = "&" if "?" in url else "?"
                    resp = session.get(
                        f"{url}{sep}{parent.page_size_param}=25"
                        f"&{parent.page_number_param}=1", timeout=REQUEST_TIMEOUT)
                    rows = src.dig(resp.json(), parent.data_path)
                    parents[spec.split_values_from] = rows if isinstance(rows, list) else []
                except Exception as e:  # noqa: BLE001
                    logging.warning("Диагностика: родитель %s недоступен: %s",
                                    spec.split_values_from, e)
                    parents[spec.split_values_from] = []

        report = diag.diagnose_source(spec, session, params, DATA_DB,
                                      find_total, parents)
        report.print()
        reports.append(report)

    broken = [r for r in reports if r.problems]
    print(f"\n{'═' * 62}")
    if broken:
        print(f"❌ Поломок найдено: {', '.join(r.key for r in broken)}")
        return 2
    risky = [r for r in reports if any(l["level"] == "⚠️" for l in r.lines)]
    if risky:
        print(f"⚠️ Поломок нет, но есть предупреждения: "
              f"{', '.join(r.key for r in risky)}")
        return 0
    print("✅ Все источники проверены, замечаний нет.")
    return 0


def main():
    args = build_arg_parser().parse_args()
    run_started_at = datetime.now()

    loaded = src.load_from_dir(SOURCES_DIR)
    loaded_ext = src.load_from_dir(SOURCES_EXT_DIR)
    if loaded_ext:
        print(f"📦 Внешних источников загружено: {loaded_ext} "
              f"(папка {SOURCES_EXT_DIR.name}/)")
    params = src.env_params()

    if args.list_sources:
        print(f"Зарегистрировано источников: {loaded + loaded_ext}\n")
        for s in src.all_sources():
            tag = ""
            if s.external:
                tag = " [внешний]"
            elif s.split_values_from:
                tag = f"  ← дробится по {s.split_values_from}"
            elif getattr(s, "fetch_mode", "http") == "browser":
                tag = "  🌐 браузер"
            print(f"  • {s.key:20} {s.title or ''}{tag}")
            try:
                print(f"    {s.resolved_url(params)[:110]}")
            except ValueError as e:
                print(f"    ⚠️ {e}")
        if _MISSING:
            print(f"\n⚠️ Для самого сбора не хватает: {', '.join(_MISSING)}")
        return 0

    if _MISSING:
        print_dependency_help(_MISSING)
        return 1

    if args.diagnose is not None:
        return run_diagnostics(args.diagnose, args.token, params)

    print("🚀 REDCAT SCRAPER")
    print("=" * 56)
    print(api_guard.status_text())
    print(f"📋 Источников загружено: {loaded + loaded_ext}")

    specs_all = src.all_sources(only=args.only)
    redcat_specs = [s for s in specs_all if not s.external]

    if redcat_specs:
        token = resolve_token(args.token)
    else:
        token = ""
        print("🔓 Собираются только внешние источники — Redcat-токен не требуется.")

    redcat_session = requests.Session()
    redcat_session.headers.update({"Authorization": f"Bearer {token}",
                                   "Accept": "application/json"})
    external_session = requests.Session()
    external_session.headers.update({"Accept": "application/json"})

    timestamp = run_started_at.strftime("%Y%m%d_%H%M")
    specs = specs_all
    if not specs:
        print("❌ Нет источников для сбора. Проверьте папки sources/, "
              "sources_external/ и --only.")
        return 1
    if args.concurrency:
        for spec in specs:
            if spec.split_param:
                spec.concurrency = args.concurrency
        print(f"  ⚙️ Параллельность переопределена: {args.concurrency}")

    # ── 1. Сбор ──
    print(f"\n1️⃣ Сбор данных ({len(specs)} источник(ов))...")
    raw, normalized, totals, any_incomplete = {}, {}, {}, False
    incomplete_by_spec = {}
    for spec in specs:
        session = external_session if spec.external else redcat_session
        try:
            rows, inc, total = collect_source(spec, session, token, params, raw)
        except ValueError as e:
            print(f"  ❌ [{spec.key}] {e}")
            logging.error("[%s] %s", spec.key, e)
            any_incomplete = True
            continue
        if not rows and inc:
            print(f"  ⚠️ [{spec.key}] не собран — история и данные прошлого "
                  f"запуска сохранены, сравнение и аномалии по нему пропущены.")
            any_incomplete = True
            continue
        raw[spec.key] = rows
        totals[spec.key] = total
        any_incomplete |= inc
        incomplete_by_spec[spec.key] = inc
        normalized[spec.key] = src.normalize(rows, spec)

    if not normalized:
        print("❌ Не собрано ни одного источника.")
        return 1

    # ── 2. Сравнение с прошлым запуском ──
    print("\n2️⃣ Сравнение с прошлым запуском...")
    all_changes, new_snapshots, prev_snapshots = [], {}, {}
    for spec in specs:
        if spec.key not in normalized:
            continue
        prev = load_snapshot(spec.key)
        prev_snapshots[spec.key] = prev
        if incomplete_by_spec.get(spec.key):
            print(f"  ⚠️ {spec.key}: сбор неполный — сравнение пропущено.")
            continue
        changes, snap = compare_snapshots(prev, normalized[spec.key], spec)
        new_snapshots[spec.key] = snap
        all_changes.extend(changes)
        if prev:
            print(f"  • {spec.key}: изменений {len(changes)}")
    is_first_run = not any(prev_snapshots.values())
    if is_first_run:
        print("  ℹ️ Первый запуск — сравнивать не с чем.")
    else:
        print(f"  🔄 Всего изменений: {len(all_changes)}")

    # ── 3. Качество данных ──
    print("\n3️⃣ Проверка качества данных...")
    quality_issues, profiles = [], {}
    for spec in specs:
        if spec.key not in normalized:
            continue
        prof = quality.profile(normalized[spec.key])
        profiles[spec.key] = prof
        old_prof = quality.load_profile(STATS_DB, spec.key)
        quality_issues.extend(quality.compare_profiles(old_prof, prof, spec.key))
    print(f"  🔍 Замечаний по схеме и заполненности: {len(quality_issues)}")

    # ── 4. Поиск аномалий ──
    found = []
    if not args.no_anomalies:
        print("\n4️⃣ Поиск аномалий...")
        found.extend(quality_issues)
        for spec in specs:
            if spec.key not in normalized:
                continue
            rows = normalized[spec.key]
            found.extend(anomaly_lib.detect_data_anomalies(
                rows, spec, z_threshold=args.sensitivity))
            found.extend(anomaly_lib.detect_identical_value_clusters(
                rows, spec))
            if not incomplete_by_spec.get(spec.key):
                found.extend(anomaly_lib.detect_record_change_anomalies(
                    prev_snapshots.get(spec.key), rows, spec))
                found.extend(anomaly_lib.detect_group_disappearance(
                    prev_snapshots.get(spec.key), rows, spec))
        print(f"  🔎 Найдено на уровне данных: {len(found)}")
    else:
        print("\n4️⃣ Поиск аномалий пропущен (--no-anomalies).")

    # ── 5. Выгрузка ──
    print("\n5️⃣ Сохранение данных...")
    skipped_notes = [
        {"Категория": f"{(src.get(key).title or key)}: сравнение пропущено",
         "ID": "", "Название": "", "Поле": "",
         "Было": "источник собран не полностью", "Стало": ""}
        for key in incomplete_by_spec if incomplete_by_spec.get(key) and key in normalized
    ]
    comparison_rows = all_changes + skipped_notes or [{"Информация":
        "Первый запуск — сравнивать не с чем" if is_first_run else "Изменений не найдено"}]

    redcat_tables = {k: v for k, v in normalized.items()
                     if not (src.get(k) and src.get(k).external)}
    external_tables = {k: v for k, v in normalized.items()
                       if src.get(k) and src.get(k).external}
    redcat_tables["comparison_vs_previous"] = comparison_rows
    if external_tables:
        external_tables["comparison_vs_previous"] = comparison_rows

    files_written = []
    storage.write_sqlite(DATA_DB, redcat_tables)
    files_written.append(DATA_DB)
    print(f"  🗄️ SQLite (Redcat): {DATA_DB.name}")
    if external_tables:
        storage.write_sqlite(EXTERNAL_DB, external_tables)
        files_written.append(EXTERNAL_DB)
        print(f"  🗄️ SQLite (внешние): {EXTERNAL_DB.name}")

    columnar = storage.write_columnar(OUTPUT_DIR, redcat_tables, timestamp)
    if external_tables:
        columnar += storage.write_columnar(OUTPUT_DIR, external_tables,
                                           f"{timestamp}_ext")
    files_written.extend(columnar)
    if columnar:
        kind = "Parquet" if columnar[0].suffix == ".parquet" else "CSV"
        print(f"  📦 {kind}: {len(columnar)} файл(ов)")

    if args.excel:
        ok, warns = storage.write_excel(
            OUTPUT_DIR / f"redcat_export_{timestamp}.xlsx",
            {k[:31]: v for k, v in redcat_tables.items()})
        for w in warns:
            print(f"  ⚠️ {w}")
        if ok:
            print("  📊 Excel записан")

    for key, snap in new_snapshots.items():
        save_snapshot(key, snap)

    try:
        import hc_crosscheck
        apt_spec = next((sp for sp in specs if sp.key == "apartments"), None) or src.get("apartments")
        hc_crosscheck.run_from_scraper(normalized, load_snapshot, apt_spec, OUTPUT_DIR, timestamp)
    except Exception:  # noqa: BLE001
        logging.exception("Перекрёстная проверка ЖК не выполнена")
        print("  ⚠️ Перекрёстная проверка ЖК не выполнена — подробности в redcat_scraper.log")

    # ── 6. Метрики запуска ──
    print("\n6️⃣ История запусков...")
    primary = max(normalized, key=lambda k: len(normalized[k]))
    metrics = run_stats.compute_generic_metrics(
        normalized=normalized, totals=totals, changes=all_changes,
        incomplete=any_incomplete, started_at=run_started_at,
        finished_at=datetime.now(), primary=primary,
        primary_spec=src.get(primary),
        all_specs={sp.key: sp for sp in specs},
    )
    run_id = run_stats.save_run(STATS_DB, metrics)

    for spec in specs:
        if spec.key in profiles:
            quality.save_profile(STATS_DB, spec.key, run_id, profiles[spec.key])
        if spec.key in normalized and spec.numeric_fields:
            storage.append_record_history(
                STATS_DB, spec.key, run_id, normalized[spec.key],
                spec.id_field, spec.numeric_fields,
                run_started_at.isoformat(timespec="seconds"))
    storage.prune_record_history(STATS_DB, KEEP_RECORD_HISTORY_RUNS)

    history = run_stats.load_history(STATS_DB, limit=100)

    if not args.no_anomalies:
        found.extend(anomaly_lib.detect_history_anomalies(
            history, z_threshold=args.sensitivity))
        for spec in specs:
            if spec.key not in normalized or incomplete_by_spec.get(spec.key):
                continue
            found.extend(anomaly_lib.detect_frozen_records(
                STATS_DB, spec.key, normalized[spec.key], spec))
    summary = anomaly_lib.summarize(found)
    storage.save_anomalies(STATS_DB, run_id, summary["items"],
                           run_started_at.isoformat(timespec="seconds"))
    print(f"  📈 Запуск #{run_id} записан (всего запусков: {len(history)}).")

    # ── 7. Дашборд ──
    dashboard = report_html.build_dashboard(
        history, DASHBOARD_FILE, files_written, summary,
        storage.load_anomaly_counts(STATS_DB))
    if dashboard:
        print(f"  📊 Дашборд: {dashboard}")

    # ── Итог ──
    print("\n" + "=" * 56)
    for key, rows in normalized.items():
        tag = " [внешний]" if (src.get(key) and src.get(key).external) else ""
        delta = run_stats.format_delta(history, f"{key}_count") if len(history) > 1 else ""
        print(f"   {key}: {len(rows)}{delta}{tag}")
    counts = summary["counts"]
    if summary["total"]:
        print(f"\n🔎 Аномалии: критичных {counts.get('critical', 0)}, "
              f"предупреждений {counts.get('warning', 0)}, "
              f"информационных {counts.get('info', 0)}")
        for a in summary["items"][:5]:
            icon = {"critical": "🔴", "warning": "🟡"}.get(a["severity"], "🔵")
            print(f"   {icon} {a['message']}")
        if summary["total"] > 5:
            print(f"   … ещё {summary['total'] - 5} — смотрите дашборд.")
    else:
        print("\n🔎 Аномалий не обнаружено.")

    if any_incomplete:
        print("\n⚠️ Часть данных не догрузилась — запуск помечен как неполный. "
              "Подробности в redcat_scraper.log")

    if args.fail_on_critical and counts.get("critical"):
        print("\n❌ Есть критичные аномалии — выход с кодом 2.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())