"""web_mail_send_hook — обёртка над send() с триггером задачи.

Оборачивает web_mail_send.send. Если передан in_reply_to и отправка
успешна — переводит задачу по этому письму в статус in_progress.
Оригинальный send() не тронут.
"""
from __future__ import annotations

import logging

from redcat.web.web_mail_send import send as _orig_send

_log = logging.getLogger(__name__)


def send(to: str, subject: str, body: str,
         cc: str = "", bcc: str = "",
         in_reply_to: str = "") -> dict:
    """Отправляет письмо. Если указан in_reply_to — переводит задачу
    по этому письму в in_progress (patch_reply_triggers_task).

    Если у задачи статус не "open" — оставляем как есть.
    Ошибка триггера не влияет на результат отправки.
    """
    result = _orig_send(to, subject, body, cc=cc, bcc=bcc)

    if not result.get("ok"):
        return result

    if not in_reply_to:
        return result

    try:
        from redcat.web.web_mail_tasks import task_in_progress_from_reply
        trigger = task_in_progress_from_reply(
            in_reply_to, to=", ".join(result.get("to") or []))
        if trigger:
            result["task_trigger"] = trigger
            _log.info("task trigger: %s", trigger)
    except Exception as e:
        _log.warning("task trigger failed: %s", e)

    return result
