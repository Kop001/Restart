"""Ядро Джарвиса: цикл «запрос — инструменты — ответ» поверх Claude API."""

from __future__ import annotations

import platform
from datetime import datetime
from pathlib import Path

import anthropic

from .config import Config
from .memory import History, Memory
from .reminders import Reminders
from .tools import ToolContext, build_tools

# Серверный фолбэк на случай отказа модели по соображениям безопасности:
# запрос автоматически уходит на подходящую запасную модель.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_PAUSE_RESTARTS = 5


class Agent:
    """Один разговор с Джарвисом, переживающий перезапуски процесса."""

    def __init__(
        self,
        config: Config,
        *,
        client: anthropic.Anthropic | None = None,
        confirm=None,
        say=None,
        memory: Memory | None = None,
        reminders: Reminders | None = None,
        history_path: Path | None = None,
        with_task_tools: bool = True,
        should_stop=None,
    ) -> None:
        self.config = config
        self.client = client or anthropic.Anthropic()
        # Память и напоминания общие: фоновые исполнители получают их снаружи,
        # чтобы всё, что узнал один, знали остальные.
        self.memory = memory or Memory(config.memory_file)
        self.reminders = reminders or Reminders(config.reminders_file)
        self.history = History(history_path or config.history_file, config.history_turns)
        self.should_stop = should_stop or (lambda: False)
        self.ctx = ToolContext(
            config=config,
            memory=self.memory,
            reminders=self.reminders,
            confirm=confirm or (lambda action: False),
            say=say or (lambda text: print(text)),
        )
        self.tools = build_tools(self.ctx, with_tasks=with_task_tools)
        self._extra_params_supported = True
        # Счётчики для панели «Аналитика».
        self.stats: dict[str, int] = {
            "requests": 0,
            "turns": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "tool_calls": 0,
        }

    # --- системный промпт ---

    def system_prompt(self) -> list[dict]:
        """Системный промпт тремя блоками: от самого стабильного к самому изменчивому.

        Кэш промптов работает по префиксу, поэтому персона (она не меняется)
        идёт первой и помечается cache_control, а время — последним.
        """
        blocks: list[dict] = [
            {
                "type": "text",
                "text": self.config.persona,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        facts = self.memory.as_prompt()
        if facts:
            blocks.append({"type": "text", "text": facts})
        blocks.append({"type": "text", "text": self._runtime_block()})
        return blocks

    def _runtime_block(self) -> str:
        pending = self.reminders.pending()
        lines = [
            "Текущая обстановка:",
            f"- время: {datetime.now().strftime('%Y-%m-%d %H:%M, %A')}",
            f"- машина: {platform.system()} {platform.release()}",
            f"- рабочий каталог: {self.config.workspace_path}",
            f"- голосовой режим: {'включён' if self.config.voice else 'выключен'}",
            f"- подтверждение опасных действий: {self.config.confirm_mode}",
        ]
        if pending:
            due = "; ".join(f"{i['due']} {i['text']}" for i in pending)
            lines.append(f"- активные напоминания: {due}")
        return "\n".join(lines)

    # --- основной цикл ---

    def ask(self, user_input: str) -> str:
        """Прогоняет реплику владельца через модель и возвращает текст ответа."""
        messages = [*self.history.messages, {"role": "user", "content": user_input}]
        new_messages: list[dict] = [{"role": "user", "content": user_input}]

        restarts = 0
        last = None
        stopped = False
        while True:
            runner = self._runner(messages)
            for message in runner:
                last = message
                self._track(message)
                if self.should_stop():
                    # Останавливаемся на границе хода: начатый вызов
                    # инструмента уже завершён, история цела.
                    stopped = True
                    break
                # Ранер держит историю у себя и не отдаёт её наружу,
                # поэтому ведём собственную копию — для диска и для pause_turn.
                turn = {"role": "assistant", "content": message.content}
                messages.append(turn)
                new_messages.append(turn)
                tool_response = runner.generate_tool_call_response()
                if tool_response is not None:
                    messages.append(tool_response)
                    new_messages.append(tool_response)

            if stopped or last is None or last.stop_reason != "pause_turn":
                break
            restarts += 1
            if restarts > MAX_PAUSE_RESTARTS:
                break

        self.stats["turns"] += 1
        self.history.extend(new_messages)

        if stopped:
            return "задача остановлена по требованию"
        if last is None:
            return "не получил ответа от модели"
        if last.stop_reason == "refusal":
            reason = getattr(last.stop_details, "category", None)
            return f"я не могу ответить на это{f' ({reason})' if reason else ''}"
        return extract_text(last) or "готово"

    def _runner(self, messages: list[dict]):
        params: dict = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "system": self.system_prompt(),
            "messages": messages,
            "tools": self.tools,
            "max_iterations": self.config.max_tool_iterations,
            "output_config": {"effort": self.config.effort},
        }
        if self.config.thinking:
            params["thinking"] = {"type": "adaptive"}

        if self._extra_params_supported and self.config.refusal_fallback:
            try:
                return self.client.beta.messages.tool_runner(
                    **params, betas=[FALLBACK_BETA], fallbacks="default"
                )
            except (TypeError, anthropic.BadRequestError):
                # Старый SDK или модель без серверных фолбэков —
                # дальше работаем без них и больше не пробуем.
                self._extra_params_supported = False

        return self.client.beta.messages.tool_runner(**params)

    def _track(self, message) -> None:
        """Копит расход токенов и число вызовов инструментов."""
        usage = getattr(message, "usage", None)
        self.stats["requests"] += 1
        if usage is not None:
            self.stats["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
            self.stats["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
            self.stats["cache_read_tokens"] += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.stats["tool_calls"] += sum(
            1 for block in message.content if getattr(block, "type", None) == "tool_use"
        )

    # --- обслуживание ---

    def attach_tasks(self, manager) -> None:
        """Отдаёт агенту менеджер фоновых задач.

        Инструменты читают ctx.tasks в момент вызова, поэтому пересобирать
        их список (и ломать префиксный кэш) не нужно.
        """
        self.ctx.tasks = manager

    def reset(self) -> None:
        """Забывает текущий разговор, но не долговременную память."""
        self.history.clear()


def extract_text(message) -> str:
    """Склеивает текстовые блоки ответа, пропуская thinking и вызовы инструментов."""
    parts = [block.text for block in message.content if getattr(block, "type", None) == "text"]
    return "\n".join(part.strip() for part in parts if part.strip()).strip()
