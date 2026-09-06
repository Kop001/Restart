"""Панель без токена не отдаёт ничего: она открыта в локальную сеть."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jarvis.config import Config
from jarvis.dashboard.auth import load_or_create_token, matches, token_from_request
from jarvis.dashboard.server import Backend, make_handler


@pytest.fixture
def panel(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stub")
    backend = Backend(Config.load({"workspace": str(tmp_path)}))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(backend))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", backend
    backend.tasks.shutdown()
    server.shutdown()


def get(url: str, headers: dict | None = None):
    request = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(request)


def test_no_token_is_rejected(panel):
    base, _ = panel
    for path in ("/", "/api/state", "/api/pending"):
        with pytest.raises(urllib.error.HTTPError) as failure:
            get(base + path)
        assert failure.value.code == 401, path


def test_wrong_token_is_rejected(panel):
    base, _ = panel
    with pytest.raises(urllib.error.HTTPError) as failure:
        get(f"{base}/api/state?token=guessed-wrong-token")
    assert failure.value.code == 401


def test_token_in_query_works_and_is_pinned_to_cookie(panel):
    base, backend = panel
    response = get(f"{base}/?token={backend.token}")
    assert response.status == 200
    # Токен закрепляется в cookie, чтобы не остаться в адресной строке.
    assert f"jarvis_token={backend.token}" in response.headers["Set-Cookie"]


def test_token_in_cookie_works(panel):
    base, backend = panel
    response = get(f"{base}/api/state", {"Cookie": f"jarvis_token={backend.token}"})
    assert json.loads(response.read())["config"]["model"]


def test_post_also_requires_token(panel):
    base, _ = panel
    request = urllib.request.Request(
        base + "/api/reset", data=b"{}", headers={"Content-Type": "application/json"}
    )
    with pytest.raises(urllib.error.HTTPError) as failure:
        urllib.request.urlopen(request)
    assert failure.value.code == 401


def test_token_survives_restart(tmp_path):
    path = tmp_path / "panel-token"
    first = load_or_create_token(path)
    assert load_or_create_token(path) == first
    assert path.stat().st_mode & 0o777 == 0o600, "токен читает только владелец"


@pytest.mark.parametrize(
    "query,cookie,expected",
    [
        ("token=abc", None, "abc"),
        ("x=1&token=abc&y=2", None, "abc"),
        ("", "jarvis_token=abc; other=1", "abc"),
        ("", None, None),
        ("token=", None, None),
    ],
)
def test_token_extraction(query, cookie, expected):
    assert token_from_request(query, cookie) == expected


def test_matches_rejects_empty():
    assert matches("secret", "secret")
    assert not matches("secret", "")
    assert not matches("secret", None)
