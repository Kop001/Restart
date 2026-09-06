"""Очередь запросов на разрешение — единственная на всю систему.

Разрешение спрашивают из разных потоков: основной диалог, каждый фоновый
исполнитель, обработчик HTTP-запроса панели. Ответ владельца приходит совсем
из другого потока — из консоли, с микрофона или из окна панели. Поэтому
запрашивающий блокируется на Event, а ответить может кто угодно.

Владелец решает всё сам: автоматических отказов здесь нет, есть только
таймаут ожидания — на случай, если у экрана никого не осталось.
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

    def request(self, action: str, source: str = "диалог") -> bool:
        """Блокирует поток инструмента до ответа владельца или таймаута.

        Args:
            action: Что именно просят разрешить.
            source: Кто просит — основной диалог или фоновая задача.
        """
        request_id = uuid.uuid4().hex[:8]
        event = threading.Event()
        record = {
            "id": request_id,
            "action": action,
            "source": source,
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
                {
                    "id": r["id"],
                    "action": r["action"],
                    "source": r["source"],
                    "age": round(time.time() - r["created_at"]),
                }
                for r in sorted(self._pending.values(), key=lambda r: r["created_at"])
            ]

    def resolve_first(self, approved: bool) -> bool:
        """Отвечает на самый старый ожидающий запрос.

        Так удобнее консоли и голосу: там владелец отвечает на то, что у него
        сейчас перед глазами, а не выбирает запрос по идентификатору.
        """
        with self._lock:
            records = sorted(self._pending.values(), key=lambda r: r["created_at"])
            if not records:
                return False
            request_id = records[0]["id"]
        return self.resolve(request_id, approved)

    def resolve(self, request_id: str, approved: bool) -> bool:
        with self._lock:
            record = self._pending.get(request_id)
            if record is None:
                return False
            record["approved"] = approved
            record["event"].set()
            return True
