"""Round 123: a lone UTF-16 surrogate in a request no longer breaks the HTTP API.

v0.115 (probe /workspace/work/surfuzz.py): POST /api/chat {"message": "\\ud800"} (valid JSON)
answered 500, because the run's task text could not be rendered as UTF-8. Worse, the text stayed
in the live agent table and the run list, so every later GET /api/status and /api/runs also
answered 500 until the daemon restarted.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

BODY = b'{"message": "plan \\ud800 now", "mode": "squad", "squad_size": 2}'


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setattr(server.settings, "max_steps", 2)
    monkeypatch.setattr(server.settings, "llm_mode", "mock")
    with TestClient(server.app, raise_server_exceptions=False) as c:
        yield c


def _post(c, body=BODY):
    return c.post("/api/chat", content=body, headers={"content-type": "application/json"})


def test_chat_with_lone_surrogate_answers(client):
    r = _post(client)
    assert r.status_code == 200, r.text[:200]
    data = json.loads(r.content)  # valid JSON: the surrogate travels as a \\u escape
    assert data["answer"]


@pytest.mark.parametrize("path", ["/api/status", "/api/runs"])
def test_later_reads_still_work(client, path):
    assert _post(client).status_code == 200
    r = client.get(path)
    assert r.status_code == 200, r.text[:200]
    json.loads(r.content)


def test_run_events_route_survives(client):
    rid = json.loads(_post(client).content)["run_id"]
    r = client.get(f"/api/runs/{rid}")
    assert r.status_code in (200, 404), r.text[:200]
    if r.status_code == 200:
        json.loads(r.content)


def test_normal_text_stays_utf8(client):
    r = _post(client, '{"message": "café 研究"}'.encode())
    assert r.status_code == 200
    assert "研究".encode() in r.content  # the fast path still sends real UTF-8, not escapes


def test_safe_response_render():
    from avrone.server import SafeJSONResponse
    out = SafeJSONResponse({"a": "x\ud800y", "b": "é"}).body
    assert json.loads(out) == {"a": "x\ud800y", "b": "é"}


def test_mermaid_map_survives(client, monkeypatch):
    from avrone import telemetry
    monkeypatch.setattr(telemetry, "to_mermaid", lambda g: "graph TD\n  A[\ud800]")
    r = client.get("/api/map", params={"format": "mermaid"})
    assert r.status_code == 200, r.text[:200]
