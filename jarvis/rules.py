"""Правила: что Джарвис решает без модели.

Это главное решение по приватности, а не по деньгам. Событие, разобранное
правилом, никогда не покидает машину — модель его не видит. Письмо, опознанное
как рассылка, не уедет в API не потому что дорого, а потому что незачем.

Модель зовётся только для того, что правила не разобрали, и не чаще, чем
позволяет бюджет.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import Config
from .events import SILENT, SPEAK, URGENT, Event
from .memory import secure

BY_RULE = "правило"
BY_MODEL = "модель"


@dataclass
class Decision:
    outcome: str
    why: str


class Attention:
    """Память о том, на что владелец махнул рукой.

    Порог одинаковый для всех событий — плохой порог. Если человек трижды
    отмахнулся от писем определённого рода, четвёртый раз спрашивать не надо:
    поднимаем планку именно для них, а не для всего сразу.
    """

    # На сколько поднимается планка за каждый отказ и докуда максимум.
    STEP = 15
    CEILING = 45

    def __init__(self, path: Path, persist: bool = True) -> None:
        self.path = path
        self.persist = persist
        self._lock = threading.Lock()
        try:
            self.counts: dict[str, int] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.counts = {}

    def dismiss(self, source: str, kind: str) -> int:
        """Владелец отмахнулся: в следующий раз планка выше."""
        key = f"{source}/{kind}"
        with self._lock:
            self.counts[key] = self.counts.get(key, 0) + 1
            self._save()
            return self.counts[key]

    def welcome(self, source: str, kind: str) -> None:
        """Владелец отреагировал: планку возвращаем обратно."""
        key = f"{source}/{kind}"
        with self._lock:
            if self.counts.pop(key, None) is not None:
                self._save()

    def raised_by(self, event: Event) -> int:
        return min(self.CEILING, self.STEP * self.counts.get(f"{event.source}/{event.kind}", 0))

    def _save(self) -> None:
        if not self.persist:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        secure(self.path.parent)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.counts, ensure_ascii=False, indent=2), encoding="utf-8")
        secure(tmp)
        tmp.replace(self.path)


def decide(
    event: Event,
    config: Config,
    now: datetime | None = None,
    attention: Attention | None = None,
) -> Decision | None:
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
    # Порог поднят для того, от чего владелец уже отмахивался.
    threshold = config.speak_threshold + (attention.raised_by(event) if attention else 0)
    if event.weight < threshold:
        return Decision(SILENT, f"вес {event.weight} ниже порога {threshold}")

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
