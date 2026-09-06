"""Заглушка Messages API, говорящая потоком.

Джарвис получает ответ по мере готовности, а значит и заглушка должна отдавать
его так же — событиями, а не одним куском. Иначе тесты проверяли бы протокол,
которым код уже не пользуется.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def message(content: list[dict], stop_reason: str = "end_turn", **usage) -> dict:
    """Готовый ответ модели — в том виде, в каком его собирает SDK."""
    return {
        "id": "msg_stub",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 10, **usage},
    }


def as_stream(msg: dict) -> bytes:
    """Разбирает готовый ответ на поток событий."""
    out: list[str] = []

    def send(event: str, data: dict) -> None:
        out.append(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n")

    opening = {k: v for k, v in msg["usage"].items() if k != "output_tokens"}
    send("message_start", {"type": "message_start", "message": {
        **msg, "content": [], "stop_reason": None,
        "usage": {**opening, "output_tokens": 0},
    }})

    for index, block in enumerate(msg["content"]):
        if block["type"] == "text":
            send("content_block_start", {"type": "content_block_start", "index": index,
                                         "content_block": {"type": "text", "text": ""}})
            # Настоящая модель шлёт текст кусками, и куски не совпадают с
            # фразами. Заглушка должна вести себя так же, иначе нарезка на
            # фразы окажется непроверенной.
            for chunk in _chunks(block["text"]):
                send("content_block_delta", {"type": "content_block_delta", "index": index,
                                             "delta": {"type": "text_delta", "text": chunk}})
        elif block["type"] == "tool_use":
            opened = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            send("content_block_start",
                 {"type": "content_block_start", "index": index, "content_block": opened})
            arguments = json.dumps(block.get("input", {}))
            send("content_block_delta",
                 {"type": "content_block_delta", "index": index,
                  "delta": {"type": "input_json_delta", "partial_json": arguments}})
        send("content_block_stop", {"type": "content_block_stop", "index": index})

    send("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": msg["stop_reason"], "stop_sequence": None},
                           "usage": {"output_tokens": msg["usage"]["output_tokens"]}})
    send("message_stop", {"type": "message_stop"})
    return "".join(out).encode("utf-8")


def _chunks(text: str, size: int = 17) -> list[str]:
    """Режет текст произвольными кусками, не по границам слов и фраз."""
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def serve(reply, requests: list | None = None, delay: float = 0.0):
    """Поднимает заглушку. `reply` получает разобранный запрос и отдаёт ответ."""
    import time

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length))
            if requests is not None:
                requests.append(request)
            if delay:
                time.sleep(delay)
            body = as_stream(reply(request))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"
