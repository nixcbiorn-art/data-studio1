"""web_hub_scheduler — единый поток + отдельный runner на задачу.

Каждая задача (job) получает свой ScraperRunner. Это позволяет двум
задачам работать одновременно: например, пока идёт долгий сбор RedCat,
быстро отрабатывает правило задач.

Внутренние задачи (tasks_rules) работают в самом scheduler-потоке —
они лёгкие. Внешние (scraper) — в отдельном subprocess через
свой ScraperRunner.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from redcat.web import web_hub
from redcat.web.web_runners import ScraperRunner

_log = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop_flag = threading.Event()
CHECK_INTERVAL = 20
JOB_TIMEOUT = 3600 * 4

# По одному runner'у на job_key
_runners: dict[str, ScraperRunner] = {}
_runners_lock = threading.Lock()
# Занятые внутренние задачи (по key) — чтобы не запустить дважды
_internal_running: set[str] = set()
_internal_lock = threading.Lock()


def _get_runner(key: str) -> ScraperRunner:
    with _runners_lock:
        if key not in _runners:
            _runners[key] = ScraperRunner()
        return _runners[key]


def get_all_runners_status() -> dict:
    """{job_key: {running, started_at, ...}}."""
    with _runners_lock:
        return {
            k: {
                "running": r.running,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
                "exit_code": r.exit_code,
                "command": list(r.command or []),
            }
            for k, r in _runners.items()
        }


def is_any_running() -> bool:
    with _runners_lock:
        return any(r.running for r in _runners.values())


def _due(job: dict) -> bool:
    if not job.get("enabled"):
        return False
    last = job.get("last_run")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except (ValueError, TypeError):
        return True
    interval = int(job.get("interval") or 60)
    return (datetime.now() - last_dt).total_seconds() >= interval * 60


# ── Внутренние задачи ──
def _run_internal(func_name: str) -> dict:
    if func_name == "tasks_rules":
        from redcat.web.web_mail_task_rules import apply_rules
        return apply_rules(days=90, limit=2000)
    return {"error": f"неизвестная функция: {func_name}"}


def _run_internal_job(key: str, func_name: str) -> dict:
    with _internal_lock:
        if key in _internal_running:
            return {"skipped": True, "reason": "уже идёт"}
        _internal_running.add(key)
    try:
        return _run_internal(func_name)
    finally:
        with _internal_lock:
            _internal_running.discard(key)


# ── Внешние задачи (subprocess) ──
def _run_scraper_job(key: str, args: list) -> dict:
    runner = _get_runner(key)
    if runner.running:
        return {"skipped": True, "reason": "уже идёт"}
    started = runner.start(list(args or []))
    if not started.get("ok"):
        return {"skipped": True,
                "reason": started.get("error", "не удалось запустить")}
    deadline = time.time() + JOB_TIMEOUT
    while runner.running and time.time() < deadline:
        time.sleep(3)
    if runner.running:
        runner.stop()
        return {"exit_code": None, "stopped": True,
                "reason": "таймаут"}
    return {"exit_code": runner.exit_code}


def _run_job(key: str, job: dict) -> dict:
    jtype = job.get("type")
    if jtype == "scraper":
        return _run_scraper_job(key, job.get("args") or [])
    if jtype == "internal":
        return _run_internal_job(key, job.get("func") or "")
    return {"error": f"неизвестный тип: {jtype}"}


def _loop() -> None:
    _log.info("Hub: scheduler запущен (parallel runners)")
    while not _stop_flag.is_set():
        try:
            data = web_hub.read_schedule()
            # Каждый job обрабатываем в отдельном потоке-обёртке —
            # чтобы долгая задача не блокировала следующие
            for key, job in data.items():
                if _stop_flag.is_set(): break
                if not _due(job): continue
                # Внутренние задачи — синхронно (они быстрые)
                if job.get("type") == "internal":
                    _log.info("Hub: старт внутренней задачи %s", key)
                    result = _run_job(key, job)
                    web_hub.mark_run(key, result)
                    continue
                # Внешние — в отдельном потоке, чтобы не блокировать цикл
                t = threading.Thread(
                    target=_run_job_bg, args=(key, job),
                    name=f"hub-job-{key}", daemon=True)
                t.start()
        except Exception as e:
            _log.exception("Hub: ошибка цикла: %s", e)
        for _ in range(CHECK_INTERVAL):
            if _stop_flag.is_set(): break
            time.sleep(1)
    _log.info("Hub: scheduler остановлен")


def _run_job_bg(key: str, job: dict) -> None:
    try:
        _log.info("Hub: старт задачи %s (%s)", key, job.get("title"))
        result = _run_job(key, job)
        web_hub.mark_run(key, result)
        _log.info("Hub: %s завершена — %s", key, result)
    except Exception as e:
        _log.exception("Hub: ошибка задачи %s: %s", key, e)


def start_scheduler() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_flag.clear()
    _thread = threading.Thread(target=_loop, name="hub-scheduler",
                               daemon=True)
    _thread.start()


def stop_scheduler() -> None:
    _stop_flag.set()
    if _thread:
        _thread.join(timeout=5)
    # Гасим все runners
    with _runners_lock:
        for r in _runners.values():
            try: r.stop()
            except Exception: pass


def run_now(job_key: str) -> dict:
    """Принудительный запуск одной задачи прямо сейчас."""
    data = web_hub.read_schedule()
    if job_key not in data:
        return {"ok": False, "error": f"нет задачи {job_key}"}
    job = data[job_key]

    if job.get("type") == "internal":
        # Проверка на дубль
        with _internal_lock:
            if job_key in _internal_running:
                return {"ok": False, "error": "эта задача уже идёт"}
        result = _run_internal_job(job_key, job.get("func") or "")
        web_hub.mark_run(job_key, result)
        return {"ok": True, "result": result}

    # Внешняя задача
    runner = _get_runner(job_key)
    if runner.running:
        return {"ok": False, "error": f"задача {job_key} уже идёт"}
    # Запускаем асинхронно — не блокируем HTTP-запрос
    t = threading.Thread(target=_run_job_bg, args=(job_key, job),
                         name=f"hub-run-now-{job_key}", daemon=True)
    t.start()
    return {"ok": True, "started": True,
            "message": f"задача {job_key} запущена"}


def stop_job(job_key: str) -> dict:
    """Остановить конкретную задачу."""
    with _runners_lock:
        r = _runners.get(job_key)
    if not r or not r.running:
        return {"ok": False, "error": f"задача {job_key} не выполняется"}
    r.stop()
    return {"ok": True}
