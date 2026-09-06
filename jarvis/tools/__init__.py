"""Сборка набора инструментов Джарвиса."""

from __future__ import annotations

from . import files, knowledge, shell, system, tasks
from .context import ToolContext, ToolError

# Серверный инструмент поиска: выполняется на стороне Anthropic,
# реализовывать функцию не нужно.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}


def build_tools(ctx: ToolContext, with_tasks: bool = True) -> list:
    """Возвращает список инструментов в стабильном порядке.

    Порядок важен: он входит в префикс запроса, а кэш промптов —
    префиксный, так что перетасовка инструментов сбрасывает кэш.

    Args:
        ctx: Общий контекст инструментов.
        with_tasks: Давать ли право запускать фоновые задачи. У фоновых
            исполнителей его нет — иначе они наплодят новых исполнителей.
    """
    tools: list = []
    tools += system.register(ctx)
    tools += files.register(ctx)
    tools += shell.register(ctx)
    tools += knowledge.register(ctx)
    if with_tasks:
        tools += tasks.register(ctx)
    if ctx.config.web_search:
        tools.append(WEB_SEARCH_TOOL)
    return tools


__all__ = ["WEB_SEARCH_TOOL", "ToolContext", "ToolError", "build_tools"]
