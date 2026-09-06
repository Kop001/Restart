"""Очередь подтверждений для веб-панели.

В терминале разрешение спрашивается через input(). В панели запрос приходит
в одном HTTP-потоке, а ответ владельца — в другом, поэтому вызывающий поток
блокируется на Event, пока UI не пришлёт решение.
"""

from __future__ import annotations

import threading
import time
import uuid


class ApprovalQueue:
    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = timeout
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}

    def request(self, action: str) -> bool:
        """Блокирует поток инструмента до ответа владельца или таймаута."""
        request_id = uuid.uuid4().hex[:8]
        event = threading.Event()
        record = {
            "id": request_id,
            "action": action,
            "created_at": time.time(),
            "event": event,
            "approved": False,
        }
        with self._lock:
            self._pending[request_id] = record

        granted = event.wait(self.timeout) and record["approved"]
        with self._lock:
            self._pending.pop(request_id, None)
        return granted

    def pending(self) -> list[dict]:
        with self._lock:
            return [
                {"id": r["id"], "action": r["action"], "age": round(time.time() - r["created_at"])}
                for r in self._pending.values()
            ]

    def resolve(self, request_id: str, approved: bool) -> bool:
        with self._lock:
            record = self._pending.get(request_id)
            if record is None:
                return False
            record["approved"] = approved
            record["event"].set()
            return True
