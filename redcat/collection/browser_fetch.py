"""
Загрузка через виртуальный браузер (Playwright).
================================================
Для источников, которым нужен JS, куки и/или защита от бота. Redcat этот
модуль не использует — он ходит обычными GET-запросами через requests.

Каждый источник получает свой профиль в browser_profiles/<source_key>/:
там сохраняются cookies и localStorage между запусками. Это позволяет
один раз пройти капчу/логин (в видимом режиме) и потом работать headless.

Установка (один раз):
    pip install playwright
    playwright install chromium

Использование из scraper’а:
    with BrowserFetcher("my_source") as f:
        html = f.fetch("https://example.com/catalog",
                       wait_for=".product-card", wait_ms=1500)
"""

from __future__ import annotations

from redcat.core import paths
import json
import logging
import time
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    _PW_AVAILABLE = True
except ImportError:
    _PW_AVAILABLE = False

try:
    from playwright_stealth import stealth_sync
    _STEALTH = True
except ImportError:
    _STEALTH = False


BASE_DIR = paths.ROOT
PROFILES_DIR = BASE_DIR / "browser_profiles"
PROFILES_DIR.mkdir(exist_ok=True)

# Реалистичный UA — иначе многие сайты отдают 403 сразу.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/122.0.0.0 Safari/537.36")

DEFAULT_TIMEOUT = 30_000   # мс


class BrowserNotInstalled(RuntimeError):
    """Playwright не установлен или не выполнен `playwright install`."""


def _ensure_playwright():
    if not _PW_AVAILABLE:
        raise BrowserNotInstalled(
            "Playwright не установлен. Выполните в папке проекта:\n"
            "    pip install playwright\n"
            "    playwright install chromium"
        )


class BrowserFetcher:
    """Одна сессия браузера на источник. Оборачивать в `with`."""

    def __init__(self, source_key: str, headless: bool = True,
                 locale: str = "ru-RU", block_non_get: bool = True):
        _ensure_playwright()
        self.source_key = source_key
        self.headless = headless
        self.locale = locale
        self.block_non_get = block_non_get
        self.profile_dir = PROFILES_DIR / source_key
        self.profile_dir.mkdir(exist_ok=True)
        self._pw = None
        self._context = None
        self._page = None

    def __enter__(self):
        self._pw = sync_playwright().start()
        # Постоянный контекст с профилем: куки и localStorage живут между
        # запусками. Первый раз можно headless=False — пройти капчу/логин
        # вручную; дальше headless=True работает с теми же куками.
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            user_agent=USER_AGENT,
            locale=self.locale,
            viewport={"width": 1366, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        self._page = self._context.new_page()
        if _STEALTH:
            try:
                stealth_sync(self._page)
            except Exception as e:  # noqa: BLE001
                logging.debug("stealth не применился: %s", e)
        self._page.set_default_timeout(DEFAULT_TIMEOUT)

        # Блокируем всё, кроме GET/HEAD/OPTIONS и технических запросов
        # антибот-защит. Приложение работает только на чтение — это правило
        # распространяется и на браузер.
        if self.block_non_get:
            self._page.route("**/*", self._route_filter)

        return self

    def __exit__(self, *exc):
        try:
            if self._context:
                self._context.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _route_filter(self, route):
        """Пускает GET/HEAD/OPTIONS и технические запросы антибот-защиты.

        Общий принцип: всё, что изменяет данные на сторонних серверах,
        блокируется. Но антибот-защиты (Qrator, Cloudflare, hCaptcha)
        требуют POST на свои эндпоинты — без этого сессия не авторизуется,
        и браузер бесконечно получает челлендж-страницу вместо данных.
        Такие POST'ы — техническая проверка «ты браузер», а не изменение
        чужой информации, поэтому они исключены из блокировки.
        """
        method = route.request.method.upper()
        url = route.request.url

        if method in ("GET", "HEAD", "OPTIONS"):
            route.continue_()
            return

        if any(marker in url for marker in (
                "/__qrator/", "__qrator", "qauth",
                "/cdn-cgi/", "challenge-platform",     # Cloudflare
                "/captcha", "/recaptcha", "/hcaptcha",
        )):
            route.continue_()
            return

        logging.warning("[%s] заблокирован %s %s", self.source_key, method, url)
        route.abort()

    def fetch(self, url: str, wait_for: str = "",
              wait_ms: int = 0) -> str:
        """Открывает URL, ждёт отрисовки, возвращает HTML."""
        page = self._page
        page.goto(url, wait_until="domcontentloaded")
        if wait_for:
            try:
                page.wait_for_selector(wait_for, timeout=DEFAULT_TIMEOUT)
            except PWTimeout:
                logging.warning("[%s] селектор %r не появился за %d мс",
                                self.source_key, wait_for, DEFAULT_TIMEOUT)
        if wait_ms:
            time.sleep(wait_ms / 1000)
        return page.content()

    def fetch_json(self, url: str, wait_for: str = "",
                   wait_ms: int = 0, retries: int = 2):
        """Открывает URL и вытаскивает JSON из тела.

        Qrator (samolet.ru) отдаёт HTML-челлендж вместо JSON, а сам JSON
        приходит только при повторном заходе с уже установленной cookie.
        Поэтому делаем до `retries` попыток: каждая — goto + ожидание,
        пока в DOM появится текст, начинающийся с «{» или «[».

        Параметр wait_ms — минимальная пауза перед чтением. Реально
        ожидание может быть дольше: столько, сколько нужно Qrator'у на
        проверку (обычно 2–5 секунд, иногда до 10).
        """
        page = self._page
        debug_path = BASE_DIR / "reports" / f"_debug_{self.source_key}.html"

        for attempt in range(1, retries + 1):
            page.goto(url, wait_until="domcontentloaded")
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=DEFAULT_TIMEOUT)
                except PWTimeout:
                    pass

            # Ждём, пока в DOM появится текст, похожий на JSON.
            # Qrator после валидации сам перезагрузит страницу на исходный URL.
            try:
                page.wait_for_function(
                    "() => {"
                    "  const t = document.body ? document.body.innerText : '';"
                    "  const s = t.trim();"
                    "  return s && (s.startsWith('{') || s.startsWith('['));"
                    "}",
                    timeout=max(wait_ms, 15_000))
            except PWTimeout:
                pass
            if wait_ms:
                time.sleep(wait_ms / 1000)

            # Пробуем два места, куда Chrome кладёт JSON-ответ:
            #   <pre> — основное для text/plain и application/json,
            #   <body> — fallback.
            for getter in (
                "() => document.querySelector('pre') ? "
                "document.querySelector('pre').innerText : ''",
                "() => document.body ? document.body.innerText : ''",
            ):
                try:
                    text = page.evaluate(getter)
                except Exception:  # noqa: BLE001
                    continue
                if not text:
                    continue
                s = text.strip()
                if not s.startswith(("{", "[")):
                    continue
                try:
                    return json.loads(s)
                except ValueError:
                    continue

            logging.warning("[%s] попытка %d/%d: JSON не найден, "
                            "в DOM, похоже, челлендж", self.source_key,
                            attempt, retries)
            time.sleep(2)  # дать Qrator'у время дорешать челлендж

        # Все попытки провалились — сохраняем дамп для разбора.
        try:
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_path.write_text(page.content(), encoding="utf-8")
            logging.warning("[%s] дамп сохранён: %s", self.source_key, debug_path)
        except OSError as e:
            logging.warning("не удалось сохранить дамп: %s", e)
        return None

    def capture_json_responses(self, url: str, url_pattern: str,
                               wait_for: str = "", wait_ms: int = 0,
                               timeout_ms: int = 30000):
        """Перехватывает все JSON-ответы XHR/fetch, чьи URL подходят
        под url_pattern (подстрока). Полезно, когда сайт сам тянет JSON
        с бэкенда — быстрее, чем парсить HTML.
        """
        page = self._page
        captured = []

        def _on_response(resp):
            try:
                if url_pattern not in resp.url:
                    return
                ct = (resp.headers.get("content-type") or "").lower()
                if "json" not in ct:
                    return
                captured.append(resp.json())
            except Exception:  # noqa: BLE001
                pass

        page.on("response", _on_response)
        try:
            page.goto(url, wait_until="domcontentloaded")
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=timeout_ms)
                except PWTimeout:
                    pass
            if wait_ms:
                time.sleep(wait_ms / 1000)
        finally:
            page.remove_listener("response", _on_response)
        return captured

    def screenshot(self, url: str, out_path) -> Path:
        """Скриншот страницы — для отладки селекторов и капчи."""
        page = self._page
        page.goto(url, wait_until="domcontentloaded")
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out), full_page=True)
        return out
    
# ──────────────────────────────────────────────────────────────
#  ASYNC-ВАРИАНТ: параллельный обход через один контекст
# ──────────────────────────────────────────────────────────────
try:
    from playwright.async_api import (
        async_playwright, TimeoutError as PWTimeoutAsync,
    )
    _PW_ASYNC_AVAILABLE = True
except ImportError:
    _PW_ASYNC_AVAILABLE = False


class AsyncBrowserFetcher:
    """Async-версия BrowserFetcher: один контекст, много вкладок.

    Sync Playwright не параллелится, поэтому для ускорения обхода нужен
    именно async. Общий контекст = общие cookies и localStorage: антибот
    (Qrator) проходит один раз, дальше все вкладки работают.
    """

    def __init__(self, source_key: str, headless: bool = True,
                 locale: str = "ru-RU"):
        if not _PW_ASYNC_AVAILABLE:
            raise BrowserNotInstalled(
                "Playwright async API не установлен. Выполните:\n"
                "    pip install playwright\n"
                "    playwright install chromium")
        self.source_key = source_key
        self.headless = headless
        self.locale = locale
        self.profile_dir = PROFILES_DIR / source_key
        self.profile_dir.mkdir(exist_ok=True)
        self._pw = None
        self._context = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            user_agent=USER_AGENT,
            locale=self.locale,
            viewport={"width": 1366, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        return self

    async def __aexit__(self, *exc):
        try:
            if self._context:
                await self._context.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def _route_filter(self, route):
        method = route.request.method.upper()
        url = route.request.url
        if method in ("GET", "HEAD", "OPTIONS"):
            await route.continue_()
            return
        if any(marker in url for marker in (
                "/__qrator/", "__qrator", "qauth",
                "/cdn-cgi/", "challenge-platform",
                "/captcha", "/recaptcha", "/hcaptcha")):
            await route.continue_()
            return
        logging.warning("[%s] заблокирован %s %s",
                        self.source_key, method, url)
        await route.abort()

    async def fetch_json(self, url: str, wait_ms: int = 3000):
        """Открывает URL в новой вкладке, ждёт появления JSON в DOM."""
        page = await self._context.new_page()
        try:
            await page.route("**/*", self._route_filter)
            await page.goto(url, wait_until="domcontentloaded")
            try:
                await page.wait_for_function(
                    "() => {const s = (document.body ? document.body.innerText : '').trim();"
                    "return s && (s.startsWith('{') || s.startsWith('['));}",
                    timeout=max(wait_ms, 15000))
            except PWTimeoutAsync:
                pass
            for selector in ("pre", "body"):
                try:
                    text = await page.evaluate(
                        f"() => {{const el = document.querySelector('{selector}'); "
                        f"return el ? el.innerText : '';}}")
                except Exception:  # noqa: BLE001
                    continue
                if text and text.strip().startswith(("{", "[")):
                    try:
                        return json.loads(text)
                    except ValueError:
                        continue
            return None
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass