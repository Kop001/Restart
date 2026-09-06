"""Интеграционный тест агента против заглушки Messages API.

Ключ API не нужен: SDK ходит на локальный сервер, который отдаёт заранее
заготовленные ответы. Проверяем форму запроса и цикл вызова инструментов.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
import pytest

from jarvis.agent import Agent
from jarvis.config import Config
from jarvis.memory import History


def _message(content: list[dict], stop_reason: str) -> dict:
    return {
        "id": "msg_stub",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 80},
    }


SCRIPT = [
    _message([{"type": "tool_use", "id": "tu_1", "name": "current_time", "input": {}}], "tool_use"),
    _message([{"type": "text", "text": "Половина одиннадцатого."}], "end_turn"),
]


@pytest.fixture
def stub_api():
    requests: list[dict] = []
    replies = iter(SCRIPT)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            requests.append(json.loads(self.rfile.read(length)))
            body = json.dumps(next(replies)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()


def test_agent_runs_tool_loop(stub_api, tmp_path, monkeypatch):
    base_url, requests = stub_api
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    config = Config.load({"workspace": str(tmp_path), "confirm_mode": "auto"})
    client = anthropic.Anthropic(api_key="sk-ant-stub", base_url=base_url, max_retries=0)
    agent = Agent(config, client=client, confirm=lambda action: True, say=lambda text: None)

    answer = agent.ask("который час?")

    assert answer == "Половина одиннадцатого."
    assert len(requests) == 2, "должен быть второй запрос с результатом инструмента"

    first = requests[0]
    assert first["model"] == "claude-opus-5"
    assert first["thinking"] == {"type": "adaptive"}
    assert first["output_config"]["effort"] == config.effort
    assert first["fallbacks"] == "default"
    # Персона идёт первым блоком и кэшируется — она не меняется между ходами.
    assert "cache_control" in first["system"][0]
    assert any(tool.get("name") == "web_search" for tool in first["tools"])

    second = requests[1]
    assert any(
        block.get("type") == "tool_result"
        for message in second["messages"]
        if isinstance(message["content"], list)
        for block in message["content"]
    )

    assert agent.stats == {
        "requests": 2, "turns": 1, "input_tokens": 200,
        "output_tokens": 20, "cache_read_tokens": 160, "tool_calls": 1,
    }
    # История должна переживать перезапуск процесса — значит, быть простым JSON.
    assert len(History(config.history_file, 40).messages) == 4
