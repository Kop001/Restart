"""Точка входа: jarvis [chat|voice|panel|ask|doctor]."""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__
from .config import Config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="Джарвис — личный ассистент на Claude API",
    )
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    parser.add_argument("--model", help="идентификатор модели Claude")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                        help="глубина размышлений модели")
    parser.add_argument("--workspace",
                        help="каталог, за пределы которого не выходят файловые инструменты")
    parser.add_argument("--confirm", dest="confirm_mode", choices=["ask", "auto", "deny"],
                        help="политика подтверждения опасных действий")
    parser.add_argument("--no-web-search", dest="web_search", action="store_false", default=None,
                        help="отключить веб-поиск")

    sub = parser.add_subparsers(dest="command")
    sub.add_parser("chat", help="диалог в терминале (по умолчанию)")

    voice = sub.add_parser("voice", help="голосовой режим со словом-активатором")
    voice.add_argument("--wake-word", help="слово-активатор")
    voice.add_argument("--no-wake-word", dest="wake_word_required", action="store_false",
                       default=None, help="реагировать на любую фразу")
    voice.add_argument("--stt-model", help="модель faster-whisper: tiny/base/small/medium/large-v3")

    panel = sub.add_parser("panel", help="веб-панель оператора")
    panel.add_argument("--host", default="127.0.0.1",
                       help="0.0.0.0 — открыть панель в локальную сеть, для телефона")
    panel.add_argument("--lan", action="store_true",
                       help="открыть панель всей локальной сети (по умолчанию выключено)")
    panel.add_argument("--port", type=int, default=8787)
    panel.add_argument("--no-open", dest="open_browser", action="store_false", default=True)
    panel.add_argument("--voice", action="store_true", default=None, help="озвучивать ответы вслух")

    ask = sub.add_parser("ask", help="один вопрос и выход")
    ask.add_argument("text", nargs="+")
    ask.add_argument("--voice", action="store_true", default=None, help="озвучить ответ")

    sub.add_parser("doctor", help="проверить окружение")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "chat"

    overrides = {
        key: getattr(args, key, None)
        for key in ("model", "effort", "workspace", "confirm_mode", "web_search",
                    "wake_word", "wake_word_required", "stt_model", "voice")
    }
    if command == "voice":
        overrides["voice"] = True
    config = Config.load(overrides)

    if command == "doctor":
        return doctor(config)

    if not _has_credentials():
        print(
            "Не найден ключ Claude API. Задайте ANTHROPIC_API_KEY "
            "или выполните `ant auth login`.",
            file=sys.stderr,
        )
        return 2

    if command == "panel":
        from .dashboard import serve

        host = "0.0.0.0" if getattr(args, "lan", False) else args.host
        serve(config, host=host, port=args.port, open_browser=args.open_browser)
        return 0

    from .session import Session

    session = Session(config)
    if command == "ask":
        session.respond(" ".join(args.text))
        return 0
    if command == "voice":
        session.run_voice()
        return 0
    session.run_text()
    return 0


def _has_credentials() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    from pathlib import Path

    base = os.environ.get("XDG_CONFIG_HOME", "~/.config")
    return (Path(base).expanduser() / "anthropic").is_dir()


def doctor(config: Config) -> int:
    """Печатает, что доступно, а что нет, и как это починить."""
    from .voice.stt import _detect as stt_detect
    from .voice.tts import detect_backend as tts_detect

    print(f"jarvis {__version__}\n")
    checks = [
        ("Ключ Claude API",
         "да" if _has_credentials() else "нет — экспортируйте ANTHROPIC_API_KEY"),
        ("Модель", config.model),
        ("Рабочий каталог", str(config.workspace_path)),
        ("Каталог состояния", str(config.state)),
        ("Подтверждения", config.confirm_mode),
        ("Синтез речи", tts_detect()),
        ("Распознавание речи", stt_detect()),
    ]
    try:
        import sounddevice  # noqa: F401

        checks.append(("Микрофон", "sounddevice доступен"))
    except ImportError:
        checks.append(("Микрофон", "нет — pip install 'jarvis[voice]'"))

    width = max(len(name) for name, _ in checks)
    for name, value in checks:
        print(f"  {name.ljust(width)}  {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
