"""scrape_imap — fetch-стратегия "imap": читает письма ТОЛЬКО на чтение.

URL источника:  imap://{imap_host}:993/INBOX?since_days=30&limit=500
Логин/пароль — из .env: REDCAT_IMAP_USER, REDCAT_IMAP_PASSWORD.

Ящик открывается readonly (EXAMINE), тела читаются через BODY.PEEK —
письма не помечаются прочитанными, ничего не удаляется и не двигается.

Дополнительно: fetch_message_body() — точечная выгрузка одного письма
по требованию (используется вкладкой «Почта» в веб-приложении).
"""
from __future__ import annotations

import imaplib
import json
import logging
import os
import re
import urllib.parse
from datetime import datetime, timedelta
from email import message_from_bytes, policy

from redcat.sources import registry as src

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# BODYSTRUCTURE даёт количество вложений без выкачивания тела;
# BODY.PEEK[HEADER.FIELDS ...] забирает только нужные заголовки.
_HDR = ("(FLAGS RFC822.SIZE BODYSTRUCTURE "
        "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE FROM TO CC SUBJECT)])")


def _imap_date(d: datetime) -> str:
    # IMAP требует английские месяцы; strftime зависит от локали — не годится.
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def _read_env_file() -> dict:
    """Читает .env напрямую. Нужно, потому что настройки сохраняются
    на лету через mail_settings.html, а os.environ в уже запущенном
    процессе их не подхватывает."""
    out: dict = {}
    try:
        from redcat.web.web_config import ENV_FILE
        p = ENV_FILE
    except Exception:
        p = None
    if not p or not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _setting(key: str, default: str = "") -> str:
    """os.environ приоритетно, иначе — .env."""
    v = os.environ.get(key)
    if v:
        return v
    return _read_env_file().get(key, default)


def _imap_host_port() -> tuple[str, int]:
    host = _setting("REDCAT_IMAP_HOST", "")
    try:
        port = int(_setting("REDCAT_IMAP_PORT", "993") or 993)
    except (TypeError, ValueError):
        port = 993
    return host, port


def _open_readonly(folder: str):
    """Открывает папку в режиме EXAMINE (readonly). Возвращает соединение
    или поднимает RuntimeError с понятным текстом."""
    host, port = _imap_host_port()
    user = _setting("REDCAT_IMAP_USER", "")
    password = _setting("REDCAT_IMAP_PASSWORD", "")
    if not (host and user and password):
        raise RuntimeError(
            "Не заданы REDCAT_IMAP_HOST / REDCAT_IMAP_USER / "
            "REDCAT_IMAP_PASSWORD в .env")

    conn = imaplib.IMAP4_SSL(host, port, timeout=30)
    conn.login(user, password)
    typ, _ = conn.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"Папка «{folder}» не открылась (readonly).")
    return conn


def _parse_header(uid: str, folder: str, data) -> dict | None:
    """Разбирает ответ FETCH: метаинформацию (FLAGS/SIZE) и заголовки."""
    meta, header = b"", b""
    for part in data:
        if isinstance(part, tuple):
            meta += part[0] or b""
            header = part[1] or b""
        elif isinstance(part, bytes):
            meta += part
    meta_s = meta.decode("utf-8", "replace")
    msg = message_from_bytes(header, policy=policy.default)

    flags = re.search(r"FLAGS \(([^)]*)\)", meta_s)
    size = re.search(r"RFC822\.SIZE (\d+)", meta_s)
    flags_s = flags.group(1) if flags else ""

    try:
        date = msg["date"].datetime.isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001 — кривые Date в рассылках не редкость
        date = None

    sender = msg["from"]
    addr = (sender.addresses[0]
            if sender is not None and sender.addresses else None)
    to_field = msg["to"]
    to_list = to_field.addresses if to_field is not None else ()
    cc_field = msg["cc"]
    cc_list = cc_field.addresses if cc_field is not None else ()

    mid = str(msg["message-id"] or "").strip("<> \r\n")
    return {
        "message_id": mid or f"{folder}:{uid}",
        "uid": int(uid),
        "folder": folder,
        "date": date,
        "from_name": addr.display_name if addr else None,
        "from_addr": addr.addr_spec.lower() if addr else None,
        "to": "; ".join(a.addr_spec for a in to_list) or None,
        "cc": "; ".join(a.addr_spec for a in cc_list) or None,
        "subject": str(msg["subject"] or "").strip() or None,
        "size": int(size.group(1)) if size else None,
        "attachments": len(re.findall(r'"attachment"', meta_s, re.I)),
        "seen": 1 if "\\Seen" in flags_s else 0,
    }


@src.register_fetcher("imap")
def _fetch_imap(url, spec, **_kw):
    """Точка входа, которую вызывает сборщик. Возвращает ответ в том же
    формате, что и HTTP-фетчеры: {status, body, headers, error}."""

    def fail(text):
        return {"status": None, "body": None, "headers": {}, "error": text}

    p = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(p.query)
    folder = urllib.parse.unquote(p.path.lstrip("/")) or "INBOX"
    since_days = int((q.get("since_days") or ["30"])[0])
    limit = int((q.get("limit") or ["500"])[0])

    items: list[dict] = []
    try:
        conn = _open_readonly(folder)
        try:
            since = _imap_date(datetime.now() - timedelta(days=since_days))
            typ, found = conn.uid("SEARCH", None, f"SINCE {since}")
            uids = (found[0].split()[-limit:]
                    if typ == "OK" and found and found[0] else [])
            for uid in uids:
                typ, data = conn.uid("FETCH", uid, _HDR)
                if typ != "OK" or not data:
                    continue
                try:
                    row = _parse_header(uid.decode(), folder, data)
                except Exception as e:  # noqa: BLE001 — одно битое письмо
                    logging.warning("[imap] uid %s не разобран: %s", uid, e)
                    continue
                if row:
                    items.append(row)
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
    except (imaplib.IMAP4.error, OSError, RuntimeError) as e:
        return fail(f"{type(e).__name__}: {e}")

    body = json.dumps({"items": items, "total": len(items)},
                      ensure_ascii=False).encode("utf-8")
    return {"status": 200, "body": body, "headers": {}, "error": None}


def _decode_mime_text(payload: bytes, charset: str | None) -> str:
    """Декодирует байты в строку, перебирая вероятные кодировки."""
    if not payload:
        return ""
    candidates = []
    if charset:
        candidates.append(charset)
    candidates += ["utf-8", "cp1251", "koi8-r", "latin-1"]
    for enc in candidates:
        try:
            return payload.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return payload.decode("utf-8", errors="replace")


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_MULTI_NL_RE = re.compile(r"\n{3,}")
_HTML_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&laquo;": "«", "&raquo;": "»",
    "&mdash;": "—", "&ndash;": "–", "&hellip;": "…",
}


def _html_to_text(html: str) -> str:
    """Грубая очистка HTML: убирает теги, оставляет текст и переносы."""
    if not html:
        return ""
    # Блок-теги → переносы строк
    html = re.sub(r"</?(?:br|p|div|tr|li|h[1-6])\b[^>]*>",
                  "\n", html, flags=re.I)
    # Скрипты и стили — прочь
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", html)
    # Остальные теги — прочь
    html = _HTML_TAG_RE.sub("", html)
    # HTML-сущности
    for ent, ch in _HTML_ENTITIES.items():
        html = html.replace(ent, ch)
    html = re.sub(r"&#(\d+);",
                  lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x10000 else "?",
                  html)
    # Множественные пустые строки — сжать
    html = re.sub(r"[ \t]+", " ", html)
    html = _HTML_MULTI_NL_RE.sub("\n\n", html)
    return html.strip()


def _extract_best_part(msg) -> tuple[str, str]:
    """Обходит MIME-дерево. Возвращает (текст, mime_type).

    Приоритет:
      1. text/plain — как есть;
      2. text/html — с грубой очисткой;
      3. первая часть с текстом — как есть.
    """
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    other_chunks: list[str] = []

    def walk(part):
        ctype = (part.get_content_type() or "").lower()
        # Метаданные — не трогаем
        if ctype.startswith("multipart/"):
            if part.is_multipart():
                for sub in part.get_payload():
                    walk(sub)
            return
        # Вложения — не трогаем
        disp = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disp:
            return
        payload = part.get_payload(decode=True)
        if payload is None:
            # Некоторые письма имеют payload как строку без decode
            raw = part.get_payload()
            if isinstance(raw, str):
                payload = raw.encode("utf-8", errors="replace")
            else:
                return
        charset = part.get_content_charset()
        text = _decode_mime_text(payload, charset).strip()
        if not text:
            return
        if ctype == "text/plain":
            plain_chunks.append(text)
        elif ctype == "text/html":
            html_chunks.append(_html_to_text(text))
        else:
            other_chunks.append(text)

    walk(msg)
    if plain_chunks:
        return "\n\n".join(plain_chunks), "text/plain"
    if html_chunks:
        return "\n\n".join(html_chunks), "text/html (очищено)"
    if other_chunks:
        return "\n\n".join(other_chunks), "прочее"
    return "", "пустое"


def fetch_message_body(folder: str, uid: int, max_chars: int = 20000) -> dict:
    """Читает тело одного письма. Внутри делает retry при сетевых
    сбоях — Яндекс закрывает второе IMAP-соединение подряд, если оно
    открыто слишком быстро после первого."""
    import time as _time
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            return _fetch_body_once(folder, uid, max_chars)
        except Exception as e:  # noqa: BLE001
            last_err = e
            msg = str(e)
            # Ретраим только сетевые сбои и «оборванные» соединения.
            retryable = any(k in msg for k in (
                "SSL", "EOF", "timed out", "Timeout", "Connection",
                "abort", "socket", "reset",
            ))
            if not retryable or attempt >= 2:
                raise
            _time.sleep(1.5 * (attempt + 1))
    if last_err:
        raise last_err
    raise RuntimeError("fetch_message_body: недостижимо")


def _fetch_body_once(folder: str, uid: int, max_chars: int = 20000) -> dict:
    """Читает тело одного письма. Парсит MIME, декодирует base64/QP
    и charset. Возвращает чистый текст.

    Вызывается только по явному действию пользователя — тела писем
    не сохраняются на диск и не попадают в FTS-индекс.
    """
    conn = _open_readonly(folder)
    try:
        # Забираем письмо целиком (RFC822). Для коротких писем это
        # дёшево, для длинных — обрезаем на стороне сервера.
        # BODY.PEEK[] не помечает письмо прочитанным.
        typ, data = conn.uid(
            "FETCH", str(uid),
            "(BODY.PEEK[]<0.500000>)")
        if typ != "OK" or not data:
            raise RuntimeError(f"Письмо uid={uid} не найдено в «{folder}».")

        raw = b""
        for part in data:
            if isinstance(part, tuple):
                raw = part[1] or b""
            elif isinstance(part, bytes):
                raw = part
            if raw:
                break

        if not raw:
            raise RuntimeError(f"Пустой ответ от сервера для uid={uid}.")

        from email import message_from_bytes, policy
        msg = message_from_bytes(raw, policy=policy.default)

        text, kind = _extract_best_part(msg)
        if not text:
            text = "(тело письма пустое или не распознано)"

        truncated = False
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True

        return {
            "uid": uid,
            "folder": folder,
            "content_type": kind,
            "body": text,
            "truncated": truncated,
        }
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass




def fetch_message_raw(folder: str, uid: int,
                      max_bytes: int = 5_000_000) -> bytes:
    """Возвращает сырые байты письма (.eml). Для сохранения.

    Не помечает письмо прочитанным. Ограничение max_bytes — защита от
    слишком больших писем (по умолчанию 5 МБ, отдаём первые 5 МБ).
    """
    conn = _open_readonly(folder)
    try:
        typ, data = conn.uid(
            "FETCH", str(uid),
            f"(BODY.PEEK[]<0.{max_bytes}>)")
        if typ != "OK" or not data:
            raise RuntimeError(f"Письмо uid={uid} не найдено в «{folder}».")
        raw = b""
        for part in data:
            if isinstance(part, tuple):
                raw = part[1] or b""
            elif isinstance(part, bytes):
                raw = part
            if raw:
                break
        return raw
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass

