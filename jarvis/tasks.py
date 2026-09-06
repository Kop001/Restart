"""Фоновые задачи: Джарвис ведёт несколько дел одновременно.

Основной диалог остаётся отзывчивым, а долгая работа уходит отдельным
исполнителям. Исполнитель — это тот же агент, но со своей историей, своей
политикой подтверждений и без права плодить новых исполнителей.

Что общее у всех: клиент API (он потокобезопасен), долговременная память и
список напоминаний. Что своё: история диалога, счётчики, набор инструментов.
"""

from __future__ import annotations

import dataclasses
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

import anthropic

from .config import Config
from .memory import Memory
from .reminders import Reminders

WORKER_PERSONA = """\
Ты — фоновый исполнитель Джарвиса. Тебе поручена одна задача, владельца рядом нет
и спросить не у кого.

Правила:
- Доводи задачу до конца сам, доступными инструментами.
- Не задавай уточняющих вопросов: их некому прочитать. Если данных не хватает —
  сделай разумное допущение и назови его в отчёте.
- Если инструмент отказал (например, нет разрешения на запись) — не обходи
  запрет, а зафиксируй это в отчёте.
- Последнее сообщение — краткий отчёт: что сделано, что не получилось и почему.
  Пять предложений максимум, его прочитают вслух.
"""

# Состояния задачи. Из running можно попасть в любое из трёх конечных.
QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
FINAL_STATES = {DONE, FAILED, CANCELLED}


@dataclass
class Task:
    id: str
    title: str
    goal: str
    status: str = QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    result: str = ""
    error: str = ""
    stats: dict = field(default_factory=dict)

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return round(end - (self.started_at or self.created_at), 1)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "goal": self.goal,
            "status": self.status,
            "elapsed": self.elapsed,
            "result": self.result,
            "error": self.error,
            "stats": self.stats,
        }

    def summary(self) -> str:
        line = f"[{self.id}] {self.title} — {self.status}, {self.elapsed} с"
        if self.error:
            line += f"\n  ошибка: {self.error}"
        elif self.result:
            line += f"\n  {self.result}"
        return line


class TaskManager:
    """Пул фоновых исполнителей поверх общей памяти."""

    def __init__(
        self,
        config: Config,
        memory: Memory,
        reminders: Reminders,
        *,
        client: anthropic.Anthropic,
        confirm: Callable[[str], bool],
        on_done: Callable[[Task], None] | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.reminders = reminders
        self.client = client
        self.confirm = confirm
        self.on_done = on_done or (lambda task: None)
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, config.max_parallel_tasks), thread_name_prefix="jarvis-task"
        )
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._futures: dict[str, Future] = {}
        self._cancelled: set[str] = set()

    # --- управление ---

    def spawn(self, goal: str, title: str = "") -> Task:
        goal = goal.strip()
        if not goal:
            raise ValueError("пустая задача")
        task = Task(
            id=uuid.uuid4().hex[:6],
            title=(title.strip() or goal)[:80],
            goal=goal,
        )
        with self._lock:
            self._tasks[task.id] = task
            self._futures[task.id] = self._pool.submit(self._run, task)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, active_only: bool = False) -> list[Task]:
        with self._lock:
            tasks = sorted(self._tasks.values(), key=lambda t: t.created_at)
        if active_only:
            tasks = [t for t in tasks if t.status not in FINAL_STATES]
        return tasks

    def cancel(self, task_id: str) -> bool:
        """Снимает задачу из очереди либо просит запущенную остановиться.

        Запущенный исполнитель останавливается не мгновенно: он проверяет флаг
        между вызовами инструментов, чтобы не бросать работу на полпути.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in FINAL_STATES:
                return False
            self._cancelled.add(task_id)
            future = self._futures.get(task_id)
            if task.status == QUEUED and future is not None and future.cancel():
                task.status = CANCELLED
                task.finished_at = time.time()
                return True
        return True

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            self._cancelled.update(self._tasks)
        self._pool.shutdown(wait=wait, cancel_futures=True)

    # --- исполнение ---

    def _should_stop(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._cancelled

    def _worker_config(self) -> Config:
        return dataclasses.replace(
            self.config,
            persona=WORKER_PERSONA,
            effort=self.config.task_effort,
            confirm_mode=self.config.task_confirm_mode,
            max_tool_iterations=self.config.task_max_iterations,
        )

    def _run(self, task: Task) -> None:
        from .agent import Agent  # локальный импорт: agent тянет tools, tools — этот модуль

        if self._should_stop(task.id):
            self._finish(task, CANCELLED)
            return

        task.status = RUNNING
        task.started_at = time.time()
        history_path = self.config.state / "tasks" / f"{task.id}.json"

        try:
            agent = Agent(
                self._worker_config(),
                client=self.client,
                confirm=self.confirm,
                say=lambda text: None,
                memory=self.memory,
                reminders=self.reminders,
                history_path=history_path,
                with_task_tools=False,
                should_stop=lambda: self._should_stop(task.id),
            )
            task.result = agent.ask(task.goal)
            task.stats = dict(agent.stats)
        except Exception as exc:
            task.error = f"{type(exc).__name__}: {exc}"
            self._finish(task, FAILED)
            return

        self._finish(task, CANCELLED if self._should_stop(task.id) else DONE)

    def _finish(self, task: Task, status: str) -> None:
        task.status = status
        task.finished_at = time.time()
        try:
            self.on_done(task)
        except Exception:  # оповещение не должно ронять исполнителя
            pass
