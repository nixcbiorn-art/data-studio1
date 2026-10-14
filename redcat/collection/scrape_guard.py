"""scrape_guard — защита от бана: лимит частоты и circuit breaker по хостам."""
from __future__ import annotations

from redcat.collection import scrape_env  # noqa: F401  — первым: загрузка .env до чтения переменных
import os
import threading
from redcat.collection import rate_limit as _rl


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
