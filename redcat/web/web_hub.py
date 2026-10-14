"""web_hub — единый хаб задач: почта + парсер.

Хранит список задач в hub_schedule.json. Каждая задача — словарь:
    key        — идентификатор
    title      — название для UI
    type       — "scraper" | "internal"
    args       — аргументы scraper'а (только для type="scraper")
    func       — имя функции (только для type="internal")
    enabled    — включена ли
    interval   — период в минутах
    last_run   — ISO когда последний раз запускалась
    last_result— результат последнего запуска
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from redcat.web.web_config import BASE_DIR


HUB_FILE = BASE_DIR / "hub_schedule.json"


# Значения по умолчанию. При первом запуске создаём hub_schedule.json
# с этими задачами, подтянув настройки почты из старого mail_scheduler.json.
DEFAULTS = {
    "mail_inbox": {
        "title": "📬 Почта (входящие)",
        "desc": "Собирается автоматически через стриминг-воркер",
        "type": "scraper",
        "args": ["--only", "mail_inbox"],
        "enabled": False,
        "interval": 60,
        "last_run": None,
        "last_result": None,
    },
    "redcat_full": {
        "title": "📊 Данные RedCat",
        "desc": "Полный сбор по всем источникам",
        "type": "scraper",
        "args": [],
        "enabled": False,
        "interval": 720,
        "last_run": None,
        "last_result": None,
    },
    "tasks_rules": {
        "title": "📋 Правила задач",
        "desc": "Применение правил авто-задач к новым письмам",
        "type": "internal",
        "func": "tasks_rules",
        "enabled": True,
        "interval": 60,
        "last_run": None,
        "last_result": None,
    },
}


def _migrate_from_mail_scheduler() -> None:
    """Если раньше был mail_scheduler.json — переносим его настройки."""
    old = BASE_DIR / "history" / "mail_scheduler.json"
    if not old.exists():
        return
    try:
        d = json.loads(old.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    ml = DEFAULTS["mail_inbox"]
    if "enabled" in d:
        ml["enabled"] = bool(d["enabled"])
    if "interval_minutes" in d:
        try:
            ml["interval"] = int(d["interval_minutes"])
        except (TypeError, ValueError):
            pass
    if d.get("last_run"):
        ml["last_run"] = d["last_run"]


def read_schedule() -> dict:
    if not HUB_FILE.exists():
        _migrate_from_mail_scheduler()
        write_schedule({k: dict(v) for k, v in DEFAULTS.items()})
        return {k: dict(v) for k, v in DEFAULTS.items()}
    try:
        data = json.loads(HUB_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    # Подстраховка: добиваем отсутствующие задачи из DEFAULTS
    changed = False
    for key, default in DEFAULTS.items():
        if key not in data:
            data[key] = dict(default)
            changed = True
    if changed:
        write_schedule(data)
    return data


def write_schedule(data: dict) -> None:
    HUB_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8")


def save_job(job_key: str, fields: dict) -> dict:
    data = read_schedule()
    if job_key not in data:
        return {"ok": False, "error": f"неизвестная задача: {job_key}"}
    allowed = {"enabled", "interval", "title", "desc"}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "enabled":
            data[job_key]["enabled"] = bool(v)
        elif k == "interval":
            try:
                data[job_key]["interval"] = max(5, min(1440 * 7, int(v)))
            except (TypeError, ValueError):
                pass
        else:
            data[job_key][k] = str(v)
    write_schedule(data)
    return {"ok": True, "job": data[job_key]}


def mark_run(job_key: str, result: dict | None) -> None:
    data = read_schedule()
    if job_key not in data:
        return
    data[job_key]["last_run"] = datetime.now().isoformat(timespec="seconds")
    data[job_key]["last_result"] = result
    write_schedule(data)


def status() -> dict:
    """Статус всех задач + состояние каждого runner'а."""
    from redcat.web.web_hub_scheduler import get_all_runners_status
    data = read_schedule()
    now = datetime.now()
    runners = get_all_runners_status()

    jobs = []
    for key, j in data.items():
        j2 = dict(j)
        j2["key"] = key
        if j.get("last_run"):
            try:
                last = datetime.fromisoformat(j["last_run"])
                next_in = max(0, int(j.get("interval", 60)
                                     - (now - last).total_seconds() / 60))
                j2["next_in_minutes"] = next_in
            except (ValueError, TypeError):
                j2["next_in_minutes"] = None
        else:
            j2["next_in_minutes"] = 0
        # Индивидуальный runner
        j2["runner"] = runners.get(key, {"running": False})
        jobs.append(j2)

    jobs.sort(key=lambda j: (not j.get("enabled"), j.get("title", "")))

    return {
        "jobs": jobs,
        "runners_count": len(runners),
        "server_time": now.isoformat(timespec="seconds"),
    }
