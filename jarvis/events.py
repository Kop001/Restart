"""События: единственный вход в систему.

Раньше у Джарвиса был один вход — человек заговорил. Помощнику нужен второй,
и он важнее: мир изменился. Напоминание наступило, задача закончилась, пришло
письмо. Всё это одна сущность — событие, и обрабатывается одним циклом.

Приватность заложена в устройство, а не прикручена сверху:

- событие несёт приметы, а не содержимое: тема письма, а не письмо;
- разобранное правилом никогда не покидает машину — модель не зовётся;
- у каждой записи видно, отправлялась ли она модели;
- хроника живёт ограниченный срок, иначе за год превращается в досье.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .memory import secure

# Что Джарвис решает сделать с событием.
SILENT, REMEMBER, ACT, SPEAK = "промолчать", "запомнить", "сделать", "сказать"
OUTCOMES = (SILENT, REMEMBER, ACT, SPEAK)

# Вес, выше которого молчать нельзя даже ночью.
URGENT = 90


@dataclass
class Event:
    """Одно происшествие. Хранит приметы, а не содержимое."""

    source: str
    kind: str
    text: str
    weight: int = 50
    details: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    at: float = field(default_factory=time.time)

    # --- как обработано ---
    outcome: str = ""
    decided_by: str = ""
    sent_to_model: bool = False
    handled_at: float = 0.0
    attempts: int = 0
    note: str = ""

    @property
    def pending(self) -> bool:
        return not self.outcome

    def as_dict(self) -> dict:
        return asdict(self)


class EventLog:
    """Очередь и хроника в одном журнале.

    Необработанное — очередь, обработанное — хроника. Разделять их незачем:
    лента показывает и то и другое, а порядок один.

    Журнал лежит на диске, чтобы перезапуск ничего не терял.
    """

    def __init__(self, path: Path, keep_days: int = 30, persist: bool = True) -> None:
        self.path = path
        self.keep_days = keep_days
        self.persist = persist
        self._lock = threading.Lock()
        self.events: list[Event] = [Event(**raw) for raw in _read(path)] if persist else []
        self._forget_old()

    # --- запись ---

    def add(self, event: Event) -> Event:
        with self._lock:
            self.events.append(event)
            self._save_locked()
        return event

    def emit(self, source: str, kind: str, text: str, weight: int = 50, **details) -> Event:
        """Короткая запись события — так его кладут источники."""
        return self.add(Event(source=source, kind=kind, text=text, weight=weight,
                              details=details))

    def resolve(
        self,
        event: Event,
        outcome: str,
        decided_by: str,
        sent_to_model: bool = False,
        note: str = "",
    ) -> Event:
        """Отмечает, что с событием сделали и кто это решил."""
        if outcome not in OUTCOMES:
            raise ValueError(f"неизвестный исход: {outcome}")
        with self._lock:
            event.outcome = outcome
            event.decided_by = decided_by
            event.sent_to_model = sent_to_model
            event.note = note
            event.handled_at = time.time()
            self._save_locked()
        return event

    def defer(self, event: Event) -> Event:
        """Возвращает событие в очередь после сбоя."""
        with self._lock:
            event.attempts += 1
            self._save_locked()
        return event

    # --- чтение ---

    def pending(self) -> list[Event]:
        with self._lock:
            return [e for e in self.events if e.pending and e.attempts < 3]

    def recent(self, limit: int = 100) -> list[Event]:
        """Лента: новое сверху."""
        with self._lock:
            return sorted(self.events, key=lambda e: -e.at)[:limit]

    def spoken(self) -> list[Event]:
        with self._lock:
            return [e for e in self.events if e.outcome == SPEAK]

    def survey(self) -> dict:
        """Сводка для панели приватности."""
        with self._lock:
            return {
                "всего": len(self.events),
                "в очереди": sum(1 for e in self.events if e.pending),
                "уходило модели": sum(1 for e in self.events if e.sent_to_model),
            }

    # --- срок жизни ---

    def _forget_old(self) -> None:
        """Хроника без срока за год превращается в досье."""
        if self.keep_days <= 0:
            return
        edge = time.time() - self.keep_days * 86400
        with self._lock:
            kept = [e for e in self.events if e.at >= edge or e.pending]
            if len(kept) != len(self.events):
                self.events = kept
                self._save_locked()

    def clear(self) -> None:
        with self._lock:
            self.events = []
            self._save_locked()

    def _save_locked(self) -> None:
        if not self.persist:
            return
        _write(self.path, [e.as_dict() for e in self.events])


def _read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _write(path: Path, payload: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    secure(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    secure(tmp)
    tmp.replace(path)
