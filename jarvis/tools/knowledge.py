"""Память и напоминания как инструменты модели."""

from __future__ import annotations

from anthropic import beta_tool

from .context import ToolContext, ToolError, guard


def register(ctx: ToolContext) -> list:
    @beta_tool
    @guard
    def remember_fact(text: str, tag: str = "general") -> str:
        """Сохраняет факт о владельце в долговременную память.

        Используй для устойчивых вещей: имена, предпочтения, рабочие пути,
        расписание. Не для содержимого текущего разговора.

        Args:
            text: Формулировка факта одной фразой.
            tag: Категория: personal, work, tech, schedule и т.п.
        """
        fact = ctx.memory.add(text, tag)
        return f"запомнил [{fact['id']}]: {fact['text']}"

    @beta_tool
    @guard
    def recall_facts(query: str = "", tag: str = "") -> str:
        """Ищет факты в долговременной памяти.

        Args:
            query: Подстрока для поиска; пусто — вернуть всё.
            tag: Ограничить поиск категорией.
        """
        found = ctx.memory.search(query, tag)
        if not found:
            return "ничего не найдено"
        return "\n".join(f"[{f['id']}] ({f['tag']}) {f['text']}" for f in found)

    @beta_tool
    @guard
    def forget_fact(fact_id: str) -> str:
        """Удаляет факт из памяти по идентификатору.

        Args:
            fact_id: Идентификатор факта, как его вернул recall_facts.
        """
        if not ctx.memory.forget(fact_id):
            raise ToolError(f"факта {fact_id} нет в памяти")
        return f"забыл {fact_id}"

    @beta_tool
    @guard
    def add_reminder(text: str, when: str) -> str:
        """Ставит напоминание; в нужный момент Джарвис произнесёт его вслух.

        Args:
            text: О чём напомнить.
            when: Когда: «через 15 минут», «в 18:30» или «2026-09-07 09:00».
        """
        try:
            item = ctx.reminders.add(text, when)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return f"напоминание [{item['id']}] на {item['due']}: {item['text']}"

    @beta_tool
    @guard
    def list_reminders() -> str:
        """Показывает активные напоминания."""
        items = ctx.reminders.pending()
        if not items:
            return "активных напоминаний нет"
        return "\n".join(f"[{i['id']}] {i['due']} — {i['text']}" for i in items)

    @beta_tool
    @guard
    def cancel_reminder(reminder_id: str) -> str:
        """Отменяет напоминание по идентификатору.

        Args:
            reminder_id: Идентификатор из list_reminders.
        """
        if not ctx.reminders.cancel(reminder_id):
            raise ToolError(f"активного напоминания {reminder_id} нет")
        return f"отменил {reminder_id}"

    return [remember_fact, recall_facts, forget_fact, add_reminder, list_reminders, cancel_reminder]
