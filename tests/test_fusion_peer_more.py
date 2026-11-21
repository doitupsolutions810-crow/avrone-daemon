"""Another pass of fusion-peer edges: headers, redirects, deadlines, and bad JSON.

These are the failures that still escaped the earlier suites: a token that is
not latin-1 looked like an outage, a lone surrogate raised out of a cycle that
promises not to raise, and one slow health read was allowed to dial every
queued post.
"""
import asyncio
import json
import time

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


def test_non_string_ids_are_rejected(client):
    for bad in (True, False, 12, 1.5, ["ok-id"], {"id": "ok-id"}):
        refused = client.post("/api/blackboard", json={"id": bad, "text": "t"}, headers=AUTH)
        assert refused.status_code == 400 and refused.json()["detail"] == "bad post id", bad
    assigned = client.post("/api/blackboard", json={"id": None, "text": "from null"}, headers=AUTH)
    assert assigned.status_code == 200 and len(assigned.json()["id"]) == 12
    kept = client.post("/api/blackboard", json={"id": "ok.id", "text": "kept"}, headers=AUTH)
    assert kept.status_code == 200 and kept.json()["duplicate"] is False
    ids = [p["id"] for p in client.get("/api/blackboard", headers=AUTH).json()["posts"]]
    assert "True" not in ids and "12" not in ids and "ok.id" in ids
    # main's blackboard rejects a non-string run id instead of storing "7" or "True".
    for bad_run in (7, True, ["run"]):
        refused = client.post("/api/blackboard", json={"text": "n", "run_id": bad_run}, headers=AUTH)
        assert refused.status_code == 400 and refused.json()["detail"] == "bad run id", bad_run


def test_userinfo_and_empty_host_are_not_dialed():
    with _Running() as peer:
        host, port = peer.server_address
        before = list(peer.gets)
        leaked = fp._exchange("GET", f"http://user:secret@{host}:{port}/health")
        assert leaked["error"] == "down"
        assert fp._exchange("GET", "http:///health")["error"] == "down"
        # NEL and a line separator are line breaks even though NEL is latin-1.
        for ugly in ("\u0085", "\u2028", "\u202e"):
            assert fp._exchange("GET", f"http://{host}:{port}/health{ugly}")["error"] == "down"
            hopped = fp._exchange("GET", f"http://{host}:{port}/health", token=f"secret{ugly}")
            assert hopped["error"] == "auth"
        assert peer.gets == before


def test_fusion_post_keeps_text_and_display_stays_one_line(client):
    """main stores bridge text as written and flattens it only when printing.

    A fusion post follows that same split: GET returns the characters, and
    compact() does not let U+2028, NEL, or a bidi override become a second line.
    """
    from avrone.bridge import compact
    text = "line\u2028next\u0085x\u202ehidden"
    posted = client.post("/api/blackboard", json={
        "id": "bidi00000001", "text": text, "agent": "scout\u202e", "origin_node": "node\u0085",
    }, headers=AUTH)
    assert posted.status_code == 200 and posted.json()["duplicate"] is False
    found = next(p for p in client.get("/api/blackboard", headers=AUTH).json()["posts"]
                 if p["id"] == "bidi00000001")
    assert found["text"] == text
    shown = compact([found])
    assert "\u2028" not in shown and "\u0085" not in shown and "\u202e" not in shown
    assert shown.count("\n") == 0


def test_breaker_opens_on_timeout_not_on_500_and_a_probe_recovers():
    with _Running() as peer:
        peer.health_delay = 0.45
        peer.posts = [dict(POST)]
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        watch = fp.FusionWatch()
        urls = _urls(base)
        urls.update(timeout=0.15, breaker_threshold=1, breaker_cooldown=30.0)
        first = watch.watch_once(**urls)
        assert first["health"] == "timeout" and first["board"] == "circuit"
        assert first["forwarded"] == 0 and "find0000001" not in watch.seen
        assert peer.gets == ["/health"]
        started = time.monotonic()
        second = watch.watch_once(**urls)
        assert time.monotonic() - started < 0.1
        assert second["health"] == "circuit" and second["board"] == "circuit"
        assert peer.gets == ["/health"]
        for host in list(watch._open_until):
            watch._open_until[host] = time.monotonic() - 1
        watch._probing.clear()
        peer.health_delay = 0
        recovered = watch.watch_once(**urls)
        assert recovered["forwarded"] == 1
        assert any(path.startswith("/api/blackboard") for path in peer.gets)

    with _Running() as peer:
        peer.health_status = 500
        peer.posts = [dict(POST, id="find0000002", text="after 500")]
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        watch = fp.FusionWatch()
        urls = _urls(base)
        urls.update(breaker_threshold=1, breaker_cooldown=30.0)
        opened = watch.watch_once(**urls)
        again = watch.watch_once(**urls)
        assert opened["health"] == "upstream" and opened["forwarded"] == 1
        assert again["health"] == "upstream" and again["board"] == "ok"
        assert peer.gets.count("/health") == 2


def test_loopback_spellings_share_a_breaker_and_a_proxy_is_ignored(monkeypatch):
    with _Running() as peer:
        host, port = peer.server_address
        base = f"http://{host}:{port}"
        watch = fp.FusionWatch()
        urls = _urls(base)
        urls.update(timeout=0.15, breaker_threshold=1, breaker_cooldown=30.0)
        peer.health_delay = 0.4
        opened = watch.watch_once(**urls)
        assert opened["health"] == "timeout"
        assert watch._circuit_blocked(f"http://[::1]:{port}/health") is True
        assert watch._circuit_blocked(f"http://[::ffff:127.0.0.1]:{port}/health") is True
        assert watch._circuit_blocked(f"http://127.0.0.2:{port}/health") is False
        before = list(peer.gets)
        skipped = watch._dial("GET", f"http://[::1]:{port}/health", timeout=0.2)
        assert skipped["error"] == "circuit"
        assert peer.gets == before

    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    with _Running() as peer:
        host, port = peer.server_address
        hop = fp._exchange("GET", f"http://{host}:{port}/health", timeout=1.0)
        assert hop["ok"] is True and hop["body"]["status"] == "ghost-online"
        assert "/health" in peer.gets


def test_empty_2xx_is_delivered_and_ports_do_not_share_a_breaker_and_a_ledger_file_is_kept(tmp_path):
    with _Running() as peer:
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        watch = fp.FusionWatch()
        peer.mode = "204"
        event = fp.event_from_post(POST)
        first = watch.submit(base + "/v1/events", "secret", event, "find0000001")
        assert first["ok"] is True and first["remembered"] is True and first["duplicate"] is False
        second = watch.submit(base + "/v1/events", "secret", event, "find0000001")
        assert second["skipped"] is True
        assert peer.posts_hits == 1

    with _Running() as left, _Running() as right:
        left.health_delay = 0.4
        watch = fp.FusionWatch()
        urls = _urls(f"http://127.0.0.1:{left.server_address[1]}")
        urls.update(timeout=0.15, breaker_threshold=1, breaker_cooldown=30.0)
        assert watch.watch_once(**urls)["health"] == "timeout"
        other = f"http://127.0.0.1:{right.server_address[1]}/health"
        assert watch._circuit_blocked(other) is False
        hop = watch._dial("GET", other, timeout=1.0)
        assert hop["ok"] is True and "/health" in right.gets

    ledger = {
        "version": 2,
        "cursor": "keep",
        "boots": {"cyclekernal": "boot-a"},
        "entries": [{
            "source": "avrone", "id": "find0000001", "state": "pending",
            "event": {"id": "find0000001", "source": "avrone"}, "detail": None,
            "attempts": 0, "status": None,
        }],
        "state_errors": 0,
    }
    path = tmp_path / "ledger.json"
    raw = json.dumps(ledger)
    path.write_text(raw)
    watch = fp.FusionWatch(path)
    assert watch.seen == set() and watch._writable is False
    assert watch._save() is False
    assert path.read_text() == raw


def test_retry_after_holds_the_origin_and_garbage_does_not_spin(tmp_path):
    with _Running() as peer:
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        url = base + "/v1/events"
        watch = fp.FusionWatch()
        event = fp.event_from_post(POST)
        peer.mode = "429"
        peer.retry_after = "0"
        first = watch.submit(url, "secret", event, "find0000001")
        assert first["error"] == "retry" and first["remembered"] is False
        assert peer.posts_hits == 1
        held = watch.submit(url, "secret", event, "find0000001")
        assert held["error"] == "retry" and peer.posts_hits == 1
        watch._hold_until.clear()
        peer.retry_after = "31"
        watch.submit(url, "secret", event, "find0000001")
        wait = next(iter(watch._hold_until.values())) - time.monotonic()
        assert 0 < wait < 0.2
        watch._hold_until.clear()
        peer.retry_after = "Wednesday"
        watch.submit(url, "secret", event, "find0000001")
        wait = next(iter(watch._hold_until.values())) - time.monotonic()
        assert 0 < wait < 0.2
        peer.mode = "ok"
        watch._hold_until.clear()
        assert watch.submit(url, "secret", event, "find0000001")["ok"] is True

    with _Running() as peer:
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        watch = fp.FusionWatch()
        peer.mode = "429"
        peer.retry_after = "0.4"
        event = fp.event_from_post(dict(POST, id="wait00000001", text="later"))
        assert watch.submit(base + "/v1/events", "secret", event, "wait00000001")["error"] == "retry"
        other = _Running()
        try:
            other.__enter__()
            hopped = watch._dial("GET", f"http://127.0.0.1:{other.httpd.server_address[1]}/health", timeout=1.0)
            assert hopped["ok"] is True
        finally:
            other.__exit__(None, None, None)


def test_breaker_env_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("FUSION_BREAKER_THRESHOLD", raising=False)
    monkeypatch.delenv("FUSION_BREAKER_COOLDOWN", raising=False)
    assert server._fusion_breaker() == (1, 30.0)
    monkeypatch.setenv("FUSION_BREAKER_THRESHOLD", "0")
    assert server._fusion_breaker()[0] == 0
    monkeypatch.setenv("FUSION_BREAKER_THRESHOLD", "junk")
    monkeypatch.setenv("FUSION_BREAKER_COOLDOWN", "inf")
    assert server._fusion_breaker() == (1, 30.0)
    monkeypatch.setenv("FUSION_BREAKER_THRESHOLD", "-3")
    monkeypatch.setenv("FUSION_BREAKER_COOLDOWN", "9999")
    assert server._fusion_breaker() == (0, 600.0)
