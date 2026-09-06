"""Локальная панель оператора: HTTP-сервер на стандартной библиотеке.

Без внешних зависимостей: ThreadingHTTPServer отдаёт одностраничный UI и
небольшой JSON API поверх того же агента, что работает в терминале.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ..agent import Agent
from ..config import Config
from ..reminders import ReminderScheduler
from ..voice import Speaker
from .approvals import ApprovalQueue

STATIC = Path(__file__).parent / "static"


class Backend:
    """Общее состояние панели. Агент не потокобезопасен — сериализуем доступ."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.approvals = ApprovalQueue()
        self.speaker = Speaker(config.tts_backend, config.tts_voice) if config.voice else None
        self.events: list[dict] = []
        self.agent = Agent(config, confirm=self.approvals.request, say=self.say)
        self.scheduler = ReminderScheduler(self.agent.reminders, self._on_reminder)
        self._lock = threading.Lock()

    def say(self, text: str) -> None:
        if self.speaker is not None:
            self.speaker.say(text)

    def _on_reminder(self, item: dict) -> None:
        self.events.append({"type": "reminder", "text": item["text"], "at": item["due"]})
        self.events[:] = self.events[-50:]
        self.say(f"Напоминание: {item['text']}")

    def chat(self, message: str) -> dict:
        with self._lock:
            answer = self.agent.ask(message)
        self.say(answer)
        return {"answer": answer, "stats": self.agent.stats}

    def state(self) -> dict:
        agent = self.agent
        return {
            "config": {
                "model": self.config.model,
                "effort": self.config.effort,
                "voice": self.config.voice,
                "confirm_mode": self.config.confirm_mode,
                "workspace": str(self.config.workspace_path),
                "wake_word": self.config.wake_word,
            },
            "skills": [
                {"name": t["name"], "kind": "server"} if isinstance(t, dict)
                else {"name": t.to_dict()["name"], "description": t.to_dict()["description"], "kind": "local"}
                for t in agent.tools
            ],
            "memory": agent.memory.facts,
            "reminders": agent.reminders.pending(),
            "stats": agent.stats,
            "history_len": len(agent.history.messages),
            "events": self.events[-20:],
            "connections": self.connections(),
        }

    def connections(self) -> list[dict]:
        """Что из внешнего мира реально доступно этой сборке."""
        from ..voice.stt import _detect as stt_detect
        from ..voice.tts import detect_backend as tts_detect

        import os

        tts = tts_detect()
        stt = stt_detect()
        return [
            {"name": "Claude API", "status": "ok" if os.environ.get("ANTHROPIC_API_KEY") else "нет ключа"},
            {"name": "Веб-поиск", "status": "ok" if self.config.web_search else "выключен"},
            {"name": "Синтез речи", "status": tts if tts != "none" else "не найден"},
            {"name": "Распознавание речи", "status": stt if stt != "none" else "не найден"},
            {"name": "Оболочка", "status": self.config.confirm_mode},
        ]


def make_handler(backend: Backend):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Jarvis/0.1"

        def log_message(self, fmt, *args):  # тише в консоли
            pass

        # --- ответы ---

        def _json(self, payload, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, content_type: str) -> None:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        # --- маршруты ---

        def do_GET(self) -> None:
            route = urlparse(self.path).path
            if route in ("/", "/index.html"):
                self._file(STATIC / "index.html", "text/html; charset=utf-8")
            elif route == "/api/state":
                self._json(backend.state())
            elif route == "/api/pending":
                self._json({"pending": backend.approvals.pending()})
            else:
                self._json({"error": "не найдено"}, 404)

        def do_POST(self) -> None:
            route = urlparse(self.path).path
            data = self._body()
            if route == "/api/chat":
                message = (data.get("message") or "").strip()
                if not message:
                    return self._json({"error": "пустое сообщение"}, 400)
                try:
                    return self._json(backend.chat(message))
                except Exception as exc:
                    return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            if route == "/api/approve":
                ok = backend.approvals.resolve(data.get("id", ""), bool(data.get("approved")))
                return self._json({"ok": ok})
            if route == "/api/memory":
                fact = backend.agent.memory.add(data.get("text", ""), data.get("tag", "general"))
                return self._json(fact)
            if route == "/api/memory/forget":
                return self._json({"ok": backend.agent.memory.forget(data.get("id", ""))})
            if route == "/api/reminders":
                try:
                    return self._json(backend.agent.reminders.add(data.get("text", ""), data.get("when", "")))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
            if route == "/api/reminders/cancel":
                return self._json({"ok": backend.agent.reminders.cancel(data.get("id", ""))})
            if route == "/api/reset":
                backend.agent.reset()
                return self._json({"ok": True})
            self._json({"error": "не найдено"}, 404)

    return Handler


def serve(config: Config, host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True) -> None:
    backend = Backend(config)
    backend.scheduler.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(backend))
    url = f"http://{host}:{port}"
    print(f"Панель Джарвиса: {url}  (Ctrl+C — остановить)")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        backend.scheduler.stop()
        httpd.server_close()
