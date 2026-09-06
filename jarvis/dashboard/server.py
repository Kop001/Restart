"""Локальная панель оператора: HTTP-сервер на стандартной библиотеке.

Без внешних зависимостей: ThreadingHTTPServer отдаёт одностраничный UI и
небольшой JSON API поверх того же агента, что работает в терминале.
"""

from __future__ import annotations

import json
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ..agent import Agent
from ..approvals import ApprovalQueue
from ..config import Config
from ..reminders import ReminderScheduler
from ..tasks import TaskManager
from ..voice import Speaker
from .auth import COOKIE, load_or_create_token, matches, token_from_request

STATIC = Path(__file__).parent / "static"


class Backend:
    """Общее состояние панели. Агент не потокобезопасен — сериализуем доступ."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.token = load_or_create_token(config.state / "panel-token")
        self.approvals = ApprovalQueue(timeout=config.approval_timeout)
        self.speaker = Speaker(config.tts_backend, config.tts_voice) if config.voice else None
        self.events: list[dict] = []
        self.agent = Agent(
            config,
            confirm=lambda action: self.approvals.request(action, "диалог"),
            say=self.say,
        )
        self.scheduler = ReminderScheduler(self.agent.reminders, self._on_reminder)
        self.tasks = TaskManager(
            config,
            self.agent.memory,
            self.agent.reminders,
            client=self.agent.client,
            # Фоновая задача спрашивает так же, как диалог: окно
            # подтверждения работает из любого потока.
            confirm=lambda action: self.approvals.request(action, "фоновая задача"),
            on_done=self._on_task_done,
        )
        self.agent.attach_tasks(self.tasks)
        self._lock = threading.Lock()

    def say(self, text: str) -> None:
        if self.speaker is not None:
            self.speaker.say(text)

    def _log(self, kind: str, text: str, at: str) -> None:
        self.events.append({"type": kind, "text": text, "at": at})
        self.events[:] = self.events[-50:]

    def _on_reminder(self, item: dict) -> None:
        self._log("reminder", item["text"], item["due"])
        self.say(f"Напоминание: {item['text']}")

    def _on_task_done(self, task) -> None:
        if task.status == "cancelled":
            self._log("task", f"задача «{task.title}» остановлена", f"{task.elapsed} с")
            return
        if task.error:
            self._log("task", f"задача «{task.title}» сорвалась: {task.error}", f"{task.elapsed} с")
            self.say(f"Задача «{task.title}» сорвалась.")
        else:
            self._log("task", f"задача «{task.title}» готова", f"{task.elapsed} с")
            self.say(f"Задача «{task.title}» готова. {task.result}")

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
            "skills": [_skill(tool) for tool in agent.tools],
            "memory": agent.memory.facts,
            "reminders": agent.reminders.pending(),
            "tasks": [task.as_dict() for task in self.tasks.list()],
            "max_parallel_tasks": self.config.max_parallel_tasks,
            "stats": agent.stats,
            "history_len": len(agent.history.messages),
            "events": self.events[-20:],
            "connections": self.connections(),
            "privacy": self.privacy(),
        }

    def privacy(self) -> dict:
        """Что Джарвис хранит о владельце и где."""
        from ..privacy import survey

        return {
            "counts": survey(self.config),
            "state_dir": str(self.config.state),
            "private_mode": self.config.private_mode,
        }

    def connections(self) -> list[dict]:
        """Что из внешнего мира реально доступно этой сборке."""
        import os

        from ..voice.stt import _detect as stt_detect
        from ..voice.tts import detect_backend as tts_detect

        tts = tts_detect()
        stt = stt_detect()
        return [
            {"name": "Claude API",
             "status": "ok" if os.environ.get("ANTHROPIC_API_KEY") else "нет ключа"},
            {"name": "Веб-поиск", "status": "ok" if self.config.web_search else "выключен"},
            {"name": "Синтез речи", "status": tts if tts != "none" else "не найден"},
            {"name": "Распознавание речи", "status": stt if stt != "none" else "не найден"},
            {"name": "Оболочка", "status": self.config.confirm_mode},
            {"name": "Фоновые исполнители",
             "status": f"до {self.config.max_parallel_tasks} параллельно"},
        ]


def _skill(tool) -> dict:
    """Описывает инструмент для панели: серверный он или локальный."""
    if isinstance(tool, dict):
        return {"name": tool["name"], "kind": "server"}
    schema = tool.to_dict()
    return {"name": schema["name"], "description": schema["description"], "kind": "local"}


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

        def _file(self, path: Path, content_type: str, set_cookie: bool = False) -> None:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if set_cookie:
                # Токен пришёл ссылкой — закрепляем, чтобы он не остался
                # в адресной строке и в истории браузера.
                self.send_header(
                    "Set-Cookie",
                    f"{COOKIE}={backend.token}; Path=/; Max-Age=31536000; SameSite=Lax",
                )
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

        # --- доступ ---

        def _authorized(self) -> bool:
            url = urlparse(self.path)
            given = token_from_request(url.query, self.headers.get("Cookie"))
            return matches(backend.token, given)

        def _deny(self) -> None:
            self._json({"error": "нужен токен доступа"}, 401)

        # --- маршруты ---

        def do_GET(self) -> None:
            if not self._authorized():
                return self._deny()
            route = urlparse(self.path).path
            if route in ("/", "/index.html"):
                self._file(
                    STATIC / "index.html",
                    "text/html; charset=utf-8",
                    set_cookie=urlparse(self.path).query != "",
                )
            elif route == "/api/state":
                self._json(backend.state())
            elif route == "/api/pending":
                self._json({"pending": backend.approvals.pending()})
            else:
                self._json({"error": "не найдено"}, 404)

        def do_POST(self) -> None:
            if not self._authorized():
                return self._deny()
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
                    item = backend.agent.reminders.add(
                        data.get("text", ""), data.get("when", "")
                    )
                    return self._json(item)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
            if route == "/api/reminders/cancel":
                return self._json({"ok": backend.agent.reminders.cancel(data.get("id", ""))})
            if route == "/api/tasks":
                try:
                    task = backend.tasks.spawn(data.get("goal", ""), data.get("title", ""))
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 400)
                return self._json(task.as_dict())
            if route == "/api/tasks/cancel":
                return self._json({"ok": backend.tasks.cancel(data.get("id", ""))})
            if route == "/api/wipe":
                from ..privacy import KINDS, describe, wipe

                kinds = tuple(k for k in data.get("kinds", KINDS) if k in KINDS) or KINDS
                removed = wipe(backend.config, kinds)
                backend.agent.reset()
                if "memory" in kinds:
                    backend.agent.memory.facts.clear()
                return self._json({"report": describe(removed)})
            if route == "/api/reset":
                backend.agent.reset()
                return self._json({"ok": True})
            self._json({"error": "не найдено"}, 404)

    return Handler


def local_ip() -> str:
    """Адрес машины в локальной сети — по нему панель открывают с телефона.

    Не резолвим имя хоста: оно часто указывает на 127.0.0.1. Вместо этого
    смотрим, с какого адреса ушёл бы пакет наружу; сокет UDP никуда не шлёт.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def serve(
    config: Config,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
) -> None:
    backend = Backend(config)
    backend.scheduler.start()
    httpd = ThreadingHTTPServer((host, port), make_handler(backend))

    shown = local_ip() if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{port}/?token={backend.token}"
    print(f"Панель Джарвиса: {url}")
    if host in ("0.0.0.0", "::"):
        print("ВНИМАНИЕ: панель открыта всей локальной сети. Кто угодно в этом")
        print("вайфае увидит её; защищает только токен из ссылки.")
        print(f"Токен лежит в {config.state / 'panel-token'} и не меняется при перезапуске.")
    else:
        print("Панель слушает только эту машину. Снаружи к ней не подключиться.")
    print("Ctrl+C — остановить")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        backend.scheduler.stop()
        backend.tasks.shutdown()
        httpd.server_close()
