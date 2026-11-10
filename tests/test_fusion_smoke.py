"""Loopback sockets for the delivery client. No shared stack, no fixed port.

One real peer returns two 429s and then 200. The measured wall time has to
cover the backoff and stay well under the virtual 135.5 s schedule. A second
bind on ::1, when the kernel allows it, accepts the same event.
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from avrone.fusion_deliver import DeliverConfig, Ledger, httpx_transport
from avrone.fusion_loopback import origin_key


class _Peer(BaseHTTPRequestHandler):
    seen: list = []
    posts = 0

    def do_GET(self):
        body = b'{"status":"ghost-online","boot":"socket-boot"}'
        self._send(200, body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        _Peer.posts += 1
        _Peer.seen.append((self.headers.get("Idempotency-Key"), self.headers.get("X-Admin-Token"), raw))
        if _Peer.posts <= 2:
            self._send(429, b'{"detail":"slow down"}')
            return
        self._send(200, b'{"accepted":true,"duplicate":false,"id":"live1"}')

    def _send(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return


def _serve(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _event():
    return {
        "id": "live1", "source": "avrone", "kind": "finding", "run_id": "smoke",
        "payload": {"text": "socket", "heat": 1.0, "cone": 0.5, "beta_t": 0.25},
        "ts": 10,
    }


def test_real_loopback_backs_off_on_429_then_accepts(tmp_path):
    _Peer.seen = []
    _Peer.posts = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Peer)
    port = server.server_address[1]
    _serve(server)
    try:
        ledger = Ledger(tmp_path / "live.json")
        assert ledger.offer(_event()) == "accepted"
        config = DeliverConfig(
            base=f"http://127.0.0.1:{port}", events_path="/v1/events", health_path="/health",
            token="socket-token", timeout=2, deadline=0, breaker_threshold=1, breaker_cooldown=30,
            backoff_base=0.05, backoff_max=0.2, interval=0.05,
        )
        wall0 = time.perf_counter()
        report = ledger.pump(httpx_transport, config, max_cycles=2)
        wall = time.perf_counter() - wall0
    finally:
        server.shutdown()
        server.server_close()
    assert report["A"] == 1 and report["D"] == 1 and report["P"] == 0 and report["F"] == 0
    assert _Peer.posts == 3
    assert [key for key, _, _ in _Peer.seen] == ["live1", "live1", "live1"]
    assert {token for _, token, _ in _Peer.seen} == {"socket-token"}
    # Two backoff sleeps: 0.05 s then 0.10 s. Wall time includes them and not a spin.
    assert wall >= 0.15
    assert wall < 3.0
    again = Ledger(tmp_path / "live.json")
    assert again.accounts()["D"] == 1


def test_ipv6_loopback_accepts_the_same_contract(tmp_path):
    class V6(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    class Once(_Peer):
        posts = 0
        seen = []

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            Once.posts += 1
            Once.seen.append(raw)
            self._send(200, b'{"accepted":true,"duplicate":false,"id":"v6"}')

    try:
        server = V6(("::1", 0), Once)
    except OSError:
        pytest.skip("IPv6 loopback is not available")
    port = server.server_address[1]
    _serve(server)
    try:
        ledger = Ledger(tmp_path / "v6.json")
        ledger.offer({
            "id": "v6", "source": "greatscale", "kind": "evidence", "run_id": "smoke",
            "payload": {"text": "v6", "heat": 0.1, "cone": 0.2, "beta_t": 0.3}, "ts": 2,
        })
        config = DeliverConfig(
            base=f"http://[::1]:{port}", events_path="/events", health_path="/health",
            token="v6", timeout=2, deadline=2, breaker_threshold=1, breaker_cooldown=5,
            backoff_base=0.05, backoff_max=0.1, interval=0.05,
        )
        assert origin_key(config.base) == origin_key(f"http://127.0.0.1:{port}")
        report = ledger.pump(httpx_transport, config, max_cycles=2)
    finally:
        server.shutdown()
        server.server_close()
    assert report["D"] == 1 and Once.posts == 1
    body = json.loads(Once.seen[0])
    assert body["source"] == "greatscale" and body["id"] == "v6"
    assert len(Once.seen[0]) <= 4096
