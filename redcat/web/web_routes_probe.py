"""web_routes_probe — пробные запросы к источникам."""
from __future__ import annotations

import json
import time
from redcat.data import dataops
from redcat.sources import registry as src
from redcat.sources import spec_validator
from redcat.web.web_http_util import _is_redcat_host
from redcat.web.web_telegram import read_env_token


class ProbeRoutes:
    """Пробные запросы к источникам."""

    def _probe(self, b):
        import requests
        url = (b.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise dataops.DataError("Укажите полный адрес, начиная с https://")
        params = src.env_params()
        for key, value in params.items():
            url = url.replace("{" + key + "}", str(value))
        token = (b.get("token") or read_env_token()).strip()
        started = time.perf_counter()

        if b.get("fetch_mode") == "browser":
            try:
                from redcat.collection import browser_fetch
            except ImportError:
                return {"ok": False,
                        "error": "browser_fetch.py не найден рядом с webapp.py."}
            try:
                with browser_fetch.BrowserFetcher("probe") as fetcher:
                    html = fetcher.fetch(
                        url,
                        wait_for=b.get("browser_wait_for") or "",
                        wait_ms=int(b.get("browser_wait_ms") or 0))
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            ms = round((time.perf_counter() - started) * 1000)
            preview = html[:4000]
            suggestion = src.suggest_spec(
                url, None, b.get("key", ""), b.get("title", ""),
                b.get("pagination_style", "jsonapi"))
            suggestion["fetch_mode"] = "browser"
            if b.get("browser_wait_for"):
                suggestion["browser_wait_for"] = b["browser_wait_for"]
            if b.get("browser_wait_ms"):
                suggestion["browser_wait_ms"] = int(b["browser_wait_ms"])
            validation = spec_validator.validate_on_sample(
                suggestion,
                html.encode("utf-8") if isinstance(html, str) else html)
            return {"ok": True, "status": 200, "ms": ms,
                    "preview": preview, "suggestion": suggestion,
                    "validation": validation}

        headers = {"Accept": "application/json"}
        if token and _is_redcat_host(url):
            headers["Authorization"] = f"Bearer {token}"
        elif token:
            print(f"  🔒 Токен не отправлен: {url} — не redcat.ai")
        try:
            resp = requests.get(url, headers=headers, timeout=25)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        ms = round((time.perf_counter() - started) * 1000)
        # Пытаемся JSON. Если не выходит — возможно, это XML или HTML,
        # и парсер нужно выбрать по содержимому. suggest_spec принимает
        # и dict, и bytes — во втором случае он сам детектит стратегию
        # (xml_tag / next_data / embedded_json / json_ld).
        payload = None
        try:
            payload = resp.json()
            content_for_suggest = payload
            preview_text = json.dumps(payload, ensure_ascii=False)[:4000]
        except ValueError:
            content_for_suggest = resp.content
            preview_text = resp.text[:4000]

        suggestion = src.suggest_spec(
            b.get("url", url), content_for_suggest, b.get("key", ""),
            b.get("title", ""), b.get("pagination_style", "jsonapi"))
        validation = spec_validator.validate_on_sample(suggestion, resp.content)
        return {"ok": resp.ok, "status": resp.status_code, "ms": ms,
                "preview": preview_text,
                "suggestion": suggestion,
                "validation": validation}

    def _probe_spec(self, b):
        """Прогоняет готовый spec на одном ответе и возвращает отчёт.

        Полезно, когда черновик уже поправлен руками: пользователь нажимает
        «Проверить spec» и сразу видит, сколько записей извлёк парсер, какие
        поля не сошлись и какие стратегии не зарегистрированы.

        Ничего не сохраняет. Регистрация источника — отдельный маршрут
        source_save / source_save_raw.
        """
        spec_dict = b.get("spec") or {}
        url = (b.get("url") or spec_dict.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise dataops.DataError("Укажите URL для проверки.")

        params = src.env_params()
        for key, value in params.items():
            url = url.replace("{" + key + "}", str(value))
        token = (b.get("token") or read_env_token()).strip()
        fetch_mode = spec_dict.get("fetch_mode") or "http"

        if fetch_mode == "browser":
            try:
                from redcat.collection import browser_fetch
            except ImportError:
                return {"ok": False,
                        "error": "browser_fetch.py не найден рядом с webapp.py."}
            spec_obj, _ = spec_validator.to_spec(spec_dict)
            strategy = (spec_obj.fetch_strategy if spec_obj else "") or "browser"
            try:
                with browser_fetch.BrowserFetcher("probe_spec") as fetcher:
                    if strategy == "browser_xhr":
                        captured = fetcher.capture_json_responses(
                            url,
                            (spec_obj.browser_xhr_pattern if spec_obj else "") or "",
                            wait_for=(spec_obj.browser_wait_for if spec_obj else "") or "",
                            wait_ms=int((spec_obj.browser_wait_ms if spec_obj else 0) or 0),
                        )
                        if not captured:
                            return {"ok": False,
                                    "error": "ни одного JSON-XHR не поймано"}
                        raw = json.dumps(captured[0], ensure_ascii=False).encode("utf-8")
                    else:
                        html = fetcher.fetch(
                            url,
                            wait_for=(spec_obj.browser_wait_for if spec_obj else "") or "",
                            wait_ms=int((spec_obj.browser_wait_ms if spec_obj else 0) or 0),
                        )
                        raw = (html.encode("utf-8")
                               if isinstance(html, str) else html)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        else:
            import requests
            headers = {"Accept": "application/json"}
            if token and _is_redcat_host(url):
                headers["Authorization"] = f"Bearer {token}"
            elif token:
                print(f"  🔒 Токен не отправлен: {url} — не redcat.ai")
            try:
                resp = requests.get(url, headers=headers, timeout=25)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if resp.status_code >= 400:
                return {"ok": False, "status": resp.status_code,
                        "error": resp.text[:400]}
            raw = resp.content

        validation = spec_validator.validate_on_sample(spec_dict, raw)
        return {"ok": bool(validation.get("parse_ok")),
                "validation": validation}
