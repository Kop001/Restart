"""Разрешения: владелец отвечает на каждый запрос, откуда бы тот ни пришёл."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
import pytest

from jarvis.approvals import ApprovalQueue
from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.reminders import Reminders
from jarvis.session import Session, parse_answer
from jarvis.tasks import TaskManager


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- очередь ---

def test_queue_delivers_decision_to_waiting_thread():
    queue = ApprovalQueue(timeout=5)
    result: list[bool] = []
    threading.Thread(target=lambda: result.append(queue.request("удалить файл", "диалог")),
                     daemon=True).start()

    assert wait_for(lambda: queue.pending())
    pending = queue.pending()[0]
    assert pending["action"] == "удалить файл"
    assert pending["source"] == "диалог"

    assert queue.resolve_first(True)
    assert wait_for(lambda: result == [True])
    assert queue.pending() == []


def test_queue_answers_oldest_first():
    queue = ApprovalQueue(timeout=5)
    results: dict[str, bool] = {}
    for name in ("первый", "второй"):
        threading.Thread(
            target=lambda n=name: results.__setitem__(n, queue.request(n)), daemon=True
        ).start()
        assert wait_for(lambda n=name: any(p["action"] == n for p in queue.pending()))

    assert [p["action"] for p in queue.pending()] == ["первый", "второй"]
    queue.resolve_first(False)
    assert wait_for(lambda: results.get("первый") is False)
    assert [p["action"] for p in queue.pending()] == ["второй"]


def test_queue_denies_on_timeout():
    queue = ApprovalQueue(timeout=0.1)
    assert queue.request("что-нибудь") is False
    assert queue.pending() == []


def test_resolve_first_on_empty_queue():
    assert ApprovalQueue().resolve_first(True) is False


@pytest.mark.parametrize(
    "phrase,expected",
    [("да", True), ("Да, разрешаю", True), ("y", True), ("ок", True),
     ("нет", False), ("Нет, отмена", False), ("n", False),
     ("а что это вообще", None), ("", None)],
)
def test_parse_answer(phrase, expected):
    assert parse_answer(phrase) == expected


# --- консоль ---

@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")
    session = Session(Config.load({"workspace": str(tmp_path)}))
    yield session
    session.tasks.shutdown()


def test_console_line_answers_pending_request(session, capsys):
    """Пока висит запрос, реплика владельца — это ответ на него, а не задача."""
    granted: list[bool] = []
    threading.Thread(
        target=lambda: granted.append(session.approvals.request("стереть диск", "фоновая задача")),
        daemon=True,
    ).start()
    assert wait_for(lambda: session.approvals.pending())

    prompt = session.pending_prompt()
    assert "Фоновая задача просит разрешение: стереть диск" == prompt
    # Повторно тот же запрос не переспрашивается.
    assert session.pending_prompt() is None

    assert session.dispatch("да") is False
    assert wait_for(lambda: granted == [True])
    assert not session.busy, "ответ на запрос не должен уходить в диалог как реплика"


def test_console_rejects_and_keeps_asking_on_unclear_answer(session):
    denied: list[bool] = []
    threading.Thread(
        target=lambda: denied.append(session.approvals.request("выполнить rm -rf /")),
        daemon=True,
    ).start()
    assert wait_for(lambda: session.approvals.pending())

    session.dispatch("что?")
    assert session.approvals.pending(), "неразборчивый ответ не снимает запрос"

    session.dispatch("нет")
    assert wait_for(lambda: denied == [False])


# --- фоновый исполнитель ---

@pytest.fixture
def writing_api():
    """Заглушка: исполнитель пробует записать файл, затем отчитывается."""
    turns = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            turns["n"] += 1
            if turns["n"] == 1:
                content = [{"type": "tool_use", "id": "tu_1", "name": "write_file",
                            "input": {"path": "отчёт.txt", "content": "готово"}}]
                stop = "tool_use"
            else:
                content = [{"type": "text", "text": "файл записан"}]
                stop = "end_turn"
            body = json.dumps({
                "id": "m", "type": "message", "role": "assistant", "model": "claude-opus-5",
                "content": content, "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_background_task_asks_owner_and_proceeds(writing_api, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    config = Config.load({"workspace": str(tmp_path)})
    assert config.task_confirm_mode == "ask", "фоновая задача должна спрашивать, а не отказывать"

    queue = ApprovalQueue(timeout=5)
    client = anthropic.Anthropic(api_key="sk-ant-stub", base_url=writing_api, max_retries=0)
    manager = TaskManager(
        config, Memory(config.memory_file), Reminders(config.reminders_file),
        client=client,
        confirm=lambda action: queue.request(action, "фоновая задача"),
    )
    try:
        task = manager.spawn("записать отчёт", "отчёт")

        # Запрос от фонового исполнителя доходит до владельца.
        assert wait_for(lambda: queue.pending()), "исполнитель не спросил разрешения"
        assert queue.pending()[0]["source"] == "фоновая задача"
        assert "отчёт.txt" in queue.pending()[0]["action"]

        queue.resolve_first(True)
        assert wait_for(lambda: task.status == "done")
        assert (tmp_path / "отчёт.txt").read_text(encoding="utf-8") == "готово"
    finally:
        manager.shutdown()
