"""web_stream_mail — IMAP-воркер: следит за ящиком и сам собирает письма.

Одно долгое соединение. Раз в MAIL_POLL_SECONDS — NOOP.
При росте EXISTS — FETCH новых писем через то же соединение, запись
в mail_inbox. Публикует события mail:connected / mail:new.
"""
from __future__ import annotations

import imaplib
import logging
import re
import socket
import sqlite3
import threading
import time
from datetime import datetime
from email import message_from_bytes, policy

from redcat.web.web_stream import publish

_log = logging.getLogger(__name__)

_stop = threading.Event()
_thread: threading.Thread | None = None

MAIL_POLL_SECONDS = 30
MAIL_RECONNECT_DELAY = 60

_stats = {
    "connected": False,
    "last_poll": None,
    "last_new": None,
    "total_new": 0,
    "last_error": None,
    "inbox": 0,
    "last_batch": None,
}
_lock = threading.Lock()


def get_stats() -> dict:
    with _lock:
        return dict(_stats)


def _upd(**kw) -> None:
    with _lock:
        _stats.update(kw)


# ── .env ──
_env_cache: dict = {}
_env_ts: float = 0.0
_env_lock = threading.Lock()


def _read_env() -> dict:
    global _env_cache, _env_ts
    with _env_lock:
        if _env_cache and time.time() - _env_ts < 30:
            return _env_cache
    from redcat.web.web_config import ENV_FILE
    out = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8",
                                        errors="replace").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    with _env_lock:
        _env_cache = out
        _env_ts = time.time()
    return out


# ── парсинг ──
_HDR = ("(FLAGS RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS "
        "(MESSAGE-ID DATE FROM TO CC SUBJECT)])")


def _parse_header(uid: str, folder: str, data) -> dict | None:
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
    except Exception:
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


def _write_messages(rows: list, table: str = "mail_inbox") -> int:
    from redcat.web.web_config import EXTERNAL_DB
    if not rows:
        return 0
    EXTERNAL_DB.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with sqlite3.connect(str(EXTERNAL_DB), timeout=15) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(f"""CREATE TABLE IF NOT EXISTS "{table}" (
                message_id TEXT PRIMARY KEY,
                uid INTEGER, folder TEXT, date TEXT,
                from_name TEXT, from_addr TEXT,
                "to" TEXT, cc TEXT, subject TEXT,
                size INTEGER, attachments INTEGER, seen INTEGER)""")
            existing = {r[1] for r in conn.execute(
                f'PRAGMA table_info("{table}")')}
            needed_txt = {"uid", "folder", "date", "from_name", "from_addr",
                          "to", "cc", "subject", "size", "attachments", "seen"}
            for col in needed_txt - existing:
                try:
                    conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" TEXT')
                except sqlite3.Error:
                    pass
            cols = ["message_id", "uid", "folder", "date", "from_name",
                    "from_addr", "to", "cc", "subject", "size",
                    "attachments", "seen"]
            placeholders = ",".join("?" * len(cols))
            col_sql = ",".join(f'"{c}"' for c in cols)
            for row in rows:
                vals = [row.get(c) for c in cols]
                cur = conn.execute(
                    f'INSERT OR IGNORE INTO "{table}" ({col_sql}) '
                    f'VALUES ({placeholders})', vals)
                written += cur.rowcount or 0
            conn.commit()
    except sqlite3.Error as e:
        _log.warning("stream-mail: не записал: %s", e)
    return written


def _get_max_uid_from_db(table: str = "mail_inbox") -> int:
    from redcat.web.web_config import EXTERNAL_DB
    if not EXTERNAL_DB.exists():
        return 0
    try:
        with sqlite3.connect(f"file:{EXTERNAL_DB}?mode=ro", uri=True) as conn:
            r = conn.execute(f'SELECT MAX(uid) FROM "{table}"').fetchone()
            return int(r[0] or 0)
    except sqlite3.Error:
        return 0


def _fetch_since_uid(conn, folder: str, last_uid: int,
                     max_batch: int = 200) -> list:
    typ, found = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
    if typ != "OK" or not found or not found[0]:
        return []
    uids = found[0].split()
    uids = [u for u in uids if int(u) > last_uid]
    if not uids:
        return []
    if len(uids) > max_batch:
        uids = uids[-max_batch:]
    rows = []
    for uid in uids:
        try:
            typ, data = conn.uid("FETCH", uid, _HDR)
        except imaplib.IMAP4.error as e:
            _log.warning("stream-mail: FETCH %s упал: %s", uid, e)
            break
        if typ != "OK" or not data:
            continue
        try:
            row = _parse_header(uid.decode(), folder, data)
        except Exception as e:
            _log.warning("stream-mail: parse %s: %s", uid, e)
            continue
        if row:
            rows.append(row)
    return rows


def _loop() -> None:
    _log.info("stream-mail: воркер запущен")
    last_error_report = 0
    while not _stop.is_set():
        env = _read_env()
        host = env.get("REDCAT_IMAP_HOST", "")
        port = int(env.get("REDCAT_IMAP_PORT", "993") or 993)
        user = env.get("REDCAT_IMAP_USER", "")
        pw = env.get("REDCAT_IMAP_PASSWORD", "")
        if not (host and user and pw):
            time.sleep(30)
            continue

        conn = None
        try:
            conn = imaplib.IMAP4_SSL(host, port, timeout=120)
            conn.login(user, pw)
            typ, data = conn.select('"INBOX"', readonly=True)
            if typ != "OK":
                raise RuntimeError(f"select INBOX: {typ}")
            try:
                last_exists = int(data[0])
            except (ValueError, TypeError, IndexError):
                last_exists = 0

            last_uid = _get_max_uid_from_db("mail_inbox")
            _upd(connected=True, last_error=None, inbox=last_exists)
            _log.info("stream-mail: подключён. INBOX=%d, last_uid=%d",
                      last_exists, last_uid)
            publish("mail:connected",
                    {"inbox": last_exists, "last_uid": last_uid})

            while not _stop.is_set():
                time.sleep(MAIL_POLL_SECONDS)
                if _stop.is_set():
                    break
                try:
                    conn.noop()
                    raw = conn.untagged_responses.get("EXISTS", [])
                    new_count = last_exists
                    if raw:
                        try:
                            new_count = int(raw[-1])
                        except (ValueError, TypeError):
                            pass

                    if new_count > last_exists:
                        _log.info("stream-mail: новых %d (было %d, стало %d)",
                                  new_count - last_exists, last_exists,
                                  new_count)
                        rows = _fetch_since_uid(conn, "INBOX", last_uid)
                        if rows:
                            written = _write_messages(rows, "mail_inbox")
                            max_uid = max(r["uid"] for r in rows)
                            last_uid = max(last_uid, max_uid)
                            now = datetime.now().isoformat(timespec="seconds")
                            with _lock:
                                _stats.update({
                                    "last_poll": now, "last_new": now,
                                    "total_new": _stats["total_new"] + written,
                                    "inbox": new_count,
                                    "last_batch": {
                                        "fetched": len(rows),
                                        "written": written,
                                        "max_uid": last_uid,
                                    },
                                })
                            publish("mail:new", {
                                "count": written,
                                "fetched": len(rows),
                                "inbox": new_count,
                                "max_uid": last_uid,
                            })
                            _log.info("stream-mail: записано %d, last_uid=%d",
                                      written, last_uid)
                        else:
                            _upd(last_poll=datetime.now().isoformat(
                                timespec="seconds"))
                        last_exists = new_count
                    else:
                        _upd(last_poll=datetime.now().isoformat(
                            timespec="seconds"), inbox=new_count)
                except (socket.timeout, OSError, imaplib.IMAP4.error) as e:
                    _log.warning("stream-mail: NOOP/FETCH упал: %s", e)
                    raise
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            _upd(connected=False, last_error=msg)
            now = time.time()
            if now - last_error_report > 300:
                _log.warning("stream-mail: ошибка: %s", msg)
                last_error_report = now
        finally:
            if conn is not None:
                try: conn.logout()
                except Exception: pass
        for _ in range(MAIL_RECONNECT_DELAY):
            if _stop.is_set(): break
            time.sleep(1)
    _log.info("stream-mail: воркер остановлен")


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="stream-mail", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
