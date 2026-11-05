"""Round 71: the fleet gateway under a flood of unauthenticated requests.

1. Every pre-HMAC refusal (no token, malformed ts/nonce, bad signature) wrote a full audit event to
   the codex log, which never rotates, and fanned it out to every dashboard: 5,000 junk requests
   left 1.16 MB of identical lines (1M would be ~230 MB). They are now audited up to
   AVRONE_FLEET_UNAUTH_AUDIT_PER_MIN (60) a minute, then counted, and one
   fleet.refused.suppressed event reports the count per code. Authenticated refusals (stale,
   replay, policy) are still audited every time.
2. During token rotation verify() hashed the whole body twice (16 MB: 21 ms instead of 10).
3. /api/fleet/* bodies above 256 KB are hashed and parsed in a worker thread, not on the loop.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import fleet as fleet_mod
from avrone.codex import Codex
from avrone.fleet import FleetManager, GatewayRefused, sign

TOK = "r71-" + "t" * 40
PATH = "/api/fleet/message"


@pytest.fixture
def fm(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", TOK)
    monkeypatch.delenv("AVRONE_FLEET_TOKEN_PREV", raising=False)
    monkeypatch.delenv("AVRONE_FLEET_ALLOW", raising=False)
    monkeypatch.delenv("AVRONE_FLEET_UNAUTH_AUDIT_PER_MIN", raising=False)
    cx = Codex(tmp_path)
    return FleetManager(SimpleNamespace(codex=cx), tmp_path)


def _events(fm, kind=None):
    f = fm.core.codex.log_dir / "fleet.jsonl"
    if not f.exists():
        return []
    out = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
    return [e for e in out if kind is None or e["kind"] == kind]


def _junk(fm, n, sig="0" * 64):
    hdr = {"x-avrone-ts": str(int(time.time())), "x-avrone-nonce": "abc", "x-avrone-sig": sig}
    for _ in range(n):
        with pytest.raises(GatewayRefused):
            fm.dispatch_gateway("message", "POST", PATH, b"{}", hdr)


def test_bad_signature_flood_is_rate_limited(fm):
    _junk(fm, 2000)
    assert len(_events(fm, "fleet.refused")) == 60  # was 2000
    assert fm.refused_unauth == 2000
    assert (fm.core.codex.log_dir / "fleet.jsonl").stat().st_size < 30_000  # was ~460 KB


def test_suppressed_summary_on_window_roll_and_flush(fm, monkeypatch):
    _junk(fm, 70)
    hdr = {"x-avrone-ts": "x", "x-avrone-nonce": "abc", "x-avrone-sig": "0" * 64}
    for _ in range(5):
        with pytest.raises(GatewayRefused):
            fm.dispatch_gateway("message", "POST", PATH, b"{}", hdr)
    assert not _events(fm, "fleet.refused.suppressed")
    fm._unauth_start -= fleet_mod.UNAUTH_AUDIT_WINDOW_S + 1  # the minute is over
    _junk(fm, 1)
    (s,) = _events(fm, "fleet.refused.suppressed")
    assert s["data"]["suppressed"] == 15 and s["data"]["codes"] == {"bad_signature": 10, "bad_timestamp": 5}
    assert len(_events(fm, "fleet.refused")) == 61  # new window audits again
    _junk(fm, 70)
    fm.flush_unauth_audit()
    assert len(_events(fm, "fleet.refused.suppressed")) == 2
    fm.flush_unauth_audit()  # nothing pending: no empty summary
    assert len(_events(fm, "fleet.refused.suppressed")) == 2


def test_authenticated_refusals_are_always_audited(fm):
    body = b"{}"
    for i in range(80):
        h = sign(TOK, "POST", PATH, body, ts=str(int(time.time()) - 10_000), nonce=f"n{i}")
        with pytest.raises(GatewayRefused) as e:
            fm.dispatch_gateway("message", "POST", PATH, body, h)
        assert e.value.code == "stale"
    assert len(_events(fm, "fleet.refused")) == 80


def test_limit_env_and_zero(fm, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_UNAUTH_AUDIT_PER_MIN", "0")
    _junk(fm, 10)
    assert not _events(fm, "fleet.refused")
    fm.flush_unauth_audit()
    (s,) = _events(fm, "fleet.refused.suppressed")
    assert s["data"]["suppressed"] == 10


def test_valid_requests_unaffected_by_flood(fm):
    _junk(fm, 500)
    body = json.dumps({"from": "peer1", "text": "hi"}).encode()
    p = fm.dispatch_gateway("message", "POST", PATH, body, sign(TOK, "POST", PATH, body))
    assert p["text"] == "hi"
    assert _events(fm, "fleet.dispatch")


def test_rotation_hashes_body_once(fm, monkeypatch):
    monkeypatch.setattr(fm, "prev_token", "r71-old-" + "o" * 40)
    calls = {"n": 0}
    real = hashlib.sha256

    def counting(data=b"", *a, **k):
        if len(data) > 1000:
            calls["n"] += 1
        return real(data, *a, **k)
    body = b'{"x":"' + b"a" * 100_000 + b'"}'
    monkeypatch.setattr(fleet_mod.hashlib, "sha256", counting)
    hdr = {"x-avrone-ts": str(int(time.time())), "x-avrone-nonce": "abc", "x-avrone-sig": "0" * 64}
    with pytest.raises(GatewayRefused):
        fm.verify("POST", PATH, body, hdr)
    assert calls["n"] == 1  # was 2
    h = sign(fm.prev_token, "POST", PATH, body)
    calls["n"] = 0
    fm.verify("POST", PATH, body, h)  # old-token peers still accepted
    assert calls["n"] == 1


def test_big_bodies_verified_off_loop(monkeypatch, tmp_path):
    import asyncio
    from avrone import server
    seen = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        seen.append(getattr(fn, "__name__", str(fn)))
        return await real(fn, *a, **k)
    monkeypatch.setattr(server.asyncio, "to_thread", spy)

    class F:
        def dispatch_gateway(self, *a):
            return {"ok": 1}

        def verify(self, *a):
            return None
    monkeypatch.setattr(server, "_fleet", lambda: F())

    class Req:
        method = "POST"
        url = SimpleNamespace(path=PATH)
        headers = {}

        def __init__(self, body):
            self._b = body

        async def body(self):
            return self._b
    big = b'{"a":"' + b"x" * (server.OFF_LOOP_BODY_BYTES + 10) + b'"}'
    assert asyncio.run(server._gateway(Req(big), "message")) == {"ok": 1}
    assert asyncio.run(server._verified(Req(big)))["a"]
    assert "dispatch_gateway" in seen and "verify" in seen and "loads" in seen
    seen.clear()
    asyncio.run(server._gateway(Req(b"{}"), "message"))
    asyncio.run(server._verified(Req(b"{}")))
    assert seen == []  # small bodies stay on the loop (no thread hop per heartbeat)
