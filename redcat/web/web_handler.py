"""web_handler — HTTP-обработчик: базовые методы, статика, диспетчеризация GET/POST."""
from __future__ import annotations

import json
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler
from redcat.data import dataops
from redcat.web.web_config import WEB_DIR
from redcat.web.web_data import _json_safe
from redcat.web.web_http_util import MAX_BODY_BYTES
from redcat.web.web_routes_csv import CsvRoutes
from redcat.web.web_routes_data import DataRoutes
from redcat.web.web_routes_export import ExportRoutes
from redcat.web.web_routes_get import GetRoutes
from redcat.web.web_routes_post import PostRoutes
from redcat.web.web_routes_probe import ProbeRoutes


class Handler(GetRoutes, PostRoutes, DataRoutes, CsvRoutes, ProbeRoutes, ExportRoutes, BaseHTTPRequestHandler):
    server_version = "RedCatStudio"

    def log_message(self, fmt, *args):
        pass

    def _send(self, payload, status=200, content_type="application/json",
              headers=None):
        if isinstance(payload, bytes):
            body = payload
        elif content_type.startswith("application/json"):
            body = json.dumps(_json_safe(payload), ensure_ascii=False,
                              default=str).encode("utf-8")
        else:
            body = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _error(self, message, status=400):
        self._send({"error": str(message)}, status)

    def _body(self):
        """Читает JSON-тело POST. Возвращает dict или None — если запрос
        уже отклонён (тогда вызывающий обязан НЕ продолжать, иначе в одно
        соединение уйдёт второй ответ)."""
        raw_length = self.headers.get("Content-Length") or 0
        try:
            length = int(raw_length)
        except (TypeError, ValueError):
            self._error("Некорректный Content-Length.", 400)
            return None
        if length < 0:
            self._error("Отрицательный Content-Length.", 400)
            return None
        if length > MAX_BODY_BYTES:
            self._error(
                f"Слишком большой запрос: {length} байт. "
                f"Лимит {MAX_BODY_BYTES // (1024 * 1024)} МБ.", 413)
            return None
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ---------- защита локального сервера ----------
    def _guard(self, is_post: bool) -> bool:
        """Host + Origin + Content-Type. Возвращает False, если запрос отклонён."""
        allowed = getattr(self.server, "allowed_hosts", None)
        if allowed:
            host = (self.headers.get("Host") or "").lower()
            if host not in allowed:
                self._error("Недопустимый Host.", 403)
                return False
            origin = (self.headers.get("Origin") or "").lower()
            if origin and origin not in {f"http://{h}" for h in allowed}:
                self._error("Недопустимый Origin.", 403)
                return False
        if is_post:
            ctype = (self.headers.get("Content-Type") or "")
            ctype = ctype.split(";")[0].strip().lower()
            if ctype != "application/json":
                self._error("Ожидается Content-Type: application/json.", 415)
                return False
        return True

    # ---------- GET ----------
    def do_GET(self):
        if not self._guard(False):
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self._api_get(path[5:], params)
            return self._static(path)
        except dataops.DataError as e:
            return self._error(e)
        except Exception as e:  # noqa: BLE001
            return self._error(f"{type(e).__name__}: {e}", 500)

    def _static(self, path):
        if path in ("/", "/index.html"):
            path = "/index.html"
        target = (WEB_DIR / path.lstrip("/")).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._send("404", 404, "text/plain; charset=utf-8")
        if not target.is_file():
            return self._send("404", 404, "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(target.read_bytes(), 200, ctype)

    # ---------- POST (LOCAL-ONLY: пишет только на ваш диск) ----------
    def do_POST(self):
        if not self._guard(True):
            return
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return self._error("Только /api/*", 404)
        route = parsed.path[5:]
        body = self._body()
        if body is None:
            return  # ответ уже отправлен (_body вернул None)
        try:
            return self._api_post(route, body)
        except dataops.DataError as e:
            return self._error(e)
        except Exception as e:  # noqa: BLE001
            return self._error(f"{type(e).__name__}: {e}", 500)
