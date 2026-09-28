"""
Управление частотой запросов и защита от бана.
================================================
Три независимых механизма:

  1. TokenBucket — «N запросов в секунду на хост». Работает и в sync
     (time.sleep), и в async (asyncio.sleep). Позволяет не выжигать
     лимиты API при высоком concurrency.

  2. CircuitBreaker — если источник отдаёт подряд N ошибок, временно
     отключаем его на cooldown. Пока breaker открыт, запросы к этому
     источнику не идут, а сбор сразу помечается неполным — вместо того
     чтобы минуту долбить API и получить 429/500.

  3. parse_retry_after — читает заголовки Retry-After и X-RateLimit-Reset
     и возвращает, сколько секунд ждать. Уважение этих заголовков —
     самый простой способ не попасть в чёрный список.

Модуль не зависит от requests/aiohttp/playwright: только stdlib.
"""
from __future__ import annotations

import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime
from email.utils import parsedate_to_datetime


# ──────────────────────────────────────────────────────────────
#  URL → хост
# ──────────────────────────────────────────────────────────────
def host_of(url: str) -> str:
    try:
        return urllib.parse.urlparse(str(url)).netloc.lower() or "unknown"
    except Exception:
        return "unknown"


# ──────────────────────────────────────────────────────────────
#  TokenBucket — «N запросов в секунду»
# ──────────────────────────────────────────────────────────────
class TokenBucket:
    """Простой token bucket.

    Токены пополняются со скоростью rate_per_sec, максимум накапливается
    до burst (по умолчанию = rate, но не меньше 1). try_consume() возвращает
    0.0 если можно идти, иначе — сколько секунд надо подождать.
    """

    __slots__ = ("rate", "burst", "tokens", "last", "lock")

    def __init__(self, rate_per_sec: float, burst: float | None = None):
        self.rate = max(0.01, float(rate_per_sec))
        self.burst = max(1.0, float(burst or max(1.0, rate_per_sec)))
        self.tokens = self.burst
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last
        if elapsed > 0:
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last = now

    def try_consume(self) -> float:
        """Возвращает 0.0 (можно), иначе секунды ожидания."""
        with self.lock:
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return 0.0
            return (1.0 - self.tokens) / self.rate

    def on_rate_limit(self, retry_after_sec: float):
        """Сервер вернул 429 — обнуляем токены и добавляем паузу."""
        with self.lock:
            self.tokens = 0.0
            self.last = time.monotonic() + max(0.0, retry_after_sec)

    def state(self) -> dict:
        with self.lock:
            self._refill()
            return {"rate": self.rate, "burst": self.burst,
                    "tokens": round(self.tokens, 3)}


# ──────────────────────────────────────────────────────────────
#  HostRateLimiters — пул бакетов
# ──────────────────────────────────────────────────────────────
class HostRateLimiters:
    """{хост: TokenBucket}. Бакеты создаются лениво, по первому запросу."""

    def __init__(self, default_rps: float = 0.0):
        self.default_rps = max(0.0, float(default_rps))
        self._buckets: dict = {}
        self._lock = threading.Lock()

    def _bucket(self, url: str, rps: float) -> TokenBucket | None:
        if rps <= 0:
            return None
        host = host_of(url)
        with self._lock:
            b = self._buckets.get(host)
            if b is None or b.rate != rps:
                b = TokenBucket(rps)
                self._buckets[host] = b
            return b

    def try_consume(self, url: str, rps: float | None = None) -> float:
        """Возвращает секунды ожидания (0.0 = идём сразу)."""
        r = rps if rps is not None else self.default_rps
        b = self._bucket(url, r)
        if b is None:
            return 0.0
        return b.try_consume()

    def on_rate_limit(self, url: str, retry_after_sec: float,
                      rps: float | None = None):
        r = rps if rps is not None else self.default_rps
        b = self._bucket(url, r)
        if b is not None:
            b.on_rate_limit(retry_after_sec)

    def snapshot(self) -> dict:
        with self._lock:
            return {h: b.state() for h, b in self._buckets.items()}


# ──────────────────────────────────────────────────────────────
#  CircuitBreaker
# ──────────────────────────────────────────────────────────────
class CircuitBreaker:
    """Отключает источник при серии ошибок.

    Состояния:
      closed   — работаем нормально;
      open     — отключено до open_until, запросы не идут;
      half_open — cooldown истёк, пробуем один запрос; успех → closed,
                  ошибка → снова open на cooldown.

    Счётчик consecutive_failures сбрасывается при первом успехе.
    """

    def __init__(self, failures: int = 8, cooldown_sec: int = 300):
        self.failures_limit = max(1, int(failures))
        self.cooldown_sec = max(1, int(cooldown_sec))
        self.consecutive_failures = 0
        self.open_until = 0.0
        self.last_reason = ""
        self.total_opens = 0
        self.last_open_at = None
        self.lock = threading.Lock()

    def is_open(self) -> bool:
        with self.lock:
            if self.open_until == 0.0:
                return False
            if time.monotonic() < self.open_until:
                return True
            # cooldown истёк — half-open: пускаем один запрос.
            # Если он упадёт — record_failure снова откроет breaker.
            return False

    def state(self) -> dict:
        with self.lock:
            now = time.monotonic()
            if self.open_until > 0 and now < self.open_until:
                state = "open"
                remaining = round(self.open_until - now, 1)
            elif self.open_until > 0:
                state = "half_open"
                remaining = 0.0
            else:
                state = "closed"
                remaining = 0.0
            return {
                "state": state,
                "consecutive_failures": self.consecutive_failures,
                "failures_limit": self.failures_limit,
                "cooldown_sec": self.cooldown_sec,
                "open_remaining_sec": remaining,
                "total_opens": self.total_opens,
                "last_reason": self.last_reason,
                "last_open_at": self.last_open_at,
            }

    def record_success(self):
        with self.lock:
            self.consecutive_failures = 0
            self.open_until = 0.0

    def record_failure(self, reason: str = ""):
        with self.lock:
            self.consecutive_failures += 1
            self.last_reason = reason or "unknown"
            if self.consecutive_failures >= self.failures_limit:
                self.open_until = time.monotonic() + self.cooldown_sec
                self.total_opens += 1
                self.last_open_at = datetime.now().isoformat(timespec="seconds")

    def open_now(self, reason: str, cooldown_sec: int | None = None):
        """Принудительно открыть (например, при явном HTTP 429)."""
        with self.lock:
            self.open_until = time.monotonic() + (
                cooldown_sec if cooldown_sec is not None else self.cooldown_sec)
            self.total_opens += 1
            self.last_reason = reason or "forced"
            self.last_open_at = datetime.now().isoformat(timespec="seconds")


# ──────────────────────────────────────────────────────────────
#  Retry-After
# ──────────────────────────────────────────────────────────────
def parse_retry_after(headers, default: float = 0.0) -> float:
    """Возвращает секунды ожидания по Retry-After / X-RateLimit-Reset.

    Retry-After бывает числом секунд или HTTP-датой. X-RateLimit-Reset
    бывает unix timestamp в секундах или миллисекундах.
    """
    if not headers:
        return max(0.0, float(default))
    get = (headers.get if hasattr(headers, "get") else
           lambda k, d=None: None)

    ra = get("Retry-After") or get("retry-after")
    if ra:
        try:
            return max(0.0, float(str(ra).strip()))
        except (TypeError, ValueError):
            try:
                dt = parsedate_to_datetime(str(ra))
                now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
                return max(0.0, (dt - now).total_seconds())
            except Exception:
                pass

    for key in ("X-RateLimit-Reset", "x-ratelimit-reset",
                "X-Rate-Limit-Reset"):
        v = get(key)
        if not v:
            continue
        try:
            reset = float(str(v).strip())
        except (TypeError, ValueError):
            continue
        # Если это unix timestamp — сравниваем с текущим временем.
        # Если это «секунд до сброса» — это маленькое число, вернём как есть.
        now = time.time()
        if reset > 1_000_000_000:  # unix timestamp (после 2001 года)
            if reset > now:
                return reset - now
            # Уже в прошлом — не ждём.
            return 0.0
        # Небольшое число — вероятно «секунд до сброса».
        return max(0.0, reset)

    return max(0.0, float(default))
