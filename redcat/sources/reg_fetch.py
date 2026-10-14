"""reg_fetch — fetch-стратегии (HTTP-реестр и браузерные фетчеры)."""
from __future__ import annotations



# ──────────────────────────────────────────────────────────────
#  FETCH-СТРАТЕГИИ (скачивание ОДНОЙ страницы)
# ──────────────────────────────────────────────────────────────
# Фетчер принимает (url, spec, **kwargs) и возвращает единый словарь:
#     {"status": int | None, "body": bytes | None,
#      "headers": dict, "error": str | None}
# Ничего не парсит и не решает, повторять ли запрос — это политика
# вызывающего кода. Регистрируется из redcat_scraper.py, где есть
# requests и aiohttp.
#
# Имя стратегии берётся из spec.fetch_strategy. Если пусто — авто:
#   fetch_mode == "browser" → "browser", иначе "http".
FETCHERS: dict = {}


ASYNC_FETCHERS: dict = {}


def register_fetcher(name: str):
    def deco(fn):
        FETCHERS[name] = fn
        return fn
    return deco


def register_async_fetcher(name: str):
    def deco(fn):
        ASYNC_FETCHERS[name] = fn
        return fn
    return deco


def _pick_fetch_strategy(spec) -> str:
    if spec is not None and getattr(spec, "fetch_strategy", ""):
        return spec.fetch_strategy
    if spec is not None and getattr(spec, "fetch_mode", "http") == "browser":
        return "browser"
    return "http"


def fetch_raw(url, spec, **kwargs) -> dict:
    """Синхронное скачивание одной страницы через стратегию spec.

    kwargs передаются в фетчер как есть. Для http обычно нужен
    session=<requests.Session>. Для browser — обычно ничего.
    """
    name = _pick_fetch_strategy(spec)
    fetcher = FETCHERS.get(name)
    if fetcher is None:
        raise ValueError(
            f"Источник «{getattr(spec, 'key', '?')}»: неизвестная стратегия "
            f"скачивания «{name}». Доступные: "
            f"{', '.join(sorted(FETCHERS)) or '(нет зарегистрированных)'}.")
    return fetcher(url, spec, **kwargs)


async def async_fetch_raw(url, spec, **kwargs) -> dict:
    """Асинхронное скачивание одной страницы через стратегию spec.

    Если spec=None — используется "http" (совместимость со старым кодом
    preflight, где spec не всегда передавался).
    """
    name = _pick_fetch_strategy(spec) if spec is not None else "http"
    fetcher = ASYNC_FETCHERS.get(name)
    if fetcher is None:
        raise ValueError(
            f"Источник «{getattr(spec, 'key', '?')}»: неизвестная асинхронная "
            f"стратегия скачивания «{name}». Доступные: "
            f"{', '.join(sorted(ASYNC_FETCHERS)) or '(нет зарегистрированных)'}.")
    return await fetcher(url, spec, **kwargs)


def _import_browser_fetch():
    try:
        from redcat.collection import browser_fetch
        return browser_fetch
    except ImportError as e:
        raise RuntimeError(
            "Для browser-фетчеров нужен Playwright. Установите:\n"
            "    pip install playwright\n"
            "    playwright install chromium"
        ) from e


@register_fetcher("browser")
def _fetch_browser_page(url, spec, fetcher=None, **_kw):
    """Скачивает HTML страницы через Playwright.

    Если передан `fetcher=` (открытый BrowserFetcher) — использует его;
    иначе открывает свой и закрывает после вызова. Для одиночных вызовов
    (probe) это норма; для серийных — передавайте fetcher явно.
    """
    bf = _import_browser_fetch()
    if not hasattr(bf, "BrowserFetcher"):
        return {"status": None, "body": None, "headers": {},
                "error": "browser_fetch.py не найден или Playwright не установлен."}

    own = fetcher is None
    if own:
        fetcher = bf.BrowserFetcher(
            getattr(spec, "key", "probe"),
            headless=getattr(spec, "browser_headless", True))
        fetcher.__enter__()
    try:
        html = fetcher.fetch(
            url,
            wait_for=getattr(spec, "browser_wait_for", "") or "",
            wait_ms=int(getattr(spec, "browser_wait_ms", 0) or 0),
        )
        body = (html or "").encode("utf-8")
        return {"status": 200, "body": body, "headers": {}, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "body": None, "headers": {},
                "error": f"{type(e).__name__}: {e}"}
    finally:
        if own:
            try:
                fetcher.__exit__(None, None, None)
            except Exception:
                pass


@register_fetcher("browser_xhr")
def _fetch_browser_xhr(url, spec, fetcher=None, **_kw):
    """Открывает страницу и возвращает JSON одного из её XHR/fetch-ответов.

    По умолчанию (spec.browser_xhr_pattern пуст) берётся первый JSON-ответ.
    Если задан паттерн — берётся первый, чей URL содержит подстроку.

    body возвращается как JSON-текст — далее parse_strategy="json_path"
    разбирает его как обычный ответ API.
    """
    import json as _json
    bf = _import_browser_fetch()
    if not hasattr(bf, "BrowserFetcher"):
        return {"status": None, "body": None, "headers": {},
                "error": "browser_fetch.py не найден или Playwright не установлен."}

    own = fetcher is None
    if own:
        fetcher = bf.BrowserFetcher(
            getattr(spec, "key", "probe"),
            headless=getattr(spec, "browser_headless", True))
        fetcher.__enter__()
    try:
        pattern = getattr(spec, "browser_xhr_pattern", "") or ""
        captured = fetcher.capture_json_responses(
            url, pattern,
            wait_for=getattr(spec, "browser_wait_for", "") or "",
            wait_ms=int(getattr(spec, "browser_wait_ms", 0) or 0),
        )
        if not captured:
            return {"status": None, "body": None, "headers": {},
                    "error": f"ни одного JSON-XHR по паттерну «{pattern}» "
                             f"не поймано"}
        payload = captured[0]
        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return {"status": 200, "body": body, "headers": {}, "error": None}
    except Exception as e:  # noqa: BLE001
        return {"status": None, "body": None, "headers": {},
                "error": f"{type(e).__name__}: {e}"}
    finally:
        if own:
            try:
                fetcher.__exit__(None, None, None)
            except Exception:
                pass
