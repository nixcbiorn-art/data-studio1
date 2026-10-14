"""web_mail_scheduler — фоновый поток, который раз в N минут собирает
почту и, если появились новые письма, шлёт уведомление.

Запускается автоматически при старте сервера (см. webapp.serve).
Останавливается при выходе — daemon=True, но есть и явный stop().

Логика цикла:
  1. Спим 30 секунд.
  2. Читаем состояние из history/mail_scheduler.json.
  3. Если enabled=False — снова спим.
  4. Если с прошлого запуска прошло >= interval_minutes — стартуем сбор.
  5. Ждём завершения RUNNER (опрос раз в 5 сек, максимум 30 мин).
  6. Проверяем новые письма и, если надо, шлём уведомление.
  7. Записываем last_run и повторяем.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from redcat.web.web_mail_notify import (
    check_and_notify, read_state, write_state,
)
from redcat.web.web_runners import RUNNER

_log = logging.getLogger(__name__)

_thread: threading.Thread | None = None
_stop_flag = threading.Event()


def _run_once() -> dict:
    """Один цикл: сбор + проверка новых. Возвращает отчёт."""
    if RUNNER.running:
        return {"ok": False, "reason": "уже идёт другой сбор"}

    started = RUNNER.start(["--only", "mail_inbox"])
    if not started.get("ok"):
        return {"ok": False,
                "reason": started.get("error") or "не удалось запустить"}

    # Ждём завершения (но не больше 30 минут)
    deadline = time.time() + 30 * 60
    while RUNNER.running and time.time() < deadline:
        time.sleep(5)

    if RUNNER.running:
        RUNNER.stop()
        return {"ok": False, "reason": "сбор не завершился за 30 минут"}

    notify = check_and_notify()
    return {"ok": True, "notify": notify}


def _loop() -> None:
    _log.info("Планировщик почты запущен")
    while not _stop_flag.is_set():
        try:
            st = read_state()
            if st.get("enabled"):
                interval = max(5, int(st.get("interval_minutes") or 60))
                last = st.get("last_run")
                now = datetime.now()
                if not last:
                    due = True
                else:
                    try:
                        last_dt = datetime.fromisoformat(last)
                        due = (now - last_dt).total_seconds() >= interval * 60
                    except ValueError:
                        due = True
                if due:
                    _log.info("Планировщик: запускаю сбор почты")
                    result = _run_once()
                    _log.info("Планировщик: %s", result)
                    st = read_state()
                    st["last_run"] = datetime.now().isoformat(timespec="seconds")
                    write_state(st)
        except Exception as e:  # noqa: BLE001
            _log.exception("Планировщик: ошибка цикла: %s", e)
        # Проверяем состояние раз в 30 секунд
        for _ in range(30):
            if _stop_flag.is_set():
                break
            time.sleep(1)
    _log.info("Планировщик почты остановлен")


def start_scheduler() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_flag.clear()
    _thread = threading.Thread(target=_loop, name="mail-scheduler",
                               daemon=True)
    _thread.start()


def stop_scheduler() -> None:
    _stop_flag.set()
    if _thread:
        _thread.join(timeout=5)


def run_now() -> dict:
    """Принудительный запуск одного цикла (кнопка «Собрать сейчас»)."""
    return _run_once()


# ──────────────────────────────────────────────────────────────
#  После сбора — применить правила задач (patch_mail_tasks.py)
# ──────────────────────────────────────────────────────────────
_orig_run_once = _run_once


def _run_once():
    result = _orig_run_once()
    try:
        from redcat.web.web_mail_task_rules import apply_rules
        rules_result = apply_rules(days=90, limit=2000)
        if isinstance(result, dict):
            result["tasks"] = rules_result
        _log.info("Планировщик: правила задач — %s", rules_result)
    except Exception as e:  # noqa: BLE001
        _log.exception("Планировщик: ошибка применения правил: %s", e)
    return result

# ──────────────────────────────────────────────────────────────
#  Хук: применить фильтры к уведомлениям (patch_mail_notify_saved.py)
# ──────────────────────────────────────────────────────────────
def _check_and_notify_filtered():
    from redcat.web.web_mail_notify_filters import get as get_filters, \
        should_notify
    from redcat.web.web_mail_notify import (
        _max_uid, _query_new_since_uid, read_state, write_state,
        _telegram_send, _format_notification)
    cfg = get_filters()
    st = read_state()
    cur_max = _max_uid()
    if cur_max == 0:
        return {"new": 0, "notified": False, "reason": "нет писем"}
    if not st.get("initialized"):
        st["last_notified_uid"] = cur_max
        st["initialized"] = True
        write_state(st)
        return {"new": 0, "notified": False, "reason": "первый запуск"}
    last = int(st.get("last_notified_uid") or 0)
    if cur_max <= last:
        return {"new": 0, "notified": False, "reason": "новых нет"}
    rows = _query_new_since_uid(last)
    st["last_notified_uid"] = max((int(r["uid"]) for r in rows), default=cur_max)
    if not st.get("notify_new"):
        write_state(st)
        return {"new": len(rows), "notified": False,
                "reason": "уведомления выключены"}
    # Применяем фильтры
    passed = []
    skipped = 0
    for r in rows:
        ok, why = should_notify(r, cfg)
        if ok:
            passed.append(r)
        else:
            skipped += 1
    if not passed:
        write_state(st)
        return {"new": len(rows), "notified": False,
                "reason": f"все отфильтрованы ({skipped})",
                "skipped": skipped}
    text = _format_notification(passed)
    if skipped:
        text += f"\n\n<i>и ещё {skipped} отфильтровано</i>"
    res = _telegram_send(text)
    write_state(st)
    return {"new": len(passed), "notified": res["ok"],
            "skipped": skipped, "error": res.get("error")}

