"""Фоновые исполнители: параллельность, изоляция, остановка."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
import pytest

from jarvis.config import Config
from jarvis.memory import Memory
from jarvis.reminders import Reminders
from jarvis.tasks import TaskManager
from jarvis.tools import build_tools
from jarvis.tools.context import ToolContext

REPLY_DELAY = 0.4


@pytest.fixture
def slow_api():
    """Заглушка Messages API, которая отвечает не мгновенно.

    Задержка нужна, чтобы отличить параллельную работу от последовательной.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length))
            goal = request["messages"][-1]["content"]
            time.sleep(REPLY_DELAY)
            body = json.dumps({
                "id": "msg_stub", "type": "message", "role": "assistant",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": f"сделано: {goal}"}],
                "stop_reason": "end_turn", "stop_sequence": None,
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


@pytest.fixture
def manager(slow_api, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    config = Config.load({"workspace": str(tmp_path), "confirm_mode": "auto"})
    config.max_parallel_tasks = 3
    client = anthropic.Anthropic(api_key="sk-ant-stub", base_url=slow_api, max_retries=0)
    done: list = []
    manager = TaskManager(
        config,
        Memory(config.memory_file),
        Reminders(config.reminders_file),
        client=client,
        confirm=lambda action: False,
        on_done=done.append,
    )
    manager.done_log = done
    yield manager
    manager.shutdown()


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_tasks_run_in_parallel(manager):
    start = time.time()
    tasks = [manager.spawn(f"дело {i}", f"задача {i}") for i in range(3)]

    assert wait_for(lambda: all(t.status == "done" for t in manager.list())), \
        [t.status for t in manager.list()]
    elapsed = time.time() - start

    # Три задачи по REPLY_DELAY каждая: последовательно вышло бы втрое дольше.
    assert elapsed < REPLY_DELAY * 2, f"похоже, задачи шли последовательно: {elapsed:.2f} с"
    assert [t.result for t in tasks] == [f"сделано: дело {i}" for i in range(3)]
    assert len(manager.done_log) == 3


def test_queue_respects_parallel_limit(slow_api, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    config = Config.load({"workspace": str(tmp_path)})
    config.max_parallel_tasks = 1
    client = anthropic.Anthropic(api_key="sk-ant-stub", base_url=slow_api, max_retries=0)
    manager = TaskManager(
        config, Memory(config.memory_file), Reminders(config.reminders_file),
        client=client, confirm=lambda action: False,
    )
    try:
        manager.spawn("первое")
        second = manager.spawn("второе")
        # Пул на одного исполнителя: вторая задача обязана подождать.
        assert wait_for(lambda: manager.get(second.id).status == "queued", timeout=0.2)
        assert wait_for(lambda: manager.get(second.id).status == "done")
    finally:
        manager.shutdown()


def test_cancel_queued_task(manager):
    for i in range(3):
        manager.spawn(f"занимаю пул {i}")
    queued = manager.spawn("эту отменим")

    assert manager.cancel(queued.id)
    assert wait_for(lambda: manager.get(queued.id).status == "cancelled")
    assert manager.get(queued.id).result == ""
    assert not manager.cancel(queued.id), "повторная отмена завершённой задачи невозможна"


def test_workers_are_isolated(manager, tmp_path):
    """У исполнителя своя история, своя персона и нет права плодить исполнителей."""
    task = manager.spawn("посчитать что-нибудь", "счёт")
    assert wait_for(lambda: task.status == "done")

    # История исполнителя лежит отдельно от истории основного диалога.
    worker_history = manager.config.state / "tasks" / f"{task.id}.json"
    assert worker_history.is_file()
    assert not manager.config.history_file.exists()

    worker_config = manager._worker_config()
    assert worker_config.confirm_mode == manager.config.task_confirm_mode == "ask"
    assert worker_config.effort == manager.config.task_effort
    assert "фоновый исполнитель" in worker_config.persona

    ctx = ToolContext(
        config=worker_config,
        memory=manager.memory,
        reminders=manager.reminders,
        confirm=lambda action: False,
        say=lambda text: None,
    )
    names = {t["name"] if isinstance(t, dict) else t.to_dict()["name"]
             for t in build_tools(ctx, with_tasks=False)}
    assert "spawn_task" not in names
    assert "read_file" in names


def test_main_agent_has_task_tools(manager, tmp_path):
    ctx = ToolContext(
        config=manager.config,
        memory=manager.memory,
        reminders=manager.reminders,
        confirm=lambda action: False,
        say=lambda text: None,
        tasks=manager,
    )
    names = {t["name"] if isinstance(t, dict) else t.to_dict()["name"] for t in build_tools(ctx)}
    assert {"spawn_task", "list_tasks", "task_result", "cancel_task"} <= names
