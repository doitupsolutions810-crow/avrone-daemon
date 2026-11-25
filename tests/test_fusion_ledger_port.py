"""Regression for ledger durability ported after PR #21 was closed unmerged.

Torn files are kept. A failed save is not an accept. Two processes do not drop
keys. A clock step below zero is not an open breaker. A shared nested dict is
not a cycle. A header-breaking token is not a dead peer. A body over 64 KiB is
not buffered.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from avrone.fusion_deliver import (
    MAX_NESTING,
    Ledger,
    ManualClock,
    prepare_event,
)
from avrone.safeio import atomic_write

PY = sys.executable
REPO = Path(__file__).resolve().parents[1]


def _event(eid: str, **payload) -> dict:
    body = {"text": eid}
    body.update(payload)
    return {
        "id": eid, "source": "avrone", "kind": "evidence", "run_id": "edge",
        "payload": body, "ts": 1,
    }


def test_torn_file_is_not_replaced(tmp_path):
    path = tmp_path / "torn.json"
    path.write_text("{", encoding="utf-8")
    broken = Ledger(path, ManualClock())
    assert broken.state_errors == 1 and broken.accounts()["A"] == 0
    with pytest.raises(OSError):
        broken.offer(_event("after"))
    assert path.read_text(encoding="utf-8") == "{"


def test_disk_full_rolls_the_new_key_back(tmp_path, monkeypatch):
    path = tmp_path / "led.json"
    ledger = Ledger(path, ManualClock())
    assert ledger.offer(_event("kept")) == "accepted"

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("avrone.safeio.atomic_write", full)
    with pytest.raises(OSError):
        ledger.offer(_event("nospace"))
    monkeypatch.undo()
    again = Ledger(path, ManualClock())
    assert {row["id"] for row in again.entries.values()} == {"kept"}


def test_two_processes_do_not_drop_keys(tmp_path):
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
    procs = [subprocess.Popen([PY, "-c", code, str(path), f"p{i:02d}"]) for i in range(8)]
    for proc in procs:
        assert proc.wait(timeout=30) == 0
    loaded = Ledger(path, ManualClock())
    assert loaded.accounts()["A"] == 8
    assert {row["id"] for row in loaded.entries.values()} == {f"p{i:02d}" for i in range(8)}


def test_clock_below_zero_is_not_an_open_breaker(tmp_path):
    class Back(ManualClock):
        def sleep(self, seconds: float) -> None:
            seconds = 0.0 if seconds < 0 else float(seconds)
            self.sleeps.append(seconds)
            self.t -= seconds if seconds else 1.0

    clock = Back()
    ledger = Ledger(tmp_path / "back.json", clock)

    def retry(method, url, body, headers, timeout):
        from avrone.fusion_deliver import HttpResult
        if method == "GET":
            return HttpResult(200, {"boot": "b"}, None)
        return HttpResult(429, {"detail": "later"}, None, "0.05")

    from avrone.fusion_deliver import DeliverConfig
    cfg = DeliverConfig(
        base="http://127.0.0.1:8090", events_path="/v1/events", health_path="/health",
        token="", timeout=0.2, deadline=0, breaker_threshold=5, breaker_cooldown=30,
        backoff_base=0.05, backoff_max=0.2, interval=0.05,
    )
    assert ledger.offer(_event("back")) == "accepted"
    report = ledger.pump(retry, cfg, max_cycles=2)
    assert report["calls"] == 64 and report["P"] == 1 and report["F"] == 0
    assert clock.t < 0


def test_shared_child_is_legal_and_depth_33_is_not():
    shared = {"k": 1}
    event, err = prepare_event(_event("diamond", left=shared, right=shared))
    assert err is None and event is not None
    node: dict = {"n": 0}
    cursor = node
    for _ in range(MAX_NESTING):
        cursor["down"] = {}
        cursor = cursor["down"]
    assert prepare_event(_event("fit", tree=node))[1] is None
    cursor["down"] = {}
    assert prepare_event(_event("over", tree=node))[1] is not None


def test_header_breaking_token_does_not_open_the_breaker(tmp_path):
    from avrone.fusion_deliver import DeliverConfig, HttpResult
    clock = ManualClock()
    seen = {"n": 0}

    def transport(method, url, body, headers, timeout):
        if method == "POST":
            seen["n"] += 1
        return HttpResult(200, {"accepted": True, "boot": "b"}, None)

    ledger = Ledger(tmp_path / "hdr.json", clock)
    ledger.offer(_event("tok"))
    cfg = DeliverConfig(
        base="http://127.0.0.1:8090", events_path="/v1/events", health_path="/health",
        token="ab\u0085cd", timeout=0.2, deadline=0, breaker_threshold=1, breaker_cooldown=30,
        backoff_base=0.05, backoff_max=0.2, interval=0.05,
    )
    report = ledger.pump(transport, cfg, max_cycles=1)
    assert report["P"] == 1 and report["F"] == 0
    assert seen["n"] == 0
    assert ledger.breaker_for("http://127.0.0.1:8090").fails == 0


def test_oversized_peer_body_does_not_open_the_breaker(tmp_path):
    from avrone.fusion_deliver import DeliverConfig, HttpResult
    ledger = Ledger(tmp_path / "big.json", ManualClock())
    ledger.offer(_event("big"))

    def oversized(method, url, body, headers, timeout):
        if method == "GET":
            return HttpResult(200, {"boot": "b"}, None)
        return HttpResult(200, None, "truncated")

    cfg = DeliverConfig(
        base="http://127.0.0.1:8090", events_path="/v1/events", health_path="/health",
        token="", timeout=0.2, deadline=0, breaker_threshold=1, breaker_cooldown=30,
        backoff_base=0.05, backoff_max=0.2, interval=0.05,
    )
    report = ledger.pump(oversized, cfg, max_cycles=1)
    assert report["P"] == 1 and report["F"] == 0
    assert ledger.breaker_for("http://127.0.0.1:8090").fails == 0


def test_old_delivered_bodies_are_dropped_and_ids_remain(tmp_path, monkeypatch):
    import avrone.fusion_deliver as fd
    monkeypatch.setattr(fd, "DELIVERED_BODY_KEEP", 2)
    from avrone.fusion_deliver import DeliverConfig, HttpResult
    path = tmp_path / "grow.json"
    ledger = Ledger(path, ManualClock())

    def ok(method, url, body, headers, timeout):
        if method == "GET":
            return HttpResult(200, {"boot": "b"}, None)
        return HttpResult(200, {"accepted": True}, None)

    cfg = DeliverConfig(
        base="http://127.0.0.1:8090", events_path="/v1/events", health_path="/health",
        token="", timeout=0.2, deadline=0, breaker_threshold=3, breaker_cooldown=30,
        backoff_base=0.05, backoff_max=0.2, interval=0.05,
    )
    for i in range(4):
        assert ledger.offer(_event(f"c{i}")) == "accepted"
    assert ledger.pump(ok, cfg, max_cycles=2)["D"] == 4
    assert path.read_text(encoding="utf-8").count('"text"') == 2
    again = Ledger(path, ManualClock())
    assert again.accounts()["D"] == 4
    assert again.offer(_event("c0")) == "duplicate"


def test_save_fsyncs(tmp_path, monkeypatch):
    seen: list[bool] = []
    real = atomic_write

    def spy(path, data, *args, **kwargs):
        seen.append(bool(kwargs.get("fsync")))
        return real(path, data, *args, **kwargs)

    monkeypatch.setattr("avrone.safeio.atomic_write", spy)
    assert Ledger(tmp_path / "fs.json", ManualClock()).offer(_event("fs")) == "accepted"
    assert seen == [True]


def test_kill_during_append_stays_valid_json(tmp_path):
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
    ledger.offer({"id": f"k{n:05d}", "source": "avrone", "kind": "evidence", "run_id": "edge",
                  "payload": {"text": "x"}, "ts": 1})
"""
    proc = subprocess.Popen([PY, "-c", code, str(path)], cwd=str(REPO))
    time.sleep(0.4)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=5)
    data = json.loads(path.read_bytes())
    assert data["version"] == 2
    again = Ledger(path, ManualClock())
    assert again.offer(_event("seed")) == "duplicate"
    assert again.accounts()["A"] == again.accounts()["D"] + again.accounts()["P"] + again.accounts()["F"]


def test_signals_leave_the_pending_row(tmp_path):
    path = tmp_path / "sig.json"
    code = r"""
import sys, time
from pathlib import Path
from avrone.fusion_deliver import DeliverConfig, HttpResult, Ledger, ManualClock
path = Path(sys.argv[1])
ledger = Ledger(path, ManualClock())
ledger.offer({"id":"sig1","source":"avrone","kind":"evidence","run_id":"edge",
              "payload":{"text":"sig1"},"ts":1})
def slow(method, url, body, headers, timeout):
    time.sleep(30)
    return HttpResult(200, {"accepted": True, "boot": "b"}, None)
cfg = DeliverConfig(base="http://127.0.0.1:8090", events_path="/v1/events", health_path="/health",
                    token="", timeout=5, deadline=0, breaker_threshold=3, breaker_cooldown=30,
                    backoff_base=0.05, backoff_max=0.2, interval=0.05)
ledger.pump(slow, cfg, max_cycles=1)
"""
    proc = subprocess.Popen([PY, "-c", code, str(path)], cwd=str(REPO))
    for _ in range(50):
        if path.exists() and path.stat().st_size > 0:
            break
        time.sleep(0.05)
    time.sleep(0.2)
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)
    assert proc.returncode != 0
    row = json.loads(path.read_text(encoding="utf-8"))["entries"][0]
    assert row["id"] == "sig1" and row["state"] == "pending"
    proc = subprocess.Popen([PY, "-c", code, str(path)], cwd=str(REPO))
    time.sleep(0.4)
    proc.send_signal(signal.SIGHUP)
    proc.wait(timeout=5)
    assert proc.returncode != 0
    json.loads(path.read_text(encoding="utf-8"))
