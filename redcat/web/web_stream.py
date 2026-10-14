"""web_stream — шина событий для SSE.

Простая in-process модель: один общий pubsub. Фоновые воркеры
вызывают publish(), SSE-эндпоинт читает очередь каждого клиента.

Хранит небольшой ring buffer последних событий (100 штук) —
это позволяет клиенту после реконнекта получить что пропустил,
если передал Last-Event-ID.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections import deque
from datetime import datetime

# Ограничения
MAX_BUFFER = 100
QUEUE_SIZE = 500
CLIENT_TIMEOUT = 5 * 60  # через 5 минут без событий шлём keepalive


class Subscriber:
    def __init__(self):
        self.q: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.alive = True

    def push(self, event: dict) -> None:
        if not self.alive:
            return
        try:
            self.q.put_nowait(event)
        except queue.Full:
            pass  # медленный клиент — теряем события, это ок

    def close(self) -> None:
        self.alive = False
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass


class Bus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs: dict = {}
        self._buffer: deque = deque(maxlen=MAX_BUFFER)
        self._counter = 0
        self._started_at = datetime.now()

    def subscribe(self) -> Subscriber:
        s = Subscriber()
        with self._lock:
            self._subs[uuid.uuid4().hex] = s
        return s

    def unsubscribe(self, s: Subscriber) -> None:
        s.close()
        with self._lock:
            for k, v in list(self._subs.items()):
                if v is s:
                    del self._subs[k]

    def publish(self, event_type: str, data=None) -> None:
        with self._lock:
            self._counter += 1
            ev = {
                "id": self._counter,
                "type": event_type,
                "ts": datetime.now().isoformat(timespec="seconds"),
                "data": data or {},
            }
            self._buffer.append(ev)
            subs = list(self._subs.values())
        for s in subs:
            s.push(ev)

    def since(self, last_id: int) -> list:
        """События с id > last_id — для восстановления после реконнекта."""
        with self._lock:
            return [e for e in self._buffer if e["id"] > last_id]

    def counts(self) -> dict:
        with self._lock:
            return {
                "subscribers": len(self._subs),
                "counter": self._counter,
                "buffer": len(self._buffer),
                "started_at": self._started_at.isoformat(timespec="seconds"),
            }


# Глобальная шина
BUS = Bus()


def publish(event_type: str, data=None) -> None:
    BUS.publish(event_type, data)


def stats() -> dict:
    return BUS.counts()
