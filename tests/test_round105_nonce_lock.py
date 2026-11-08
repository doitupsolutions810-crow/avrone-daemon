"""Round 105: the fleet nonce lock has a deadline and fails closed.

v0.97: FleetManager.verify() took a blocking flock on fleet_nonces.json.lock while holding the
manager's thread lock. A peer process (a second uvicorn worker, an overlapping restart) that hung
while holding that lock froze every signed fleet request on the node, and because self._lock is
shared, register/pick too. Now the flock waits AVRONE_FLEET_NONCE_LOCK_S (default 5 s) and then
refuses with code "busy" (HTTP 503 + Retry-After). It never checks the nonce without merging the
peer's cache first, which could accept a replay.
"""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import avrone.fleet as fleet_mod
from avrone.fleet import FleetManager, GatewayRefused, sign

TOKEN = "round105-fleet-token-" + "q" * 32
PATH = "/api/fleet/run"


@pytest.fixture
def d(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", TOKEN)
    monkeypatch.delenv("AVRONE_FLEET_ALLOW", raising=False)
    monkeypatch.setenv("AVRONE_FLEET_NONCE_LOCK_S", "0.3")
    return tmp_path


def _fm(d: Path) -> FleetManager:
    core = SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None),
                           settings=SimpleNamespace(data_dir=str(d)))
    return FleetManager(core, data_dir=d)


def _req(i):
    body = json.dumps({"i": i}).encode()
    return body, sign(TOKEN, "POST", PATH, body)


class Holder:
    def __init__(self, path: Path):
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def release(self):
        os.close(self.fd)


def _bounded(fn, limit=5.0):
    box = {}

    def go():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(limit)
    return not t.is_alive(), box


def _lock_path(fm):
    return Path(str(fm._nonce_path) + ".lock")


def test_verify_refuses_busy_not_hang(d):
    fm = _fm(d)
    h = Holder(_lock_path(fm))
    body, hd = _req(1)
    try:
        done, box = _bounded(lambda: fm.verify("POST", PATH, body, hd))
        assert done, "verify blocked forever on a held nonce lock"
        e = box.get("e")
        assert isinstance(e, GatewayRefused) and e.code == "busy" and not e.auth
    finally:
        h.release()
    fm.verify("POST", PATH, body, hd)  # same request is accepted once the lock frees (never seen)
    with pytest.raises(GatewayRefused) as r:
        fm.verify("POST", PATH, body, hd)
    assert r.value.code == "replay"


def test_busy_does_not_record_nonce(d):
    fm = _fm(d)
    h = Holder(_lock_path(fm))
    body, hd = _req(2)
    try:
        _bounded(lambda: fm.verify("POST", PATH, body, hd))
        assert hd["x-avrone-nonce"] not in fm._nonces
    finally:
        h.release()


def test_manager_lock_not_pinned(d):
    fm = _fm(d)
    h = Holder(_lock_path(fm))
    body, hd = _req(3)
    try:
        t = threading.Thread(target=lambda: _bounded(lambda: fm.verify("POST", PATH, body, hd)), daemon=True)
        t.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        got = fm._lock.acquire(timeout=3)
        assert got, "manager thread lock held forever behind the nonce flock"
        fm._lock.release()
        assert time.monotonic() - t0 < 2
    finally:
        h.release()


def test_dispatch_gateway_audits_busy(d):
    fm = _fm(d)
    seen = []
    fm._audit = lambda ev, **kw: seen.append((ev, kw.get("code")))
    h = Holder(_lock_path(fm))
    body, hd = _req(4)
    try:
        done, box = _bounded(lambda: fm.dispatch_gateway("spawn", "POST", PATH, body, hd))
        assert done and isinstance(box.get("e"), GatewayRefused)
    finally:
        h.release()
    assert ("fleet.refused", "busy") in seen


def test_env_junk(monkeypatch):
    for v, want in (("", 5.0), ("x", 5.0), ("nan", 5.0), ("-3", 5.0), ("999", 60.0), ("0", 0.0), ("1.5", 1.5)):
        monkeypatch.setenv("AVRONE_FLEET_NONCE_LOCK_S", v)
        assert fleet_mod.nonce_lock_wait_s() == want


class _Req:
    method = "POST"

    def __init__(self, body, headers):
        self._b, self.headers = body, headers
        self.url = SimpleNamespace(path=PATH)

    async def body(self):
        return self._b


def test_gateway_inline_never_waits_then_retries_off_loop(d, monkeypatch):
    """Small bodies are verified on the event loop: a held nonce lock must not stall it."""
    import asyncio
    from avrone import server
    fm = _fm(d)
    monkeypatch.setattr(server, "_fleet", lambda: fm)
    monkeypatch.setenv("AVRONE_FLEET_NONCE_LOCK_S", "0.5")
    h = Holder(_lock_path(fm))
    body, hd = _req(5)

    async def main():
        ticks = []

        async def ticker():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)
        tk = asyncio.create_task(ticker())
        loop = asyncio.get_running_loop()
        loop.call_later(0.2, h.release)  # the peer lets go while the request waits in a thread
        t0 = time.monotonic()
        from fastapi import HTTPException
        try:
            out = await asyncio.wait_for(server._gateway(_Req(body, hd), "spawn"), 4)
        except HTTPException as e:  # verified, then the policy layer decides (403 here): not busy
            out = e.status_code
        finally:
            tk.cancel()
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        return out, max(gaps or [0]), time.monotonic() - t0
    try:
        out, gap, el = asyncio.run(main())
    finally:
        try:
            h.release()
        except OSError:
            pass
    assert out != 503 and out != 401, out  # the retry waited off-loop and the signature verified
    assert gap < 0.15, f"event loop stalled {gap:.3f}s on the nonce lock"


def test_gateway_busy_is_503(d, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from avrone import server
    fm = _fm(d)
    monkeypatch.setattr(server, "_fleet", lambda: fm)
    h = Holder(_lock_path(fm))
    body, hd = _req(6)
    try:
        with pytest.raises(HTTPException) as r:
            asyncio.run(asyncio.wait_for(server._gateway(_Req(body, hd), "spawn"), 4))
    finally:
        h.release()
    assert r.value.status_code == 503 and r.value.headers.get("Retry-After")


def test_uncontended_verify_still_fast(d):
    fm = _fm(d)
    t0 = time.monotonic()
    for i in range(100):
        b, hd = _req(100 + i)
        fm.verify("POST", PATH, b, hd)
    assert time.monotonic() - t0 < 5
