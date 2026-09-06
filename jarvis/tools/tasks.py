"""Инструменты управления фоновыми задачами.

Есть только у основного диалога: фоновый исполнитель их не получает, иначе
одна просьба развернулась бы в лавину исполнителей.
"""

from __future__ import annotations

from anthropic import beta_tool

from .context import ToolContext, ToolError


def register(ctx: ToolContext) -> list:
    def manager():
        if ctx.tasks is None:
            raise ToolError("менеджер фоновых задач не запущен в этом режиме")
        return ctx.tasks

    @beta_tool
    def spawn_task(goal: str, title: str = "") -> str:
        """Запускает задачу в фоне и сразу возвращает управление.

        Бери этот инструмент, когда работа долгая (собрать данные, разобраться
        в коде, прогнать сборку) или когда владелец поручил несколько дел
        сразу — тогда запусти их по одному вызову на дело, а потом ответь,
        что взялся. Результат придёт позже, спрашивать его через task_result.

        Не бери для короткого действия, ответ на которое нужен прямо сейчас:
        его быстрее выполнить обычным инструментом.

        Args:
            goal: Задача целиком, своими словами, со всем нужным контекстом —
                исполнитель не видит вашего разговора.
            title: Короткое название для списка задач.
        """
        try:
            task = manager().spawn(goal, title)
        except ValueError as exc:
            raise ToolError(str(exc))
        return f"задача [{task.id}] «{task.title}» запущена в фоне"

    @beta_tool
    def list_tasks(active_only: bool = False) -> str:
        """Показывает фоновые задачи и их состояние.

        Args:
            active_only: Только незавершённые.
        """
        tasks = manager().list(active_only=active_only)
        if not tasks:
            return "фоновых задач нет"
        return "\n".join(task.summary() for task in tasks)

    @beta_tool
    def task_result(task_id: str) -> str:
        """Возвращает отчёт фоновой задачи.

        Args:
            task_id: Идентификатор из spawn_task или list_tasks.
        """
        task = manager().get(task_id)
        if task is None:
            raise ToolError(f"задачи {task_id} нет")
        if task.status in {"queued", "running"}:
            return f"задача [{task.id}] ещё выполняется ({task.elapsed} с)"
        if task.error:
            return f"задача [{task.id}] завершилась ошибкой: {task.error}"
        return task.result or f"задача [{task.id}]: {task.status}, отчёта нет"

    @beta_tool
    def cancel_task(task_id: str) -> str:
        """Останавливает фоновую задачу.

        Запущенный исполнитель остановится на ближайшей границе хода,
        не бросая начатый вызов инструмента на полпути.

        Args:
            task_id: Идентификатор задачи.
        """
        if not manager().cancel(task_id):
            raise ToolError(f"активной задачи {task_id} нет")
        return f"остановка задачи {task_id} запрошена"

    return [spawn_task, list_tasks, task_result, cancel_task]
