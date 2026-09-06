"""Правила: что Джарвис решает без модели.

Это главное решение по приватности, а не по деньгам. Событие, разобранное
правилом, никогда не покидает машину — модель его не видит. Письмо, опознанное
как рассылка, не уедет в API не потому что дорого, а потому что незачем.

Модель зовётся только для того, что правила не разобрали, и не чаще, чем
позволяет бюджет.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from .config import Config
from .events import SILENT, SPEAK, URGENT, Event

BY_RULE = "правило"
BY_MODEL = "модель"


@dataclass
class Decision:
    outcome: str
    why: str


def decide(event: Event, config: Config, now: datetime | None = None) -> Decision | None:
    """Решение без модели. None — правила не разобрали, нужна модель.

    Порядок важен: сначала то, что заведомо требует голоса, потом то, что
    заведомо не требует. Спорное уходит модели.
    """
    now = now or datetime.now()

    # Человек обратился — отвечаем всегда, никакой порог тут не применим.
    if event.source == "человек":
        return Decision(SPEAK, "человек обратился")

    # То, что владелец сам завёл: напоминание, поручение.
    if event.kind in ("напоминание", "задача-готова", "задача-сорвалась"):
        return Decision(SPEAK, "владелец сам этого ждал")

    # Ночью молчим — кроме прямо важного.
    if _is_quiet(now, config) and event.weight < URGENT:
        return Decision(SILENT, "часы тишины")

    # Ниже порога вмешательства не стоит того, чтобы прерывать человека.
    if event.weight < config.speak_threshold:
        return Decision(SILENT, f"вес {event.weight} ниже порога {config.speak_threshold}")

    # Заведомо шумное: служебные каталоги, автоматические уведомления.
    if event.kind == "файл" and _is_noise(event.details.get("path", "")):
        return Decision(SILENT, "служебный каталог")

    return None


def _is_quiet(now: datetime, config: Config) -> bool:
    """Попадает ли текущее время в часы тишины (промежуток через полночь)."""
    start, end = config.quiet_from, config.quiet_to
    if start == end:
        return False
    hour = now.hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


NOISE = ("/.cache/", "/node_modules/", "/__pycache__/", "/.git/", "/tmp/")


def _is_noise(path: str) -> bool:
    return any(part in path for part in NOISE)


class ModelBudget:
    """Потолок обращений к модели в час.

    Без него один шумный источник событий превращает Джарвиса в спамера,
    а счёт — в неприятный сюрприз.
    """

    def __init__(self, per_hour: int) -> None:
        self.per_hour = per_hour
        self._calls: list[float] = []

    def allow(self) -> bool:
        if self.per_hour <= 0:
            return False
        edge = time.time() - 3600
        self._calls = [t for t in self._calls if t >= edge]
        return len(self._calls) < self.per_hour

    def spend(self) -> None:
        self._calls.append(time.time())

    @property
    def left(self) -> int:
        edge = time.time() - 3600
        self._calls = [t for t in self._calls if t >= edge]
        return max(0, self.per_hour - len(self._calls))
