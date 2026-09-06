"""Файловые инструменты."""

from __future__ import annotations

from anthropic import beta_tool

from .context import ToolContext, ToolError, guard


def register(ctx: ToolContext) -> list:
    @beta_tool
    @guard
    def list_dir(path: str = ".") -> str:
        """Показывает содержимое каталога в рабочей директории.

        Args:
            path: Путь к каталогу, абсолютный или относительно рабочей директории.
        """
        target = ctx.resolve_path(path)
        if not target.is_dir():
            raise ToolError(f"{target} — не каталог")
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        if not entries:
            return f"{target}: пусто"
        lines = [
            f"{'d' if e.is_dir() else '-'} {e.name}" + ("" if e.is_dir() else f"  {e.stat().st_size} b")
            for e in entries[:200]
        ]
        return f"{target}:\n" + "\n".join(lines)

    @beta_tool
    @guard
    def read_file(path: str, max_bytes: int = 20000) -> str:
        """Читает текстовый файл.

        Args:
            path: Путь к файлу.
            max_bytes: Ограничение на размер прочитанного куска.
        """
        target = ctx.resolve_path(path)
        if not target.is_file():
            raise ToolError(f"файла нет: {target}")
        data = target.read_bytes()[:max_bytes]
        text = data.decode("utf-8", errors="replace")
        suffix = "\n[...обрезано...]" if target.stat().st_size > max_bytes else ""
        return text + suffix

    @beta_tool
    @guard
    def write_file(path: str, content: str, append: bool = False) -> str:
        """Записывает текст в файл. Требует подтверждения владельца.

        Args:
            path: Путь к файлу.
            content: Текст для записи.
            append: Дописать в конец вместо перезаписи.
        """
        target = ctx.resolve_path(path)
        verb = "дописать в" if append else "перезаписать"
        ctx.ensure_allowed(f"{verb} файл {target} ({len(content)} символов)")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a" if append else "w", encoding="utf-8") as fh:
            fh.write(content)
        return f"записано в {target} ({len(content)} символов)"

    return [list_dir, read_file, write_file]
