"""Инструменты работы с системой владельца: время, открытие ссылок, уведомления."""

from __future__ import annotations

import platform
import shutil
import subprocess
import time
from datetime import datetime

from anthropic import beta_tool

from .context import ToolContext, ToolError


def register(ctx: ToolContext) -> list:
    @beta_tool
    def current_time() -> str:
        """Возвращает текущие дату, время и часовой пояс машины."""
        now = datetime.now()
        return now.strftime("%Y-%m-%d %H:%M:%S %A") + f" ({time.tzname[0]})"

    @beta_tool
    def system_info() -> str:
        """Возвращает сведения о машине: ОС, архитектура, имя хоста, рабочий каталог."""
        return (
            f"ОС: {platform.system()} {platform.release()}\n"
            f"архитектура: {platform.machine()}\n"
            f"хост: {platform.node()}\n"
            f"рабочий каталог: {ctx.config.workspace_path}"
        )

    @beta_tool
    def open_url(url: str) -> str:
        """Открывает ссылку или файл в приложении по умолчанию. Требует подтверждения.

        Args:
            url: URL или путь к файлу.
        """
        ctx.ensure_allowed(f"открыть {url}")
        opener = _opener()
        if opener is None:
            raise ToolError("на этой машине нет open/xdg-open")
        subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"открыл {url}"

    @beta_tool
    def notify(title: str, message: str) -> str:
        """Показывает системное уведомление на рабочем столе.

        Args:
            title: Заголовок уведомления.
            message: Текст уведомления.
        """
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], check=False)
        elif platform.system() == "Darwin" and shutil.which("osascript"):
            script = f'display notification {message!r} with title {title!r}'
            subprocess.run(["osascript", "-e", script], check=False)
        else:
            ctx.say(f"{title}. {message}")
            return "уведомлений нет, сказал вслух"
        return "уведомление показано"

    return [current_time, system_info, open_url, notify]


def _opener() -> str | None:
    for name in ("xdg-open", "open", "wslview"):
        if shutil.which(name):
            return name
    return None
