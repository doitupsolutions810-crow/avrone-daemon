"""Harder edges around the fusion peer: hostile files, transient HTTP, and bad values.

The cases here are the ones that used to hang a read, follow a symlink, drop a
throttled post, or overwrite a state file this process does not own.
"""
import json
import os
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from avrone import fusion_peer as fp
from avrone import server
from tests.test_fusion_peer import AUTH, POST, _Running, _urls

AUTH_H = AUTH


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def _core(tmp_path):
    return SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path))


def test_hostile_ts_refs_and_run_id_stay_inside_the_data_dir(client):
    huge = "9" * 400
    body = {
        "id": "oddts000001",
        "text": "kept",
        "ts": True,
        "refs": [f"r{i:02d}" for i in range(25)] + ["bad ref", "r00"],
        "agent": "A" * 200,
        "role": "R" * 200,
        "origin_node": "O" * 200,
        "run_id": "../../etc/passwd",
        "kind": "  Finding ",
    }
    # A 400-digit integer is accepted by JSON and used to raise in float().
    raw = json.dumps(body).replace('"ts": true', f'"ts": {huge}')
    posted = client.post("/api/blackboard", content=raw.encode(), headers={**AUTH_H, "content-type": "application/json"})
    assert posted.status_code == 200 and posted.json()["duplicate"] is False
    inf = client.post(
        "/api/blackboard", content=b'{"id":"inf-ts00001","text":"n","ts":1e999}',
        headers={**AUTH_H, "content-type": "application/json"})
    assert inf.status_code == 200
    posts = client.get("/api/blackboard", headers=AUTH_H).json()["posts"]
    odd = next(p for p in posts if p["id"] == "oddts000001")
    assert odd["kind"] == "finding"
    assert len(odd["refs"]) == 20 and "bad ref" not in odd["refs"]
    assert len(odd["agent"]) == 80 and len(odd["role"]) == 60 and len(odd["origin_node"]) == 80
    assert math_finite(odd["ts"]) and math_finite(next(p["ts"] for p in posts if p["id"] == "inf-ts00001"))
    folder = Path(server.settings.data_dir) / "bridge"
    files = list(folder.glob("*.jsonl"))
    assert files and all(p.resolve().is_relative_to(folder.resolve()) for p in files)
    assert not any(part == ".." for p in files for part in p.parts)
    # A lone surrogate in the text is stored, not a 500.
    surrogate = client.post("/api/blackboard", content=b'{"id":"surrogate01","text":"\\ud800"}',
                            headers={**AUTH_H, "content-type": "application/json"})
    assert surrogate.status_code == 200


def math_finite(value) -> bool:
    return isinstance(value, float) and value > 0 and value == value and value != float("inf")


def test_deep_json_is_400_not_500(client):
    nested = "[" * 2000 + "]" * 2000  # under the 4096-byte cap, deeper than the parser's stack
    refused = client.post("/api/blackboard", content=nested.encode(), headers=AUTH_H)
    assert refused.status_code == 400 and refused.json()["detail"] == "body must be JSON"


def test_chunked_oversize_is_413_before_auth(client):
    def chunks():
        yield b"y" * 5000

    refused = client.post("/api/blackboard", content=chunks(), headers={"X-Admin-Token": "nope"})
    assert refused.status_code == 413
    assert refused.json()["detail"] == "request body too large (max 4096 bytes)"
    assert client.get("/api/blackboard", headers=AUTH_H).json()["count"] == 0


def test_empty_previous_token_does_not_match(client, monkeypatch):
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN_PREVIOUS", "")
    assert client.get("/api/blackboard", headers={"X-Admin-Token": ""}).status_code == 401
    monkeypatch.setenv("AVRONE_ADMIN_TOKEN_PREVIOUS", "test-token")
    # The current token still works, and so does the same value parked as previous.
    assert client.get("/api/blackboard", headers=AUTH_H).status_code == 200


def test_symlink_journal_and_fifo_order_do_not_block_or_escape(tmp_path):
    core = _core(tmp_path)
    folder = tmp_path / "bridge"
    folder.mkdir()
    outside = tmp_path / "outside.jsonl"
    secret = {"id": "secret000001", "run_id": "out", "agent": "a", "role": "r", "kind": "finding",
              "text": "secret", "refs": [], "ts": 1.0, "origin_node": "local"}
    outside.write_text(json.dumps(secret) + "\n")
    (folder / "linked.jsonl").symlink_to(outside)
    order = folder / "_fanin_order.json"
    order.symlink_to(outside)
    started = time.monotonic()
    posts = fp.blackboard_posts(core)
    assert time.monotonic() - started < 1.0
    assert posts == []
    assert (folder / "linked.jsonl").is_symlink()
    assert order.is_symlink()
    # A FIFO at the order path used to block the fan-in on open().
    order.unlink()
    os.mkfifo(order)
    started = time.monotonic()
    assert fp.blackboard_posts(core) == []
    assert time.monotonic() - started < 1.0
    assert order.is_fifo()


def test_disk_cap_skips_the_rest_and_a_file_bridge_dir_does_not_500(tmp_path, monkeypatch):
    monkeypatch.setattr(fp, "MAX_DISK_BRIDGES", 1)
    core = _core(tmp_path)
    folder = tmp_path / "bridge"
    folder.mkdir()

    def write(name, pid, ts):
        rec = {"id": pid, "run_id": name, "agent": "a", "role": "r", "kind": "finding",
               "text": name, "refs": [], "ts": ts, "origin_node": "local"}
        (folder / f"{name}.jsonl").write_text(json.dumps(rec) + "\n")

    write("a-run", "aaaa00000001", 1.0)
    write("b-run", "bbbb00000001", 2.0)
    ids = [p["id"] for p in fp.blackboard_posts(core)]
    assert ids == ["aaaa00000001"]
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "bridge").write_text("not a directory")
    core2 = _core(blocked)
    assert fp.blackboard_posts(core2) == []
    post, err = fp.normalize_post({"id": "mem000000001", "text": "memory", "run_id": "mem"})
    assert err is None and post is not None
    with pytest.raises(fp.BridgeError):
        fp.blackboard_accept(core2, post)


def test_concurrent_same_id_stores_one_post(tmp_path):
    core = _core(tmp_path)
    results = []

    def one():
        post, err = fp.normalize_post({"id": "same0000001", "text": "only", "run_id": "race"})
        assert err is None and post is not None
        results.append(fp.blackboard_accept(core, post))

    threads = [threading.Thread(target=one) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    flags = [duplicate for duplicate, _pid in results]
    assert flags.count(False) == 1 and flags.count(True) == 7
    assert [p["text"] for p in fp.blackboard_posts(core)] == ["only"]


def test_state_symlink_fifo_version2_and_foreign_version(tmp_path):
    target = tmp_path / "real.json"
    target.write_text(json.dumps({
        "version": 1, "seen": ["find0000001"],
        "boots": {"avrone": "a", "cyclekernal": "c"},
    }))
    link = tmp_path / "link.json"
    link.symlink_to(target)
    started = time.monotonic()
    watch = fp.FusionWatch(link)
    assert time.monotonic() - started < 1.0
    assert watch.seen == set() and watch.state_errors >= 1
    assert watch._save() is False
    assert link.is_symlink()
    assert "find0000001" in json.loads(target.read_text())["seen"]

    fifo = tmp_path / "state.fifo"
    os.mkfifo(fifo)
    started = time.monotonic()
    fifo_watch = fp.FusionWatch(fifo)
    assert time.monotonic() - started < 1.0
    assert fifo_watch.state_errors >= 1 and fifo_watch._save() is False
    assert fifo.is_fifo()

    version2 = tmp_path / "v2.json"
    version2.write_text(json.dumps({
        "version": 2, "seen": ["find0000001"], "cursor": "find0000001",
        "boots": {"avrone": "board-a", "cyclekernal": "boot-a"},
    }))
    loaded = fp.FusionWatch(version2)
    assert loaded.seen == {"find0000001"} and loaded.boots["cyclekernal"] == "boot-a"
    assert loaded._save() is True
    saved = json.loads(version2.read_text())
    assert saved["version"] == 2 and saved["cursor"] == "find0000001"
    assert saved["seen"] == ["find0000001"]

    foreign = tmp_path / "v9.json"
    original = json.dumps({"version": 9, "seen": ["keep00000001"], "cursor": "keep"})
    foreign.write_text(original)
    foreign_watch = fp.FusionWatch(foreign)
    assert foreign_watch.seen == set() and foreign_watch._save() is False
    assert foreign.read_text() == original

    huge = tmp_path / "huge.json"
    huge.write_bytes(b"{" + b"x" * (fp._STATE_CAP + 10))
    before = huge.stat().st_size
    huge_watch = fp.FusionWatch(huge)
    assert huge_watch.state_errors >= 1 and huge_watch._save() is False
    assert huge.stat().st_size == before


def test_quote_and_emoji_text_stays_under_the_byte_cap():
    body = {
        "id": "clip0000002", "source": "avrone", "kind": "finding", "run_id": "r",
        "payload": {"id": "clip0000002", "text": ('"\\' * 3000) + ("😀" * 800)},
        "ts": 1,
    }
    raw = fp.encode_bounded(body)
    assert raw is not None and len(raw) <= 4096
    text = json.loads(raw.decode())["payload"]["text"]
    assert text.endswith("...")
    assert fp.encode_bounded({"id": "x", "payload": {"blob": "y" * 5000}}) is None


def test_unsafe_token_file_url_and_timeout_clamp_do_not_dial(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("do-not-read")
    hopped = fp._exchange("GET", secret.as_uri())
    assert hopped["error"] == "down"
    assert secret.read_text() == "do-not-read"
    injected = fp._exchange("GET", "http://127.0.0.1:9/health", token="secret\r\nX-Evil: 1")
    assert injected["error"] == "auth"
    spaced = fp._exchange("GET", "http://127.0.0.1:9/health bad")
    assert spaced["error"] == "down"
    with _Running() as peer:
        url = f"http://{peer.server_address[0]}:{peer.server_address[1]}/v1/events"
        watch = fp.FusionWatch()
        event = fp.event_from_post(POST)
        blocked = watch.submit(url, "secret\r\nX-Evil: 1", event, "find0000001")
        assert blocked["error"] == "auth" and blocked["remembered"] is False
        assert peer.posts_hits == 0
        assert "find0000001" not in watch.seen
        peer.posts = [dict(POST)]
        nan = watch.watch_once(**{**_urls(f"http://{peer.server_address[0]}:{peer.server_address[1]}"),
                                  "timeout": float("nan")})
        assert nan["forwarded"] == 1 and peer.posts_hits == 1


def test_retryable_statuses_and_avrone_boot_keeps_findings(tmp_path):
    with _Running() as peer:
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        url = base + "/v1/events"
        watch = fp.FusionWatch(tmp_path / "retry.json")
        event = fp.event_from_post(POST)
        for mode, error in (("404", "missing"), ("429", "retry"), ("text", "malformed")):
            peer.mode = mode
            result = watch.submit(url, "secret", {**event, "id": "retry0000001"}, "retry0000001")
            assert result["error"] == error and result["remembered"] is False
            assert "retry0000001" not in watch.seen
        peer.mode = "ok"
        assert watch.submit(url, "secret", {**event, "id": "retry0000001"}, "retry0000001")["ok"] is True

        peer.posts = [dict(POST, id="find0000002", text="stays")]
        peer.board_boot = "board-a"
        peer.boot = "boot-a"
        cycle = fp.FusionWatch(tmp_path / "boot.json")
        urls = _urls(base)
        assert cycle.watch_once(**urls)["forwarded"] == 1
        sent = peer.posts_hits
        cycle.seen.add("ck-mood-zzzzzzzz")
        peer.board_boot = "board-b"
        peer.stored.clear()
        report = cycle.watch_once(**urls)
        assert report["restart"] is True
        assert "find0000002" in cycle.seen
        assert "ck-mood-zzzzzzzz" not in cycle.seen
        assert peer.posts_hits == sent  # the finding was not resent

        # A float boot, a bool, and a boot with a newline are not a restart.
        peer.boot = 1.5
        assert cycle.watch_once(**urls)["restart"] is False
        peer.boot = True
        assert cycle.watch_once(**urls)["restart"] is False
        peer.boot = "boot-a\nextra"
        assert cycle.watch_once(**urls)["restart"] is False
        assert peer.posts_hits == sent


def test_forward_cap_and_echo_variants(monkeypatch):
    monkeypatch.setattr(fp, "MAX_FORWARD", 1)
    with _Running() as peer:
        second = dict(POST, id="find0000002", text="second")
        peer.posts = [dict(POST), second, {
            "id": "case00000001", "run_id": "run-1", "agent": "CycleKernal:Health",
            "role": "agent", "kind": "finding", "text": "mirror", "refs": [], "ts": 2.0,
            "origin_node": "local",
        }, {
            "id": "text00000001", "run_id": "run-1", "agent": "scout", "role": "agent",
            "kind": "finding", "text": '{ "agent" : "cyclekernal:mood" }', "refs": [],
            "ts": 3.0, "origin_node": "local",
        }]
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        watch = fp.FusionWatch()
        first = watch.watch_once(**_urls(base))
        assert first["forwarded"] == 1 and first["skipped"] >= 1
        assert list(peer.stored) == ["find0000001"]
        second_cycle = watch.watch_once(**_urls(base))
        assert second_cycle["forwarded"] == 1
        assert set(peer.stored) == {"find0000001", "find0000002"}


def test_watch_route_clamps_timeout_and_get_does_not_dial(client, monkeypatch):
    monkeypatch.setenv("FUSION_TIMEOUT", "inf")
    assert server._fusion_timeout() == 5.0
    monkeypatch.setenv("FUSION_TIMEOUT", "nan")
    assert server._fusion_timeout() == 5.0
    monkeypatch.setenv("FUSION_TIMEOUT", "-4")
    assert server._fusion_timeout() == 0.05
    monkeypatch.setenv("FUSION_TIMEOUT", "999")
    assert server._fusion_timeout() == 30.0
    monkeypatch.setenv("FUSION_TIMEOUT", "junk")
    assert server._fusion_timeout() == 5.0

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    hanging = f"http://127.0.0.1:{port}/api/blackboard"
    monkeypatch.setenv("FUSION_BOARD_URL", "   ")
    monkeypatch.setenv("FUSION_EVENTS_URL", hanging)
    monkeypatch.setenv("FUSION_HEALTH_URL", hanging)
    try:
        started = time.monotonic()
        unset = client.post("/api/fusion/watch", headers=AUTH_H)
        assert time.monotonic() - started < 0.5
        assert unset.json() == {"watched": False, "reason": "unset"}
        started = time.monotonic()
        status = client.get("/api/fusion", headers=AUTH_H)
        assert time.monotonic() - started < 0.5
        assert status.status_code == 200 and status.json()["boot"] == server.boot_id()
    finally:
        sock.close()


def test_oversized_response_is_not_remembered():
    with _Running() as peer:
        base = f"http://{peer.server_address[0]}:{peer.server_address[1]}"
        watch = fp.FusionWatch()
        event = fp.event_from_post(POST)
        short = watch.submit(base + "/v1/events", "secret", event, "find0000001", max_read=8)
        assert short["error"] == "truncated" and short["remembered"] is False
        assert "find0000001" not in watch.seen
        assert peer.posts_hits == 1
        again = watch.submit(base + "/v1/events", "secret", event, "find0000001")
        assert again["ok"] is True and peer.posts_hits == 2
