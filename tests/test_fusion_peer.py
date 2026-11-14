"""Fusion peer: blackboard contract plus a watchdog over the relay's hops.

Covers the edges the relay treats as delivery outcomes: bridge down, duplicate
ids (including ones the ring already evicted), bodies over 4096 bytes, a bad
or missing admin token, and a peer restart announced by ``boot``.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from avrone import server
from avrone.fusion_peer import (
    FUSION_MAX_BODY,
    FusionWatch,
    encode_bounded,
    event_from_post,
)

AUTH = {"X-Admin-Token": "test-token"}
POST = {
    "id": "find0000001",
    "run_id": "run-1",
    "agent": "scout",
    "role": "agent",
    "kind": "finding",
    "text": "heat on the cone",
    "refs": [],
    "ts": 1700000000.0,
    "origin_node": "local",
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _send(self, code: int, payload, *, raw: bytes | None = None) -> None:
        body = raw if raw is not None else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        peer = self.server
        peer.gets.append(self.path.split("?", 1)[0])
        if self.path.startswith("/health"):
            if peer.health_delay:
                time.sleep(peer.health_delay)
            if peer.health_status:
                self._send(peer.health_status, {"detail": "upstream"})
                return
            if self.headers.get("X-Admin-Token"):
                peer.health_saw_token = True
            payload = {"status": "ghost-online", "version": "3.1.0"}
            if not peer.omit_boot:
                payload["boot"] = peer.boot
            self._send(200, payload)
            return
        if self.path.startswith("/api/blackboard"):
            if peer.mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/steal")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.headers.get("X-Admin-Token") != peer.token:
                self._send(401, {"detail": "Bad admin token"})
                return
            if peer.board_body is not None:
                if isinstance(peer.board_body, bytes):
                    self._send(200, None, raw=peer.board_body)
                else:
                    self._send(200, peer.board_body)
                return
            body = {"posts": peer.posts, "count": len(peer.posts), "boot": peer.board_boot}
            self._send(200, body)
            return
        self._send(404, {"detail": "missing"})

    def do_POST(self):
        peer = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        peer.posts_hits += 1
        peer.requests.append({
            "body": raw,
            "key": self.headers.get("Idempotency-Key"),
            "token": self.headers.get("X-Admin-Token"),
        })
        if length > 4096 or len(raw) > 4096 or peer.mode == "413":
            self._send(413, {"detail": "request body too large (max 4096 bytes)"})
            return
        if self.headers.get("X-Admin-Token") != peer.token:
            self._send(401, {"detail": "Bad admin token"})
            return
        if peer.mode == "500":
            self._send(500, {"detail": "upstream"})
            return
        if peer.mode == "400":
            self._send(400, {"detail": "bad"})
            return
        if peer.mode == "404":
            self._send(404, {"detail": "missing"})
            return
        if peer.mode == "429":
            self.send_response(429)
            body = b'{"detail":"slow down"}'
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if peer.retry_after is not None:
                self.send_header("Retry-After", peer.retry_after)
            self.end_headers()
            self.wfile.write(body)
            return
        if peer.mode == "text":
            self._send(200, None, raw=b"not-json")
            return
        if peer.mode == "204":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if peer.mode == "307":
            self.send_response(307)
            self.send_header("Location", "http://127.0.0.1:9/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"detail": "body must be JSON"})
            return
        eid = data.get("id")
        if eid in peer.stored:
            self._send(200, {"accepted": True, "duplicate": True, "id": eid})
            return
        peer.stored[eid] = data
        self._send(200, {"accepted": True, "duplicate": False, "id": eid})


class _Peer(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.token = "secret"
        self.boot = "boot-a"
        self.board_boot = "board-a"
        self.posts: list = []
        self.stored: dict = {}
        self.posts_hits = 0
        self.requests: list = []
        self.mode = "ok"
        self.omit_boot = False
        self.board_body = None
        self.health_saw_token = False
        self.health_delay = 0.0
        self.health_status = 0
        self.retry_after: str | None = None
        self.gets: list[str] = []
        self.timeout = 0.2


class _Running:
    def __init__(self):
        self.httpd = _Peer()
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.httpd

    def __exit__(self, *args):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        return False

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address
        return f"http://{host}:{port}"


# --------------------------------------------------------------------------- blackboard + boot


def test_status_boot_is_stable_and_public(client):
    first = client.get("/api/status")
    assert first.status_code == 200
    body = first.json()
    assert body["runner"] == "local" and body["backend"] == "mock"
    assert body["boot"] == server.boot_id()
    assert len(body["boot"]) == 32 and body["boot"] == client.get("/api/status").json()["boot"]
    board = client.get("/api/blackboard", headers=AUTH)
    assert board.status_code == 200
    assert board.json()["boot"] == body["boot"]
    assert board.json()["posts"] == [] and board.json()["count"] == 0


def test_boot_changes_in_a_new_process():
    code = "from avrone.server import boot_id; print(boot_id(), end='')"
    env = os.environ.copy()
    env["AVRONE_LLM_MODE"] = "mock"
    env["AVRONE_RUNNER"] = "local"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.check_output([sys.executable, "-c", code], env=env, text=True, timeout=90)
    assert len(out) == 32 and out != server.boot_id()
    assert all(c in "0123456789abcdef" for c in out)


def test_blackboard_auth_matrix(client, monkeypatch):
    assert client.get("/api/blackboard").status_code == 401
    assert client.get("/api/blackboard", headers={"X-Admin-Token": "nope"}).json()["detail"] == "Bad admin token"
    assert client.get("/api/blackboard", headers={"X-Admin-Token": "test-token "}).status_code == 401
    # httpx will not put a non-ASCII header on the wire. The compare itself must not raise:
    # a latin-1 token used to 500 inside hmac.compare_digest.
    assert server.token_eq("caf\u00e9", "test-token") is False
    assert server.token_eq("caf\u00e9", "caf\u00e9") is True
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN_PREVIOUS", "old-token ")
    assert client.get("/api/blackboard", headers={"X-Admin-Token": "old-token"}).status_code == 401
    assert client.get("/api/blackboard", headers={"X-Admin-Token": "old-token "}).status_code == 200
    # The grace token does not open other admin routes.
    denied = client.post("/api/terminal", json={"cmd": "echo hi"}, headers={"X-Admin-Token": "old-token "})
    assert denied.status_code == 401
    monkeypatch.setattr(server.settings, "admin_token", "")
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN_PREVIOUS", "old-token")
    off = client.get("/api/blackboard", headers={"X-Admin-Token": "old-token"})
    assert off.status_code == 403
    assert "AVRONE_ADMIN_TOKEN" in off.json()["detail"]


def test_loopback_and_public_access_do_not_open_blackboard(tmp_path, monkeypatch):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "loopback")
    with TestClient(server.app, client=("127.0.0.1", 50000)) as local:
        assert local.get("/api/status").status_code == 200
        assert local.get("/api/runs").status_code == 200  # loopback may read run history
        assert local.get("/api/blackboard").status_code == 401
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "all")
    with TestClient(server.app, client=("203.0.113.9", 50000)) as remote:
        assert remote.get("/api/blackboard").status_code == 401
        assert remote.get("/api/fusion").status_code == 401


def test_oversized_blackboard_is_413_before_auth(client, monkeypatch):
    monkeypatch.setattr(server.settings, "admin_token", "")
    huge = b"x" * 4097
    refused = client.post("/api/blackboard", content=huge, headers={"X-Admin-Token": "nope"})
    assert refused.status_code == 413
    assert refused.json()["detail"] == "request body too large (max 4096 bytes)"
    assert refused.headers.get("connection") == "close"
    # A declared length over the cap never enters the route.
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

    import asyncio
    asyncio.run(server.BodyLimit(app)(
        {"type": "http", "path": "/api/blackboard", "headers": [(b"content-length", b"4097")]},
        receive, send))
    assert called == []
    assert sent[0]["status"] == 413
    assert dict(sent[0]["headers"])[b"connection"] == b"close"
    assert json.loads(sent[1]["body"])["detail"] == "request body too large (max 4096 bytes)"
    # The process-wide cap is unchanged.
    monkeypatch.setenv("AVRONE_MAX_BODY_BYTES", "1")
    monkeypatch.setenv("AVRONE_MAX_MESSAGE_CHARS", "1000")
    assert server.max_body_bytes("/api/blackboard") == 4096
    assert server.max_body_bytes("/api/chat") == 12 * 1000 + 64 * 1024
    assert server.FUSION_BLACKBOARD_MAX_BODY == FUSION_MAX_BODY == 4096


def test_exact_4096_is_accepted_and_4096_of_junk_is_400(client):
    prefix = '{"id":"exact4096","kind":"finding","text":"'
    suffix = '"}'
    pad = 4096 - len(prefix) - len(suffix)
    raw = (prefix + ("a" * pad) + suffix).encode()
    assert len(raw) == 4096
    ok = client.post("/api/blackboard", content=raw, headers={**AUTH, "content-type": "application/json"})
    assert ok.status_code == 200 and ok.json()["duplicate"] is False and ok.json()["id"] == "exact4096"
    junk = b"{" + b"x" * 4095  # 4096 bytes, not JSON
    assert len(junk) == 4096
    bad = client.post("/api/blackboard", content=junk, headers={**AUTH, "content-type": "application/json"})
    assert bad.status_code == 400 and bad.json()["detail"] == "body must be JSON"
    over = client.post("/api/blackboard", content=raw + b"a", headers={"X-Admin-Token": "nope"})
    assert over.status_code == 413


def test_bad_json_kind_and_id(client):
    assert client.post("/api/blackboard", content=b"", headers=AUTH).status_code == 400
    assert client.post("/api/blackboard", content=b"[]", headers=AUTH).json()["detail"] == "body must be JSON"
    assert client.post("/api/blackboard", content=b"\xff\xfe", headers=AUTH).status_code == 400
    mood = client.post("/api/blackboard", json={"kind": "mood", "text": "x"}, headers=AUTH)
    assert mood.status_code == 400 and "kind" in mood.json()["detail"]
    assert client.post("/api/blackboard", json={"text": "   "}, headers=AUTH).status_code == 400
    assert client.post("/api/blackboard", json={"id": "has space", "text": "t"}, headers=AUTH).status_code == 400
    assert client.post("/api/blackboard", json={"id": "x" * 81, "text": "t"}, headers=AUTH).status_code == 400
    assigned = client.post("/api/blackboard", json={"text": "hello"}, headers=AUTH)
    assert assigned.status_code == 200 and len(assigned.json()["id"]) == 12
    kept = client.post("/api/blackboard", json={
        "id": "withrefs01", "text": "t", "kind": "Conclusion",
        "refs": ["missing-ref", "bad ref", 12],
    }, headers=AUTH)
    assert kept.status_code == 200 and kept.json()["duplicate"] is False
    posts = client.get("/api/blackboard", headers=AUTH).json()["posts"]
    found = next(p for p in posts if p["id"] == "withrefs01")
    assert found["kind"] == "conclusion"
    assert "missing-ref" in found["refs"] and "bad ref" not in found["refs"]


def test_duplicate_does_not_overwrite_and_evicted_id_stays_dead(client, monkeypatch):
    first = {"id": "post0001", "text": "one", "kind": "finding", "run_id": "fusion-relay",
             "origin_node": "fusion-bridge", "agent": "cyclekernal:mood", "role": "cyclekernal-relay"}
    assert client.post("/api/blackboard", json=first, headers=AUTH).json()["duplicate"] is False
    first["text"] = "two"
    again = client.post("/api/blackboard", json=first, headers={**AUTH, "Idempotency-Key": "post0001"})
    assert again.json() == {"posted": True, "duplicate": True, "id": "post0001"}
    # The header is not a second dedup key: a new id with the same key is stored.
    other = client.post("/api/blackboard", json={"id": "post0002", "text": "other", "run_id": "fusion-relay"},
                        headers={**AUTH, "Idempotency-Key": "post0001"})
    assert other.json()["duplicate"] is False
    texts = [p["text"] for p in client.get("/api/blackboard", headers=AUTH).json()["posts"] if p["id"] == "post0001"]
    assert texts == ["one"]
    run = client.get("/api/runs/fusion-relay/bridge")
    assert run.status_code == 200
    assert [p["text"] for p in run.json()["posts"] if p["id"] == "post0001"] == ["one"]

    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "1")
    assert client.post("/api/blackboard", json={"id": "evict-a", "text": "a", "run_id": "ring"},
                       headers=AUTH).json()["duplicate"] is False
    assert client.post("/api/blackboard", json={"id": "evict-b", "text": "b", "run_id": "ring"},
                       headers=AUTH).json()["duplicate"] is False
    revived = client.post("/api/blackboard", json={"id": "evict-a", "text": "back", "run_id": "ring"}, headers=AUTH)
    assert revived.json()["duplicate"] is True
    ids = [p["id"] for p in client.get("/api/blackboard", headers=AUTH).json()["posts"] if p["run_id"] == "ring"]
    assert ids == ["evict-b"]


def test_since_cursor_does_not_skip_an_older_timestamp(client):
    assert client.post("/api/blackboard", json={"id": "cursor-a", "text": "new", "ts": 100},
                       headers=AUTH).status_code == 200
    client.get("/api/blackboard", headers=AUTH)  # first snapshot is [cursor-a]
    assert client.post("/api/blackboard", json={"id": "cursor-b", "text": "old", "ts": 50},
                       headers=AUTH).status_code == 200
    page = client.get("/api/blackboard", params={"since": "cursor-a"}, headers=AUTH).json()
    assert [p["id"] for p in page["posts"]] == ["cursor-b"]
    unknown = client.get("/api/blackboard", params={"since": "missing"}, headers=AUTH).json()
    assert [p["id"] for p in unknown["posts"]] == ["cursor-a", "cursor-b"]


def test_disk_journal_is_in_the_fan_in(client):
    folder = Path(server.settings.data_dir) / "bridge"
    folder.mkdir(parents=True)
    rec = {"id": "disk00000001", "run_id": "fromdisk", "agent": "scout", "role": "agent",
           "kind": "finding", "text": "from disk", "refs": [], "ts": 10.0, "origin_node": "local"}
    (folder / "fromdisk.jsonl").write_text(json.dumps(rec) + "\n" + "{not json}\n")
    posts = client.get("/api/blackboard", headers=AUTH).json()["posts"]
    assert [p["text"] for p in posts if p["id"] == "disk00000001"] == ["from disk"]


def test_multibyte_body_over_4096_never_reaches_the_clip(client):
    raw = json.dumps({"id": "emoji000001", "kind": "finding", "text": "é" * 3000}).encode()
    assert len(raw) > 4096
    refused = client.post("/api/blackboard", content=raw, headers=AUTH)
    assert refused.status_code == 413
    assert client.get("/api/blackboard", headers=AUTH).json()["posts"] == []


def test_blackboard_routes_are_admin_on_the_surface():
    from avrone import tools_hostview as hv
    routes = [r for r in hv.api_surface()["routes"] if r["path"] in
              ("/api/blackboard", "/api/fusion", "/api/fusion/watch")]
    assert routes and all(r["auth"] == "admin" for r in routes)
    assert hv.api_surface()["routes"]
    status = next(r for r in hv.api_surface()["routes"] if r["path"] == "/api/status")
    assert status["auth"] == "public"
    assert not any("blackboard" in item or "/api/fusion" in item for item in hv.exposure()["remote_open"])


def test_squad_post_is_visible_to_the_fan_in(client):
    from avrone.bridge import for_core
    posted = for_core(server.core(), "squad-run").post("scout", "finding", "agents concluded")
    posts = client.get("/api/blackboard", headers=AUTH).json()["posts"]
    assert any(p["id"] == posted["id"] and p["text"] == "agents concluded" for p in posts)


# --------------------------------------------------------------------------- watchdog


def _urls(base: str) -> dict:
    return {
        "board_url": base + "/api/blackboard",
        "events_url": base + "/v1/events",
        "health_url": base + "/health",
        "board_token": "secret",
        "events_token": "secret",
        "timeout": 2.0,
    }


def test_watch_forwards_once_and_sends_idempotency_key(tmp_path):
    with _Running() as peer:
        peer.posts = [dict(POST)]
        watch = FusionWatch(tmp_path / "state.json")
        first = watch.watch_once(**_urls(f"http://{peer.server_address[0]}:{peer.server_address[1]}"))
        second = watch.watch_once(**_urls(f"http://{peer.server_address[0]}:{peer.server_address[1]}"))
        assert first["forwarded"] == 1 and first["restart"] is False and first["health"] == "ok"
        assert second["forwarded"] == 0 and second["duplicates"] == 0
        assert peer.posts_hits == 1
        assert peer.requests[0]["key"] == "find0000001"
        assert peer.requests[0]["token"] == "secret"
        assert peer.health_saw_token is False
        stored = peer.stored["find0000001"]
        assert stored["source"] == "avrone" and stored["payload"]["text"] == "heat on the cone"
        assert len(peer.requests[0]["body"]) <= 4096
        again = FusionWatch(tmp_path / "state.json")
        again.watch_once(**_urls(f"http://{peer.server_address[0]}:{peer.server_address[1]}"))
        assert peer.posts_hits == 1  # a restarted watchdog keeps the ids it already delivered


def test_watch_bridge_down_does_not_raise_or_remember(tmp_path):
    port = _closed_port()
    dead = f"http://127.0.0.1:{port}"
    state = tmp_path / "state"
    state.mkdir()  # a directory, not a file: the state write fails closed
    watch = FusionWatch(state)
    report = watch.watch_once(
        board_url=dead + "/api/blackboard", events_url=dead + "/v1/events",
        health_url=dead + "/health", board_token="secret", events_token="secret", timeout=1.0)
    assert report["board"] == "down" and report["health"] == "down"
    assert report["forwarded"] == 0 and report["watched"] is True
    assert report["state_errors"] >= 1
    assert watch.seen == set()


def test_watch_bad_auth_is_not_delivered_then_retries(tmp_path):
    with _Running() as peer:
        peer.posts = [dict(POST)]
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        watch = FusionWatch(tmp_path / "auth.json")
        urls = _urls(base)
        urls["events_token"] = "nope"
        denied = watch.watch_once(**urls)
        assert denied["forwarded"] == 0 and denied["errors"] >= 1
        assert "find0000001" not in watch.seen
        assert peer.posts_hits == 1
        urls["events_token"] = "secret"
        allowed = watch.watch_once(**urls)
        assert allowed["forwarded"] == 1
        assert "find0000001" in watch.seen
        assert peer.posts_hits == 2
        # A wrong board token never reaches the events hop.
        urls["board_token"] = "nope"
        urls["events_token"] = "secret"
        watch2 = FusionWatch()
        blocked = watch2.watch_once(**urls)
        assert blocked["board"] == "auth" and watch2.seen == set()
        assert peer.posts_hits == 2


def test_watch_duplicate_oversized_permanent_and_retryable(tmp_path):
    with _Running() as peer:
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        url = base + "/v1/events"
        watch = FusionWatch()
        peer.stored["find0000001"] = {"id": "find0000001"}
        event = event_from_post(POST)
        dup = watch.submit(url, "secret", event, "find0000001")
        assert dup["duplicate"] is True and dup["remembered"] is True
        assert watch.submit(url, "secret", event, "find0000001")["skipped"] is True
        assert peer.posts_hits == 1

        huge = {"id": "toobig00001", "source": "avrone", "kind": "finding", "run_id": "r",
                "payload": {"blob": "x" * 5000}, "ts": 1}
        oversized = watch.submit(url, "secret", huge, "toobig00001")
        assert oversized["error"] == "oversized" and oversized["remembered"] is True
        assert peer.posts_hits == 1  # never dialed

        clip = dict(event)
        clip["id"] = "clip0000001"
        clip["payload"] = dict(event["payload"])
        clip["payload"]["text"] = "é" * 3000
        clipped = watch.submit(url, "secret", clip, "clip0000001")
        assert clipped["ok"] is True
        raw = peer.requests[-1]["body"]
        assert len(raw) <= 4096
        assert json.loads(raw.decode())["payload"]["text"].endswith("...")
        assert peer.requests[-1]["key"] == "clip0000001"

        peer.mode = "500"
        retry = watch.submit(url, "secret", {**event, "id": "retry000001"}, "retry000001")
        assert retry["error"] == "upstream" and "retry000001" not in watch.seen
        peer.mode = "ok"
        assert watch.submit(url, "secret", {**event, "id": "retry000001"}, "retry000001")["ok"] is True

        peer.mode = "400"
        rejected = watch.submit(url, "secret", {**event, "id": "rej00000001"}, "rej00000001")
        assert rejected["error"] == "rejected" and rejected["remembered"] is True
        hits = peer.posts_hits
        assert watch.submit(url, "secret", {**event, "id": "rej00000001"}, "rej00000001")["skipped"] is True
        assert peer.posts_hits == hits

        peer.mode = "413"
        permanent = watch.submit(url, "secret", {**event, "id": "big41300001"}, "big41300001")
        assert permanent["error"] == "oversized" and "big41300001" in watch.seen
        hits = peer.posts_hits
        watch.submit(url, "secret", {**event, "id": "big41300001"}, "big41300001")
        assert peer.posts_hits == hits


def test_watch_restart_resends_and_missing_boot_does_not(tmp_path):
    with _Running() as peer:
        peer.posts = [dict(POST)]
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        watch = FusionWatch(tmp_path / "boot.json")
        urls = _urls(base)
        assert watch.watch_once(**urls)["forwarded"] == 1
        assert watch.watch_once(**urls)["forwarded"] == 0
        assert peer.posts_hits == 1
        peer.boot = "boot-b"  # CycleKernal process restarted and forgot ids
        peer.stored.clear()
        resent = watch.watch_once(**urls)
        assert resent["restart"] is True and resent["forwarded"] == 1
        assert peer.posts_hits == 2
        assert watch.watch_once(**urls)["forwarded"] == 0
        peer.omit_boot = True  # an older peer with no boot field
        assert watch.watch_once(**urls)["restart"] is False
        assert peer.posts_hits == 2
        peer.omit_boot = False
        peer.boot = 4  # integer epoch
        peer.stored.clear()
        assert watch.watch_once(**urls)["restart"] is True
        assert peer.posts_hits == 3


def test_watch_echo_malformed_redirect_and_health_down():
    with _Running() as peer:
        echo = dict(POST, id="ck-mood-aaaaaaaa", origin_node="fusion-bridge",
                    role="cyclekernal-relay", agent="cyclekernal:mood", kind="conclusion",
                    text='{"agent":"cyclekernal:mood","mood":"convergent"}')
        peer.posts = [echo, dict(POST)]
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        report = FusionWatch().watch_once(**_urls(base))
        assert report["forwarded"] == 1 and report["skipped"] >= 1
        assert list(peer.stored) == ["find0000001"]

        peer.board_body = {"nope": 1}
        malformed = FusionWatch().watch_once(**_urls(base))
        assert malformed["board"] == "malformed" and peer.posts_hits == 1

        peer.board_body = [dict(POST, id="array0000001", text="from an array")]
        peer.mode = "ok"
        listed = FusionWatch().watch_once(**_urls(base))
        assert listed["forwarded"] == 1
        assert "array0000001" in peer.stored

        peer.mode = "redirect"
        peer.board_body = None
        redirected = FusionWatch().watch_once(**_urls(base))
        assert redirected["board"] == "redirect"
        assert "find0000002" not in peer.stored

    port = _closed_port()
    with _Running() as peer:
        peer.posts = [dict(POST, id="healthdown01", text="still forwarded")]
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        urls = _urls(base)
        urls["health_url"] = f"http://127.0.0.1:{port}/health"
        report = FusionWatch().watch_once(**urls)
        assert report["health"] == "down" and report["forwarded"] == 1


def test_watch_timeout_is_not_delivered():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    held = []

    def accept_and_hold():
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        held.append(conn)
        time.sleep(1.5)
        try:
            conn.close()
        except OSError:
            pass

    threading.Thread(target=accept_and_hold, daemon=True).start()
    try:
        watch = FusionWatch()
        started = time.monotonic()
        result = watch.submit(
            f"http://127.0.0.1:{port}/v1/events", "secret",
            event_from_post(POST), "find0000001", timeout=0.4)
        elapsed = time.monotonic() - started
        assert result["error"] == "timeout" and result["remembered"] is False
        assert "find0000001" not in watch.seen
        assert elapsed < 1.2
    finally:
        sock.close()
        for conn in held:
            try:
                conn.close()
            except OSError:
                pass


def test_corrupt_state_is_discarded(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{")
    watch = FusionWatch(path)
    assert watch.state_errors == 1 and watch.seen == set()
    assert encode_bounded({"payload": {"text": "ok"}, "id": "a"}) is not None
    assert encode_bounded({"payload": {"blob": "x" * 5000}}) is None


def test_fusion_routes_do_not_dial_until_configured(client, monkeypatch, tmp_path):
    assert client.get("/api/fusion").status_code == 401
    assert client.post("/api/fusion/watch").status_code == 401
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    monkeypatch.setenv("FUSION_BOARD_URL", f"http://127.0.0.1:{port}/api/blackboard")
    monkeypatch.delenv("FUSION_EVENTS_URL", raising=False)
    monkeypatch.delenv("FUSION_HEALTH_URL", raising=False)
    try:
        started = time.monotonic()
        unset = client.post("/api/fusion/watch", headers=AUTH)
        assert time.monotonic() - started < 0.5
        assert unset.json() == {"watched": False, "reason": "unset"}
    finally:
        sock.close()
    opened = client.get("/api/fusion", headers=AUTH).json()
    assert opened["boot"] == server.boot_id()
    assert opened["blackboard"] == "/api/blackboard"

    with _Running() as peer:
        peer.posts = [dict(POST)]
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        monkeypatch.setenv("FUSION_BOARD_URL", base + "/api/blackboard")
        monkeypatch.setenv("FUSION_EVENTS_URL", base + "/v1/events")
        monkeypatch.setenv("FUSION_HEALTH_URL", base + "/health")
        monkeypatch.setenv("FUSION_TOKEN", "secret")
        monkeypatch.setenv("AVRONE_FUSION_WATCH_PATH", str(tmp_path / "route-state.json"))
        monkeypatch.setenv("FUSION_TIMEOUT", "2")
        first = client.post("/api/fusion/watch", headers=AUTH)
        second = client.post("/api/fusion/watch", headers=AUTH)
        assert first.status_code == 200 and first.json()["forwarded"] == 1
        assert second.json()["forwarded"] == 0
        assert peer.posts_hits == 1
        assert "secret" not in first.text
        status = client.get("/api/fusion", headers=AUTH).json()
        assert status["watch"]["forwarded"] == 0
        assert status["boot"] == opened["boot"]


def test_concurrent_accepts_keep_every_id(tmp_path):
    from types import SimpleNamespace
    from avrone.fusion_peer import blackboard_accept, blackboard_posts, normalize_post
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))
    errors = []

    def one(pid: str) -> None:
        try:
            post, err = normalize_post({"id": pid, "text": "t", "kind": "finding", "run_id": "batch"})
            assert err is None and post is not None
            duplicate, stored = blackboard_accept(core, post)
            assert duplicate is False and stored == pid
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    ids = [f"batch{i:04d}" for i in range(16)]
    threads = [threading.Thread(target=one, args=(pid,)) for pid in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(p["id"] for p in blackboard_posts(core)) == sorted(ids)
