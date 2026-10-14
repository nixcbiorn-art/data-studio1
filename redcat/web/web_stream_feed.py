"""web_stream_feed — фидовый воркер: следит за изменениями таблиц RedCat.

Раз в FEED_POLL_SECONDS считает COUNT(*) по каждой таблице внешних
источников. Если число изменилось — событие feed:update.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime

from redcat.web.web_stream import publish

_log = logging.getLogger(__name__)

_stop = threading.Event()
_thread: threading.Thread | None = None

FEED_POLL_SECONDS = 120

_stats = {
    "last_poll": None,
    "last_new": None,
    "total_new": 0,
    "last_error": None,
    "sources": 0,
}
_lock = threading.Lock()


def get_stats() -> dict:
    with _lock:
        return dict(_stats)


def _upd(**kw) -> None:
    with _lock:
        _stats.update(kw)


def _snapshot() -> dict:
    from redcat.web.web_data import db_for_table, load_specs
    out = {}
    for key in load_specs():
        try:
            db = db_for_table(key)
            if not db.exists():
                continue
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                n = conn.execute(
                    f'SELECT COUNT(*) FROM "{key}"').fetchone()[0]
                last = conn.execute(
                    f'SELECT MAX(rowid) FROM "{key}"').fetchone()[0]
                out[key] = {"count": n, "last_id": last}
        except Exception:
            continue
    return out


def _loop() -> None:
    _log.info("stream-feed: воркер запущен")
    prev = _snapshot()
    with _lock:
        _stats["sources"] = len(prev)
    while not _stop.is_set():
        for _ in range(FEED_POLL_SECONDS):
            if _stop.is_set(): break
            time.sleep(1)
        if _stop.is_set(): break
        try:
            cur = _snapshot()
            changes = []
            for key, info in cur.items():
                p = prev.get(key)
                if not p: continue
                if info["count"] != p["count"]:
                    changes.append({
                        "source": key,
                        "before": p["count"],
                        "after": info["count"],
                        "delta": info["count"] - p["count"],
                    })
            now = datetime.now().isoformat(timespec="seconds")
            if changes:
                with _lock:
                    _stats["last_poll"] = now
                    _stats["last_new"] = now
                    _stats["total_new"] = _stats["total_new"] + len(changes)
                    _stats["sources"] = len(cur)
                publish("feed:update", {"changes": changes})
                _log.info("stream-feed: изменений: %d", len(changes))
            else:
                _upd(last_poll=now, sources=len(cur))
            prev = cur
        except Exception as e:
            _upd(last_error=f"{type(e).__name__}: {e}")
    _log.info("stream-feed: воркер остановлен")


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="stream-feed", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
