"""Память и напоминания как инструменты модели."""

from __future__ import annotations

from anthropic import beta_tool

from .context import ToolContext, ToolError, guard


def register(ctx: ToolContext) -> list:
    @beta_tool
    @guard
    def remember_fact(
        text: str,
        tag: str = "general",
        replaces: str = "",
        importance: str = "normal",
    ) -> str:
        """Сохраняет факт о владельце в долговременную память.

        Используй для устойчивых вещей: имена, предпочтения, рабочие пути,
        расписание. Не для содержимого текущего разговора.

        Если новое сведение отменяет то, что ты уже помнишь — обязательно
        укажи `replaces`. Иначе в памяти окажутся два взаимоисключающих
        факта, и позже будет непонятно, какому верить. Идентификатор
        старого факта возьми из своей памяти или из recall_facts.

        Args:
            text: Формулировка факта одной фразой.
            tag: Категория: personal, work, tech, schedule и т.п.
            replaces: Идентификатор факта, который это сведение отменяет.
            importance: low, normal или high. high — то, что важно не забыть
                и не перепутать: здоровье, безопасность, деньги.
        """
        known = ctx.memory.find_twin(text)
        if known is not None:
            return f"уже помню это: [{known['id']}] {known['text']}"

        similar = [] if replaces else ctx.memory.similar(text, tag)
        fact = ctx.memory.add(
            text, tag, replaces=replaces, importance=importance, source=ctx.source
        )

        if replaces:
            return f"запомнил [{fact['id']}]: {fact['text']}, заменил {replaces}"
        if similar:
            listed = "; ".join(f"[{f['id']}] {f['text']}" for f in similar)
            return (
                f"запомнил [{fact['id']}]: {fact['text']}.\n"
                f"Похоже на то, что уже знаю: {listed}. "
                f"Если новое сведение отменяет старое — запиши его ещё раз, "
                f"указав replaces с нужным идентификатором, иначе в памяти "
                f"останутся два противоречащих факта."
            )
        return f"запомнил [{fact['id']}]: {fact['text']}"

    @beta_tool
    @guard
    def recall_facts(query: str = "", tag: str = "", include_stale: bool = False) -> str:
        """Ищет факты в долговременной памяти.

        Args:
            query: Подстрока для поиска; пусто — вернуть всё.
            tag: Ограничить поиск категорией.
            include_stale: Показать и заменённые факты — например, чтобы
                понять, что владелец говорил раньше.
        """
        found = ctx.memory.search(query, tag, include_stale=include_stale, count=True)
        if not found:
            return "ничего не найдено"
        return "\n".join(_line(f) for f in found)

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

    @beta_tool
    @guard
    def what_happened(hours: float = 24.0) -> str:
        """Рассказывает, что произошло за последнее время.

        Бери, когда владелец спрашивает «что нового», «что я пропустил»,
        «что было, пока меня не было».

        Args:
            hours: За сколько последних часов смотреть.
        """
        if ctx.log is None:
            raise ToolError("хроника не ведётся в этом режиме")
        from ..digest import since

        return since(ctx.log, hours) or "за это время ничего не происходило"

    return [remember_fact, recall_facts, forget_fact, what_happened,
            add_reminder, list_reminders, cancel_reminder]


def _line(fact: dict) -> str:
    """Строка факта для модели: важное и заменённое видно сразу."""
    marks = ""
    if fact.get("importance") == "high":
        marks += " !важно"
    if fact.get("superseded_by"):
        marks += f" (заменён на {fact['superseded_by']})"
    return f"[{fact['id']}] ({fact['tag']}) {fact['text']}{marks}"
