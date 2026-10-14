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
from redcat.web.web_routes_mail import MailRoutes
from redcat.web.web_routes_probe import ProbeRoutes


class Handler(GetRoutes, PostRoutes, DataRoutes, CsvRoutes, ProbeRoutes, ExportRoutes, MailRoutes, BaseHTTPRequestHandler):
    server_version = "RedCatStudio"

    def log_message(self, fmt, *args):
        pass

    # Сетевые ошибки, которые значат «клиент ушёл раньше, чем мы ответили».
    # Не считаем их проблемой — просто молча выходим.
    _NET_ERRORS = (
        BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
        ConnectionError, OSError,
    )

    _NET_ERRORS = (
        BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
        ConnectionError, OSError,
    )

    _NET_ERRORS = (
        BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
        ConnectionError, OSError,
    )

    def _send(self, payload, status=200, content_type="application/json",
              headers=None):
        try:
            if isinstance(payload, bytes):
                body = payload
            elif content_type.startswith("application/json"):
                body = json.dumps(_json_safe(payload), ensure_ascii=False,
                                  default=str).encode("utf-8")
            else:
                body = str(payload).encode("utf-8")
        except Exception:
            return
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except self._NET_ERRORS:
            # Клиент отключился — не наша проблема
            pass

    def _error(self, message, status=400):
        try:
            self._send({"error": str(message)}, status)
        except Exception:
            pass

    def handle_one_request(self):
        """Перехватываем сетевые обрывы на уровне сокета, чтобы
        socketserver не печатал traceback в консоль."""
        try:
            super().handle_one_request()
        except self._NET_ERRORS:
            self.close_connection = True

    def handle(self):
        try:
            super().handle()
        except self._NET_ERRORS:
            pass

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


    def _api_stream_status_safe(self, p):
        """Безопасный вариант: любые исключения возвращаются как JSON.
        Причина — какие-то словари не хэшируются, ловим и отдаём текст."""
        import traceback
        try:
            from redcat.web.web_stream import stats as _st_bus
            try:
                bus = _st_bus()
            except Exception as e:
                bus = {"error": f"{type(e).__name__}: {e}"}
            try:
                from redcat.web.web_stream_mail import get_stats as _st_w
                workers = _st_w()
            except Exception as e:
                workers = {"error": f"{type(e).__name__}: {e}"}
            return self._send({"bus": bus, "workers": workers})
        except Exception as e:
            tb = traceback.format_exc()
            return self._send({"error": f"{type(e).__name__}: {e}",
                               "trace": tb[-2000:]}, 500)

    def handle_error(self, request, client_address):
        """Не печатать traceback при обрыве соединения."""
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError, ConnectionError)):
            return
        super().handle_error(request, client_address)

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




    # ──────────────────────────────────────────────────────────────
    #  SSE-стриминг (patch_streaming.py)
    # ──────────────────────────────────────────────────────────────
    def _one(self, p, key, default=None):
        """Достаёт первое значение из parse_qs-словаря.
        Используется в _api_stream и других вспомогательных методах."""
        v = p.get(key) if isinstance(p, dict) else None
        if isinstance(v, list):
            return v[0] if v else default
        return v if v is not None else default

    def _api_stream(self, p: dict):
        """Server-Sent Events. Держит соединение открытым и пишет
        события из общей шины."""
        import queue as _q
        import time as _t
        import json as _json
        from redcat.web.web_stream import BUS

        # Последний ID — если клиент переподключился
        last_id_raw = self.headers.get("Last-Event-ID") or self._one(p, "last_id") or "0"
        try:
            last_id = int(last_id_raw)
        except (TypeError, ValueError):
            last_id = 0

        sub = BUS.subscribe()
        try:
            # Заголовки SSE
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def _write(text: str):
                try:
                    self.wfile.write(text.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    raise

            # Приветствие
            _write(": redcat-stream\n\n")

            # Досылаем пропущенные
            for ev in BUS.since(last_id):
                payload = _json.dumps({
                    "type": ev["type"], "ts": ev["ts"], "data": ev["data"],
                }, ensure_ascii=False)
                _write(f"id: {ev['id']}\nevent: {ev['type']}\ndata: {payload}\n\n")

            # Основной цикл
            last_keepalive = _t.time()
            while True:
                try:
                    ev = sub.q.get(timeout=20)
                except _q.Empty:
                    # keep-alive раз в 20 сек
                    try:
                        _write(": ping\n\n")
                    except Exception:
                        break
                    continue
                if ev is None:
                    break
                payload = _json.dumps({
                    "type": ev["type"], "ts": ev["ts"], "data": ev["data"],
                }, ensure_ascii=False)
                try:
                    _write(f"id: {ev['id']}\nevent: {ev['type']}\n"
                           f"data: {payload}\n\n")
                except Exception:
                    break
        finally:
            BUS.unsubscribe(sub)
        return None

