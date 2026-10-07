"""scrape_http — синхронные и асинхронные HTTP-запросы с повторами."""
from __future__ import annotations

import asyncio
import time
from redcat.collection import rate_limit as _rl
from redcat.sources import registry as src
from redcat.collection.scrape_config import REQUEST_TIMEOUT
from redcat.collection.scrape_env import aiohttp, requests
from redcat.collection.scrape_guard import _RATE_LIMITERS, _breaker_for, _rps_for


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

    request_headers = dict(getattr(spec, "headers", None) or {})
    try:
        resp = session.get(url, headers=request_headers or None,
                           timeout=kwargs.get("timeout", REQUEST_TIMEOUT))
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
        else:
            # 4xx кроме 429 — наша ошибка (неверный URL, нет
            # прав). Сервер ответил, значит он жив: с точки
            # зрения breaker это успех. Иначе half-open-пробный
            # после 404/403 «зависнет» навсегда.
            cb.record_success()

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

    request_headers = dict(getattr(spec, "headers", None) or {})
    try:
        async with session.get(url, headers=request_headers or None) as resp:
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
            else:
                # 4xx кроме 429 — наша ошибка, сервер жив.
                # См. комментарий в sync-версии.
                cb.record_success()

            return {"status": status, "body": body,
                    "headers": headers, "error": None}
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        cb.record_failure(type(e).__name__)
        return {"status": None, "body": None, "headers": {},
                "error": str(e), "error_kind": "network"}
