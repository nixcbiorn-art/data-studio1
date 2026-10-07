"""
collect_pool.py — параллельный запуск сбора источников для redcat_scraper.

Два независимых пула:
  • HTTP-источники     — ThreadPoolExecutor (I/O-bound, requests отпускает GIL).
  • browser-источники  — ProcessPoolExecutor (Playwright, свой Chrome-профиль
                          на источник в browser_profiles/<key>/, изоляция
                          процессов — упавший/зависший Chromium не задевает
                          остальные источники).

Плюс — ограничение параллельности ПО РЕАЛЬНОМУ ХОСТУ, а не только по
режиму http/browser: если несколько источников физически ходят в один и
тот же бэкенд (разные срезы одного API под разными URL), общий пул из
20-30 воркеров ударит по этому бэкенду всей массой сразу — риск бана или
троттлинга растёт вместе с числом источников. Поэтому у каждого хоста
есть свой отдельный семафор с лимитом (по умолчанию 3 одновременных
запроса на хост), общий на всех воркеров процесса — вне зависимости от
того, в скольких потоках/процессах эти воркеры физически исполняются.
Настроить лимит для конкретного хоста можно файлом host_limits.json
рядом со скриптом (см. _load_host_overrides).

Волны (waves): источник, зависящий от другого (через spec.split_values_from
ИЛИ spec.depends_on), не может стартовать раньше, чем зависимость
соберётся. Поэтому источники обходятся волнами в топологическом порядке,
а не одним плоским пулом.

ВАЖНО (Windows / multiprocessing):
  На Windows используется spawn — дочерний процесс заново импортирует
  этот модуль и redcat_scraper.py. Убедитесь, что вызов main() обёрнут в
  `if __name__ == "__main__":`, и что импорт redcat_scraper.py не имеет
  побочных эффектов на уровне модуля.
"""

from __future__ import annotations

from redcat.core import paths
import json
import logging
import urllib.parse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from multiprocessing import Manager
from pathlib import Path

import requests

DEFAULT_HTTP_WORKERS = 25
DEFAULT_BROWSER_WORKERS = 4
DEFAULT_PER_HOST_WORKERS = 3

# Необязательный файл вида {"api.developer-x.ru": 1, "some-other.host": 5} —
# переопределяет лимит для конкретных хостов. Если файла нет — используется
# DEFAULT_PER_HOST_WORKERS для всех.
HOST_LIMITS_FILE = paths.ROOT / "host_limits.json"


def _get_collect_source():
    # Отложенный импорт — чтобы не создавать циклическую зависимость
    # (redcat_scraper.py импортирует collect_pool, а не наоборот).
    from redcat.collection import redcat_scraper as rs
    return rs.collect_source


def _fetch_mode(spec) -> str:
    return getattr(spec, "fetch_mode", "http") or "http"


def _resolve_host(spec, params: dict) -> str:
    """Реальный хост источника — по нему группируем лимит параллельности.

    Если URL не резолвится (не хватает плейсхолдеров и т.п.) — источник
    получает собственную изолированную группу, чтобы ошибка резолва не
    смешала его с чужим лимитом по ошибке.
    """
    try:
        url = spec.resolved_url(params)
        host = urllib.parse.urlparse(url).netloc.lower()
        return host or f"__isolated__:{spec.key}"
    except Exception as e:  # noqa: BLE001
        logging.warning("collect_pool: не удалось определить хост для %s (%s) — "
                        "источник получит отдельный лимит", spec.key, e)
        return f"__isolated__:{spec.key}"


def _load_host_overrides() -> dict:
    if HOST_LIMITS_FILE.exists():
        try:
            return json.loads(HOST_LIMITS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logging.warning("collect_pool: host_limits.json не прочитан (%s), "
                            "использую лимит по умолчанию для всех хостов.", e)
    return {}


def _build_waves(specs: list) -> list[list]:
    """Волны с учётом ОБЕИХ зависимостей: split_values_from и depends_on.

    Волна N выполняется только после того, как волна N-1 целиком
    завершилась и её результаты попали в `raw`.
    """
    by_key = {s.key: s for s in specs}
    remaining = list(specs)
    done: set[str] = set()
    waves: list[list] = []

    def deps_of(spec) -> list[str]:
        d = list(getattr(spec, "depends_on", None) or [])
        if spec.split_values_from:
            d.append(spec.split_values_from)
        return d

    while remaining:
        wave = [
            s for s in remaining
            if all(dep in done or dep not in by_key for dep in deps_of(s))
        ]
        if not wave:
            logging.warning(
                "collect_pool: не удалось построить порядок волн для %s — "
                "обрабатываю оставшееся одной волной (проверьте depends_on "
                "на циклы)",
                [s.key for s in remaining],
            )
            wave = remaining

        waves.append(wave)
        done.update(s.key for s in wave)
        remaining = [s for s in remaining if s not in wave]

    return waves


def _make_session(spec, token: str) -> requests.Session:
    session = requests.Session()
    if getattr(spec, "external", False):
        session.headers.update({"Accept": "application/json"})
    else:
        session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })
    return session


def _run_http_worker(spec, token, params, collected_snapshot, since, host_sem):
    """Выполняется в потоке (ThreadPoolExecutor)."""
    collect_source = _get_collect_source()
    session = _make_session(spec, token)
    since_map = {spec.key: since} if since else None
    host_sem.acquire()
    try:
        rows, inc, total = collect_source(
            spec, session, token, params, collected_snapshot)
        return spec.key, rows, inc, total, None
    except Exception as e:  # noqa: BLE001 — один источник не должен ронять остальные
        logging.exception("[%s] сбор упал", spec.key)
        return spec.key, [], True, None, f"{type(e).__name__}: {e}"
    finally:
        host_sem.release()
        session.close()


def _run_browser_worker(spec, token, params, collected_snapshot, since, host_sem):
    """Выполняется в отдельном процессе (ProcessPoolExecutor).

    `host_sem` — прокси-семафор из multiprocessing.Manager, он корректно
    синхронизируется между процессами (и потоками тоже), в отличие от
    обычного threading.Semaphore.
    """
    collect_source = _get_collect_source()
    since_map = {spec.key: since} if since else None
    host_sem.acquire()
    try:
        rows, inc, total = collect_source(
            spec, None, token, params, collected_snapshot)
        return spec.key, rows, inc, total, None
    except Exception as e:  # noqa: BLE001
        logging.exception("[%s] браузерный сбор упал", spec.key)
        return spec.key, [], True, None, f"{type(e).__name__}: {e}"
    finally:
        host_sem.release()


def run_collection(
    specs: list,
    token: str,
    params: dict,
    http_workers: int = DEFAULT_HTTP_WORKERS,
    browser_workers: int = DEFAULT_BROWSER_WORKERS,
    per_host_workers: int = DEFAULT_PER_HOST_WORKERS,
    since_map: dict | None = None,
):
    """Параллельная замена шага «1. Сбор» в main().

    since_map — {spec.key: "значение для incremental_param"} для источников,
    которые в этом прогоне собираются инкрементально (см. incremental_state.py
    и main() в redcat_scraper.py). Источники не из since_map собираются
    как обычно, целиком.

    Возвращает (raw, totals, any_incomplete, incomplete_by_spec, errors).
    """
    since_map = since_map or {}
    raw: dict = {}
    totals: dict = {}
    incomplete_by_spec: dict = {}
    errors: dict = {}
    any_incomplete = False

    waves = _build_waves(specs)
    logging.info("collect_pool: %d волн(а/ы), источников всего: %d",
                 len(waves), len(specs))

    host_overrides = _load_host_overrides()
    manager = Manager()
    host_semaphores: dict = {}

    def sem_for(spec):
        host = _resolve_host(spec, params)
        if host not in host_semaphores:
            limit = int(host_overrides.get(host, per_host_workers))
            host_semaphores[host] = manager.Semaphore(max(1, limit))
        return host_semaphores[host]

    for wave_num, wave in enumerate(waves, 1):
        http_specs = [s for s in wave if _fetch_mode(s) != "browser"]
        browser_specs = [s for s in wave if _fetch_mode(s) == "browser"]

        print(f"  🌊 Волна {wave_num}/{len(waves)}: "
              f"{len(http_specs)} HTTP, {len(browser_specs)} браузер")

        futures = {}
        with ThreadPoolExecutor(max_workers=max(1, http_workers)) as http_pool, \
             ProcessPoolExecutor(max_workers=max(1, browser_workers)) as browser_pool:

            snapshot = dict(raw)  # снимок на момент старта волны

            for spec in http_specs:
                fut = http_pool.submit(
                    _run_http_worker, spec, token, params, snapshot,
                    since_map.get(spec.key), sem_for(spec))
                futures[fut] = spec

            for spec in browser_specs:
                fut = browser_pool.submit(
                    _run_browser_worker, spec, token, params, snapshot,
                    since_map.get(spec.key), sem_for(spec))
                futures[fut] = spec

            for fut in as_completed(futures):
                spec = futures[fut]
                key, rows, inc, total, err = fut.result()

                if err is not None:
                    errors[key] = err
                    any_incomplete = True
                    print(f"  ❌ [{key}] {err}")
                    continue

                if not rows and inc:
                    print(f"  ⚠️ [{key}] не собран — история и данные прошлого "
                          f"запуска сохранены, сравнение и аномалии по нему "
                          f"пропущены.")
                    incomplete_by_spec[key] = inc
                    any_incomplete = True
                    continue

                raw[key] = rows
                totals[key] = total
                incomplete_by_spec[key] = inc
                any_incomplete |= inc
                tag = " (инкремент)" if key in since_map else ""
                print(f"  ✅ [{key}] собрано: {len(rows)}{tag}")

    return raw, totals, any_incomplete, incomplete_by_spec, errors