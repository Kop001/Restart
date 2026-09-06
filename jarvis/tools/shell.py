"""Выполнение команд оболочки."""

from __future__ import annotations

import subprocess

from anthropic import beta_tool

from .context import ToolContext, ToolError, guard


def register(ctx: ToolContext) -> list:
    @beta_tool
    @guard
    def run_shell(command: str, timeout: int = 0) -> str:
        """Выполняет команду в оболочке рабочей машины и возвращает вывод.

        Команды не из белого списка требуют подтверждения владельца.

        Args:
            command: Команда целиком, как её набрали бы в терминале.
            timeout: Таймаут в секундах; 0 — значение из конфигурации.
        """
        command = command.strip()
        if not command:
            raise ToolError("пустая команда")

        allowed = any(command.startswith(prefix) for prefix in ctx.config.shell_allowlist)
        if not allowed:
            ctx.ensure_allowed(f"выполнить команду: {command}")

        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(ctx.config.workspace_path),
                capture_output=True,
                text=True,
                timeout=timeout or ctx.config.shell_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"команда не уложилась в таймаут: {command}") from exc

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        parts = [f"код возврата: {proc.returncode}"]
        if out:
            parts.append(f"stdout:\n{out[:8000]}")
        if err:
            parts.append(f"stderr:\n{err[:2000]}")
        return "\n".join(parts)

    return [run_shell]
