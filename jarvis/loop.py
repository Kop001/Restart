"""Цикл обработки событий.

Один поток разбирает очередь: сначала правилами, и только неразобранное —
моделью, если позволяет бюджет. Всё, что решено правилом, машину не покидает.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .config import Config
from .events import SILENT, SPEAK, Event, EventLog
from .rules import BY_MODEL, BY_RULE, ModelBudget, decide


class EventWorker:
    """Разбирает события в фоне и зовёт голос, когда есть что сказать."""

    def __init__(
        self,
        log: EventLog,
        config: Config,
        *,
        speak: Callable[[Event], None],
        ask_model: Callable[[Event], tuple[str, str]] | None = None,
        interval: float = 1.0,
    ) -> None:
        self.log = log
        self.config = config
        self.speak = speak
        self.ask_model = ask_model
        self.interval = interval
        self.budget = ModelBudget(config.model_calls_per_hour)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="jarvis-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def drain(self) -> int:
        """Разбирает всё, что накопилось. Возвращает число разобранных."""
        handled = 0
        for event in self.log.pending():
            if self._handle(event):
                handled += 1
        return handled

    # --- разбор одного события ---

    def _handle(self, event: Event) -> bool:
        try:
            verdict = decide(event, self.config)
            if verdict is not None:
                self.log.resolve(event, verdict.outcome, BY_RULE, note=verdict.why)
            else:
                self._ask(event)
        except Exception as exc:
            # Сбой не должен терять событие: вернём в очередь, но не навечно.
            self.log.defer(event)
            event.note = f"{type(exc).__name__}: {exc}"
            return False

        if event.outcome == SPEAK:
            try:
                self.speak(event)
            except Exception:
                pass
        return True

    def _ask(self, event: Event) -> None:
        """Спрашивает модель — но только если есть кого спросить и на что."""
        if self.ask_model is None or not self.budget.allow():
            why = "нечем спросить" if self.ask_model is None else "бюджет модели исчерпан"
            self.log.resolve(event, SILENT, BY_RULE, note=why)
            return

        self.budget.spend()
        outcome, note = self.ask_model(event)
        self.log.resolve(event, outcome, BY_MODEL, sent_to_model=True, note=note)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.drain()
            except Exception:
                pass
