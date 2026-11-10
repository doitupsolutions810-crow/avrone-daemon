"""Another pass of fusion-peer edges: headers, redirects, deadlines, and bad JSON.

These are the failures that still escaped the earlier suites: a token that is
not latin-1 looked like an outage, a lone surrogate raised out of a cycle that
promises not to raise, and one slow health read was allowed to dial every
queued post.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from avrone import fusion_peer as fp
from avrone import server
from tests.test_fusion_peer import AUTH, POST, _Running, _urls


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def test_bom_null_and_string_bodies(client):
    bom = b'\xef\xbb\xbf{"id":"bom000000001","text":"from a bom"}'
    posted = client.post("/api/blackboard", content=bom, headers={**AUTH, "content-type": "application/json"})
    assert posted.status_code == 200 and posted.json()["duplicate"] is False
    stored = client.get("/api/blackboard", headers=AUTH).json()["posts"]
    assert any(p["id"] == "bom000000001" and p["text"] == "from a bom" for p in stored)
    for body in (b"null", b'"just a string"', b"   ", b"\xef\xbb\xbf"):
        refused = client.post("/api/blackboard", content=body, headers=AUTH)
        assert refused.status_code == 400 and refused.json()["detail"] == "body must be JSON"
    repeated = client.get("/api/blackboard", params=[("since", "missing-a"), ("since", "missing-b")], headers=AUTH)
    assert repeated.status_code == 200 and repeated.json()["count"] >= 1


def test_absurd_content_length_never_enters_the_route():
    called = []

    async def app(scope, receive, send):
        called.append(True)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    huge = b"9" * 80
    asyncio.run(server.BodyLimit(app)(
        {"type": "http", "path": "/api/blackboard", "headers": [(b"content-length", huge)]},
        receive, send))
    assert called == []
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["detail"] == "request body too large (max 4096 bytes)"


def test_surrogate_and_circular_values_do_not_raise():
    event = fp.event_from_post(dict(POST, text="hot \ud800 spot"))
    assert event is not None
    raw = fp.encode_bounded(event)
    assert raw is not None and len(raw) <= 4096
    parsed = json.loads(raw.decode("utf-8"))
    assert "hot" in parsed["payload"]["text"]
    loop = {"payload": {"text": "x"}}
    loop["payload"]["self"] = loop
    assert fp.encode_bounded(loop) is None
    nan = {"id": "n", "payload": {"text": "x"}, "ts": float("nan")}
    assert fp.encode_bounded(nan) is None


def test_non_latin1_token_and_post_redirect_do_not_follow(tmp_path):
    with _Running() as peer:
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        url = base + "/v1/events"
        watch = fp.FusionWatch(tmp_path / "state.json")
        event = fp.event_from_post(dict(POST, text="hot \ud800 spot"))
        blocked = watch.submit(url, "sekret\u0100", event, "find0000001")
        assert blocked["error"] == "auth" and blocked["remembered"] is False
        assert peer.posts_hits == 0 and "find0000001" not in watch.seen
        sent = watch.submit(url, "secret", event, "find0000001")
        assert sent["ok"] is True and peer.posts_hits == 1
        assert "\\ud800" in peer.requests[0]["body"].decode("ascii") or "ud800" in peer.requests[0]["body"].decode("utf-8")
        peer.mode = "307"
        redirected = watch.submit(url, "secret", fp.event_from_post(dict(POST, id="redir0000001", text="no")), "redir0000001")
        assert redirected["error"] == "redirect" and redirected["remembered"] is False
        assert "redir0000001" not in watch.seen
        assert "redir0000001" not in peer.stored


def test_cycle_deadline_does_not_dial_the_board_after_a_slow_health():
    with _Running() as peer:
        peer.health_delay = 0.8
        peer.posts = [dict(POST)]
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        watch = fp.FusionWatch()
        urls = _urls(base)
        urls["deadline"] = 0.25
        urls["timeout"] = 2.0
        import time
        started = time.monotonic()
        report = watch.watch_once(**urls)
        elapsed = time.monotonic() - started
        assert report["health"] == "timeout"
        assert report["board"] == "timeout"
        assert report["forwarded"] == 0
        assert "find0000001" not in watch.seen
        assert elapsed < 0.7
        assert not any(path.startswith("/api/blackboard") for path in peer.gets)
        peer.health_delay = 0
        recovered = watch.watch_once(**_urls(base))
        assert recovered["forwarded"] == 1
        assert any(path.startswith("/api/blackboard") for path in peer.gets)


def test_deadline_env_and_zero_disables(monkeypatch):
    monkeypatch.delenv("FUSION_CYCLE_DEADLINE", raising=False)
    assert server._fusion_deadline() == 10.0
    monkeypatch.setenv("FUSION_CYCLE_DEADLINE", "0")
    assert server._fusion_deadline() is None
    monkeypatch.setenv("FUSION_CYCLE_DEADLINE", "inf")
    assert server._fusion_deadline() is None
    monkeypatch.setenv("FUSION_CYCLE_DEADLINE", "junk")
    assert server._fusion_deadline() == 10.0
    monkeypatch.setenv("FUSION_CYCLE_DEADLINE", "999")
    assert server._fusion_deadline() == 120.0
    monkeypatch.setenv("FUSION_CYCLE_DEADLINE", "0.4")
    assert server._fusion_deadline() == 0.4
