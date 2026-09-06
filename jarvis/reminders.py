"""Напоминания: хранение и фоновый планировщик.

Планировщик крутится в демон-потоке, раз в секунду смотрит на список и
отдаёт «созревшие» напоминания через callback — обычно это озвучка.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

_RELATIVE = re.compile(
    r"(?:через\s+)?(\d+)\s*(сек|секунд\w*|мин|минут\w*|час\w*|дн\w*|s|sec|m|min|h|hour|d|day)s?\b",
    re.IGNORECASE,
)

_UNIT_SECONDS = {
    "сек": 1, "s": 1, "sec": 1,
    "мин": 60, "m": 60, "min": 60,
    "час": 3600, "h": 3600, "hour": 3600,
    "дн": 86400, "d": 86400, "day": 86400, "ден": 86400,
}


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """Разбирает «через 10 минут», «в 18:30», «2026-09-07 09:00».

    Кидает ValueError, если формат не распознан, — модель увидит ошибку
    в результате вызова инструмента и попробует другой формат.
    """
    now = now or datetime.now()
    text = text.strip().lower()

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass

    match = re.fullmatch(r"(?:в\s+|at\s+)?(\d{1,2})[:.](\d{2})", text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    match = _RELATIVE.search(text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        for prefix, seconds in _UNIT_SECONDS.items():
            if unit.startswith(prefix):
                return now + timedelta(seconds=amount * seconds)

    raise ValueError(
        f"не понял время {text!r}; используй «через N минут», «в 18:30» или «2026-09-07 09:00»"
    )


class Reminders:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        try:
            self.items: list[dict] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.items = []

    def add(self, text: str, when: str) -> dict:
        due = parse_when(when)
        item = {
            "id": uuid.uuid4().hex[:8],
            "text": text.strip(),
            "due": due.strftime("%Y-%m-%d %H:%M:%S"),
            "done": False,
        }
        with self._lock:
            self.items.append(item)
            self._save()
        return item

    def pending(self) -> list[dict]:
        with self._lock:
            return [i for i in self.items if not i["done"]]

    def cancel(self, reminder_id: str) -> bool:
        with self._lock:
            for item in self.items:
                if item["id"] == reminder_id and not item["done"]:
                    item["done"] = True
                    self._save()
                    return True
        return False

    def pop_due(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now()
        fired = []
        with self._lock:
            for item in self.items:
                if item["done"]:
                    continue
                if datetime.strptime(item["due"], "%Y-%m-%d %H:%M:%S") <= now:
                    item["done"] = True
                    fired.append(item)
            if fired:
                self._save()
        return fired

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


class ReminderScheduler:
    """Фоновый поток, который отдаёт наступившие напоминания в callback."""

    def __init__(
        self,
        reminders: Reminders,
        on_fire: Callable[[dict], None],
        interval: float = 1.0,
    ) -> None:
        self.reminders = reminders
        self.on_fire = on_fire
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="jarvis-reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            for item in self.reminders.pop_due():
                try:
                    self.on_fire(item)
                except Exception:  # напоминание не должно ронять поток
                    pass
