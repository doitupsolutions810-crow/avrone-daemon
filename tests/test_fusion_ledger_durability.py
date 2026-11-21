"""Crash, clock, peer, and disk edges for the delivery ledger and FusionWatch.

A torn ledger file is kept. Two processes sharing one file do not drop keys.
A clock that jumps does not fail a pending key or spin past the 64-try cap.
A slow, partial, or oversized peer response is an error result, not a crash,
and it does not open the breaker when the peer did answer.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from avrone.fusion_deliver import (
    MAX_RESPONSE_BYTES,
    DeliverConfig,
    Ledger,
    ManualClock,
    httpx_transport,
)
from avrone.fusion_peer import FusionWatch


def _event(eid: str, text: str | None = None) -> dict:
    return {
        "id": eid,
        "source": "avrone",
        "kind": "evidence",
        "run_id": "edge",
        "payload": {"text": text or eid},
        "ts": 1,
    }


def _cfg(**kw) -> DeliverConfig:
    base = dict(
        base="http://127.0.0.1:8090",
        events_path="/v1/events",
        health_path="/health",
        token="",
        interval=0.05,
        timeout=0.2,
        deadline=0,
        backoff_base=0.05,
        backoff_max=0.2,
        breaker_threshold=1,
        breaker_cooldown=30,
        max_body=4096,
    )
    base.update(kw)
    return DeliverConfig(**base)


class _JumpBack(ManualClock):
    """A retry moves the clock backward instead of forward."""

    def sleep(self, seconds: float) -> None:
        seconds = 0.0 if seconds < 0 else float(seconds)
        self.sleeps.append(seconds)
        self.t -= seconds if seconds else 1.0


class _JumpForward(ManualClock):
    """One sleep jumps far past any deadline."""

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        self.t += 10_000


def _rss() -> int:
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def run_soak(seconds: float) -> dict:
    """Deliver a fixed set, then keep admitting new keys for ``seconds``.

    Every admitted key is delivered once. RSS growth has to stay with the
    ledger, not with the number of retries.
    """
    import tempfile

    started = time.time()
    rss0 = _rss()
    folder = Path(tempfile.mkdtemp(prefix="fusion-soak-"))
    path = folder / "ledger.json"
    clock = ManualClock()

    class Tape:
        def __init__(self) -> None:
            self.posts = 0

        def __call__(self, method, url, body, headers, timeout):
            from avrone.fusion_deliver import HttpResult
            if method == "GET":
                return HttpResult(200, {"boot": "soak"}, None)
            self.posts += 1
            return HttpResult(200, {"accepted": True, "duplicate": False}, None)

    tape = Tape()
    ledger = Ledger(path, clock)
    for i in range(50):
        assert ledger.offer(_event(f"warm-{i:04d}")) == "accepted"
    assert ledger.pump(tape, _cfg(deadline=0), max_cycles=4)["D"] == 50
    admitted = 50
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        eid = f"live-{admitted:06d}"
        assert ledger.offer(_event(eid)) == "accepted"
        admitted += 1
        if admitted % 25 == 0:
            report = ledger.pump(tape, _cfg(deadline=0), max_cycles=2)
            assert report["P"] == 0 and report["F"] == 0
            assert report["A"] == report["D"] + report["P"] + report["F"]
    ledger.pump(tape, _cfg(deadline=0), max_cycles=4)
    reloaded = Ledger(path, ManualClock())
    accounts = reloaded.accounts()
    rss1 = _rss()
    return {
        "seconds": round(time.time() - started, 3),
        "offered": admitted,
        "delivered": accounts["D"],
        "pending": accounts["P"],
        "failed": accounts["F"],
        "identity": accounts["A"] == accounts["D"] + accounts["P"] + accounts["F"],
        "posts": tape.posts,
        "rss_kib_start": rss0,
        "rss_kib_end": rss1,
        "rss_kib_delta": rss1 - rss0,
        "file_bytes": path.stat().st_size,
    }


def test_torn_file_is_not_replaced(tmp_path):
    path = tmp_path / "torn.json"
    path.write_text("{", encoding="utf-8")
    broken = Ledger(path, ManualClock())
    assert broken.state_errors == 1 and broken.accounts()["A"] == 0
    with pytest.raises(OSError):
        broken.offer(_event("after"))
    assert path.read_text(encoding="utf-8") == "{"


def test_kill_during_append_replays_without_a_second_copy(tmp_path):
    path = tmp_path / "live.json"
    assert Ledger(path, ManualClock()).offer(_event("seed")) == "accepted"
    code = r"""
import sys
from pathlib import Path
from avrone.fusion_deliver import Ledger, ManualClock
path = Path(sys.argv[1])
ledger = Ledger(path, ManualClock())
n = 0
while True:
    n += 1
    ledger.offer({
        "id": f"k{n:05d}", "source": "avrone", "kind": "evidence",
        "run_id": "edge", "payload": {"text": "x"}, "ts": 1,
    })
"""
    proc = subprocess.Popen([sys.executable, "-c", code, str(path)])
    time.sleep(0.4)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=5)
    raw = path.read_bytes()
    data = json.loads(raw)
    assert data["version"] == 2
    again = Ledger(path, ManualClock())
    accounts = again.accounts()
    assert accounts["A"] == accounts["D"] + accounts["P"] + accounts["F"]
    assert again.offer(_event("seed")) == "duplicate"
    ids = {row["id"] for row in again.entries.values()}
    assert "seed" in ids
    # A second process offering the same ids does not add a second row.
    assert Ledger(path, ManualClock()).offer(_event("seed")) == "duplicate"


def test_two_processes_share_one_ledger_without_losing_keys(tmp_path):
    path = tmp_path / "shared.json"
    code = r"""
import sys
from pathlib import Path
from avrone.fusion_deliver import Ledger, ManualClock
path, eid = Path(sys.argv[1]), sys.argv[2]
event = {"id": eid, "source": "avrone", "kind": "evidence", "run_id": "edge",
         "payload": {"text": eid}, "ts": 1}
raise SystemExit(0 if Ledger(path, ManualClock()).offer(event) == "accepted" else 1)
"""
    procs = [
        subprocess.Popen([sys.executable, "-c", code, str(path), f"p{i:02d}"])
        for i in range(8)
    ]
    for proc in procs:
        assert proc.wait(timeout=20) == 0
    loaded = Ledger(path, ManualClock())
    assert loaded.accounts()["A"] == 8
    assert loaded.accounts()["P"] == 8
    assert {row["id"] for row in loaded.entries.values()} == {f"p{i:02d}" for i in range(8)}


def test_clock_jump_does_not_fail_the_key_or_spin(tmp_path):
    back = _JumpBack()
    ledger = Ledger(tmp_path / "back.json", back)

    def retry_after(method, url, body, headers, timeout):
        from avrone.fusion_deliver import HttpResult
        if method == "GET":
            return HttpResult(200, {"boot": "b"}, None)
        return HttpResult(429, {"detail": "later"}, None, "0.05")

    ledger.offer(_event("back"))
    report = ledger.pump(retry_after, _cfg(deadline=0, breaker_threshold=5), max_cycles=2)
    assert report["P"] == 1 and report["F"] == 0
    assert report["calls"] == 64
    assert back.t < 0

    forward = _JumpForward()
    ledger2 = Ledger(tmp_path / "fwd.json", forward)

    def once_then_ok(method, url, body, headers, timeout):
        from avrone.fusion_deliver import HttpResult
        if method == "GET":
            return HttpResult(200, {"boot": "b"}, None)
        if not getattr(once_then_ok, "seen", False):
            once_then_ok.seen = True
            return HttpResult(429, {"detail": "slow"}, None, "0.05")
        return HttpResult(200, {"accepted": True}, None)

    ledger2.offer(_event("fwd"))
    report = ledger2.pump(once_then_ok, _cfg(deadline=1, breaker_threshold=3), max_cycles=4)
    # The jump ends this cycle. The key stays pending. It is not a failure.
    assert report["P"] == 1 and report["F"] == 0
    assert forward.t >= 10_000
    report = ledger2.pump(once_then_ok, _cfg(deadline=1, breaker_threshold=3), max_cycles=2)
    assert report["D"] == 1 and report["P"] == 0 and report["F"] == 0


def test_peer_slow_partial_oversized_and_chunked(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_POST(self):
            mode = self.server.mode
            if mode == "slow":
                time.sleep(1.5)
                body = b'{"accepted":true}'
                self._full(200, body)
                return
            if mode == "partial":
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"acce')
                self.close_connection = True
                return
            if mode == "oversized":
                body = b'{"accepted":true,"pad":"' + b"x" * (MAX_RESPONSE_BYTES + 100) + b'"}'
                self._full(200, body)
                return
            if mode == "chunked":
                body = b'{"accepted":true,"duplicate":false}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n")
                return
            self._full(200, b'{"accepted":true}')

        def _full(self, code, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.mode = "chunked"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    url = f"http://{host}:{port}/v1/events"
    try:
        chunked = httpx_transport("POST", url, b"{}", {"Content-Type": "application/json"}, 2)
        assert chunked.status == 200 and chunked.error is None
        assert chunked.body["accepted"] is True
        server.mode = "slow"
        slow = httpx_transport("POST", url, b"{}", {}, 0.2)
        assert slow.error == "timeout"
        server.mode = "partial"
        partial = httpx_transport("POST", url, b"{}", {}, 2)
        assert partial.error in {"refused", "timeout"}
        server.mode = "oversized"
        big = httpx_transport("POST", url, b"{}", {}, 2)
        assert big.error == "truncated"
    finally:
        server.shutdown()
        thread.join(timeout=3)

    clock = ManualClock()
    ledger = Ledger(tmp_path / "big.json", clock)
    ledger.offer(_event("big"))

    def oversized(method, url, body, headers, timeout):
        from avrone.fusion_deliver import HttpResult
        if method == "GET":
            return HttpResult(200, {"boot": "b"}, None)
        return HttpResult(200, None, "truncated")

    report = ledger.pump(oversized, _cfg(breaker_threshold=1), max_cycles=1)
    assert report["P"] == 1 and report["F"] == 0
    assert ledger.breaker_for("http://127.0.0.1:8090").fails == 0


def test_dns_failure_and_proxy_env_do_not_steal_the_call(tmp_path, monkeypatch):
    missed = httpx_transport("GET", "http://no-such-host.invalid/health", None, {}, 1.0)
    assert missed.error in {"refused", "timeout"}
    seen = {"n": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            seen["n"] += 1
            body = b'{"boot":"local"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:9")
        result = httpx_transport("GET", f"http://{host}:{port}/health", None, {}, 2)
        assert result.status == 200 and result.body["boot"] == "local"
        assert seen["n"] == 1
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_read_only_dir_and_disk_full_do_not_drop_or_claim_a_save(tmp_path, monkeypatch):
    parent = tmp_path / "ro"
    parent.mkdir()
    path = parent / "ledger.json"
    assert Ledger(path, ManualClock()).offer(_event("kept")) == "accepted"
    if os.geteuid() != 0:
        parent.chmod(0o555)
        try:
            with pytest.raises(OSError):
                Ledger(path, ManualClock()).offer(_event("new"))
        finally:
            parent.chmod(0o755)
    else:
        # Root ignores the directory mode. The same OSError is what a full disk
        # and a non-root read-only directory raise from the lock open.
        real_open = os.open

        def refuse_lock(file, flags, mode=0o777, *, dir_fd=None):
            if str(file).endswith(".lock"):
                raise OSError(30, "Read-only file system")
            return real_open(file, flags, mode)

        monkeypatch.setattr(os, "open", refuse_lock)
        with pytest.raises(OSError):
            Ledger(path, ManualClock()).offer(_event("new"))
        monkeypatch.undo()
    loaded = Ledger(path, ManualClock())
    assert loaded.accounts()["A"] == 1
    assert loaded.offer(_event("kept")) == "duplicate"
    assert loaded.offer(_event("new")) == "accepted"

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("avrone.safeio.atomic_write", full)
    with pytest.raises(OSError):
        loaded.offer(_event("nospace"))
    monkeypatch.undo()
    again = Ledger(path, ManualClock())
    assert "nospace" not in {row["id"] for row in again.entries.values()}
    assert again.offer(_event("kept")) == "duplicate"


def test_watch_flapping_peer_retries_then_forwards_once(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            self.server.gets += 1
            if self.server.fail_gets > 0:
                self.server.fail_gets -= 1
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path.startswith("/health"):
                body = json.dumps({"boot": self.server.boot}).encode()
            else:
                body = json.dumps({
                    "posts": [self.server.post],
                    "count": 1,
                    "boot": self.server.board_boot,
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.server.posts += 1
            if self.server.fail_posts > 0:
                self.server.fail_posts -= 1
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b'{"accepted":true,"duplicate":false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.fail_gets = 2
    server.fail_posts = 1
    server.gets = 0
    server.posts = 0
    server.boot = "boot-a"
    server.board_boot = "board-a"
    server.post = {
        "id": "flap00000001",
        "text": "still here",
        "kind": "finding",
        "run_id": "run",
        "ts": 1,
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    watch = FusionWatch(tmp_path / "watch.json")
    urls = {
        "board_url": base + "/api/blackboard",
        "events_url": base + "/v1/events",
        "health_url": base + "/health",
        "board_token": "secret",
        "events_token": "secret",
        "timeout": 2.0,
        "breaker_threshold": 2,
        "breaker_cooldown": 0.05,
    }
    try:
        down = watch.watch_once(**urls)
        assert down["forwarded"] == 0 and down["errors"] >= 1
        assert "flap00000001" not in watch.seen
        time.sleep(0.1)
        up = watch.watch_once(**urls)
        # The post may land on this cycle or the next, after the one 500 on POST.
        if up["forwarded"] != 1:
            up = watch.watch_once(**urls)
        assert up["forwarded"] == 1
        assert watch.seen == {"flap00000001"}
        again = watch.watch_once(**urls)
        assert again["forwarded"] == 0
        hits = server.posts
        server.board_boot = "board-b"
        flipped = watch.watch_once(**urls)
        assert flipped["restart"] is True
        # Avrone's own post is not a mirror, so a board-boot change may resend it.
        assert server.posts >= hits
    finally:
        server.shutdown()
        thread.join(timeout=3)


def test_short_soak_keeps_identity_and_bounds_rss():
    report = run_soak(2.0)
    assert report["identity"] is True
    assert report["delivered"] == report["offered"]
    assert report["pending"] == 0 and report["failed"] == 0
    assert report["posts"] == report["offered"]
    # The ledger is the growth. A leak would be many times the file.
    assert report["rss_kib_delta"] < (report["file_bytes"] // 1024) + 64_000
