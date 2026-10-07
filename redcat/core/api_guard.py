"""
ЗАЩИТА ОТ ЗАПИСИ В API (read-only guard)
========================================
Главное правило этого приложения: **наружу уходят только GET-запросы**.
Никаких PUT, POST, PATCH, DELETE в сторонний API — ни случайно, ни намеренно.

Это не договорённость «мы постараемся», а техническая блокировка на уровне
библиотек. Модуль подменяет точки входа `requests` и `aiohttp`: любой вызов
с методом, которого нет в ALLOWED_METHODS, падает с исключением
`WriteRequestBlocked` ещё ДО открытия сокета. Сетевого запроса просто не
происходит.

Почему так, а не «просто не писать такой код»:
  • источники описываются JSON-файлами, их правит пользователь — там нельзя
    задать метод, но страховка нужна на случай будущих правок;
  • любая сторонняя библиотека, затянутая в проект, тоже проходит через
    requests/aiohttp и тоже окажется под блокировкой;
  • ошибка копипаста в коде превратится в понятное исключение с текстом,
    а не в тихую модификацию чужих данных.

Проверить, что защита реально работает:
    python -m redcat.tools.selftest_readonly

Дополнительно ведётся журнал всех исходящих запросов (метод, URL, HTTP-код,
время ответа) — его видно в приложении на вкладке «Журнал API». То есть
read-only не только обещан, но и доказуем постфактум.
"""

from __future__ import annotations

import logging
import time

# Единственные методы, которые разрешено отправлять наружу.
# OPTIONS оставлен, потому что его иногда шлёт сам транспорт (CORS/preflight),
# и он по определению не меняет данные на сервере.
ALLOWED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Методы, изменяющие данные. Перечислены явно, чтобы сообщение об ошибке было
# конкретным, а не «неизвестный метод».
WRITE_METHODS = frozenset({
    "POST", "PUT", "PATCH", "DELETE", "TRACE", "CONNECT",
    "COPY", "MOVE", "LOCK", "UNLOCK", "MKCOL", "PROPPATCH",
})

_audit_hook = None
_blocked_count = 0


class WriteRequestBlocked(RuntimeError):
    """Попытка отправить в API запрос, изменяющий данные."""


def set_audit_hook(fn) -> None:
    """Регистрирует функцию журналирования: fn(method, url, status, ms, note)."""
    global _audit_hook
    _audit_hook = fn


def blocked_count() -> int:
    """Сколько попыток записи было заблокировано за время работы процесса."""
    return _blocked_count


def _audit(method, url, status, ms, note=""):
    if _audit_hook is None:
        return
    try:
        _audit_hook(method, str(url), status, ms, note)
    except Exception as e:  # журнал никогда не должен ломать основную работу
        logging.debug("Журнал API недоступен: %s", e)


def check_method(method, url="") -> str:
    """Пропускает безопасный метод, иначе поднимает WriteRequestBlocked."""
    global _blocked_count
    m = str(method or "").upper()
    if m in ALLOWED_METHODS:
        return m

    _blocked_count += 1
    kind = "изменяющий данные" if m in WRITE_METHODS else "неизвестный"
    message = (
        f"⛔ Запрос {m} к «{url}» заблокирован.\n"
        f"   Это {kind} метод, а приложение работает с API строго на чтение.\n"
        f"   Разрешены только: {', '.join(sorted(ALLOWED_METHODS))}.\n"
        f"   Если нужно что-то поправить в данных — правьте локальную копию\n"
        f"   во вкладке «Данные»: правки хранятся у вас и в API не уходят."
    )
    logging.error("Заблокирован %s %s", m, url)
    _audit(m, url, None, 0, "ЗАБЛОКИРОВАНО")
    raise WriteRequestBlocked(message)


# ──────────────────────────────────────────────────────────────
#  ПОДМЕНА ТОЧЕК ВХОДА
# ──────────────────────────────────────────────────────────────
def _install_requests() -> bool:
    try:
        import requests
    except ImportError:
        return False

    original = requests.sessions.Session.request
    if getattr(original, "_redcat_guarded", False):
        return True

    def guarded_request(self, method, url, *args, **kwargs):
        check_method(method, url)
        started = time.perf_counter()
        status = None
        try:
            response = original(self, method, url, *args, **kwargs)
            status = response.status_code
            return response
        finally:
            _audit(str(method).upper(), url, status,
                   round((time.perf_counter() - started) * 1000, 1))

    guarded_request._redcat_guarded = True
    requests.sessions.Session.request = guarded_request
    return True


def _install_aiohttp() -> bool:
    try:
        import aiohttp
    except ImportError:
        return False

    original = aiohttp.ClientSession._request
    if getattr(original, "_redcat_guarded", False):
        return True

    async def guarded_request(self, method, url, *args, **kwargs):
        check_method(method, url)
        started = time.perf_counter()
        status = None
        try:
            response = await original(self, method, url, *args, **kwargs)
            status = response.status
            return response
        finally:
            _audit(str(method).upper(), url, status,
                   round((time.perf_counter() - started) * 1000, 1))

    guarded_request._redcat_guarded = True
    aiohttp.ClientSession._request = guarded_request
    return True


def install() -> dict:
    """Включает блокировку. Вызывать один раз при старте, до сетевых вызовов."""
    result = {"requests": _install_requests(), "aiohttp": _install_aiohttp()}
    logging.info("Read-only guard включён: %s", result)
    return result


def status_text() -> str:
    """Короткая строка для вывода в консоль и в интерфейсе."""
    parts = []
    try:
        import requests
        parts.append("requests" if getattr(
            requests.sessions.Session.request, "_redcat_guarded", False) else "requests(!)")
    except ImportError:
        pass
    try:
        import aiohttp
        parts.append("aiohttp" if getattr(
            aiohttp.ClientSession._request, "_redcat_guarded", False) else "aiohttp(!)")
    except ImportError:
        pass
    return f"🔒 Только чтение (GET). Под защитой: {', '.join(parts) or 'нет клиентов'}"
