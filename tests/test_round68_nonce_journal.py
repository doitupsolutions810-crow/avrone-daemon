"""Round 68: the fleet replay cache cost O(n) per accepted request, on the event loop.

FleetManager.verify() pruned the whole nonce dict and rewrote fleet_nonces.json in full for every
accepted request (it runs synchronously inside the async gateway handlers). Measured: 0.8 ms per
request at 1k live nonces, 8 ms at 10k, 42 ms at 50k, 85 ms near the 100k cap, so a busy or hostile
signed peer saturated the loop at ~100 rps and stalled every other client. Past NONCE_JOURNAL_MIN
live nonces an accepted request now appends one line to fleet_nonces.json.journal (compacted into
the snapshot once it outgrows the live set) and expired entries are pruned at most once a second.
Replay protection is unchanged: across instances sharing the data dir, across restarts, through
compaction, and the cache still fails closed when full.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import avrone.fleet as fleet_mod
from avrone.fleet import FleetManager, GatewayRefused, sign

TOKEN = "round68-fleet-token-" + "z" * 32
PATH = "/api/fleet/run"


def _fm(d: Path) -> FleetManager:
    core = SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None),
                           settings=SimpleNamespace(data_dir=str(d)))
    return FleetManager(core, data_dir=d)


@pytest.fixture
def d(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", TOKEN)
    monkeypatch.delenv("AVRONE_FLEET_ALLOW", raising=False)
    return tmp_path


def _fill(fm: FleetManager, n: int) -> None:
    now = time.time()
    fm._nonces = {secrets.token_hex(12): now + 100 for _ in range(n)}
    fm._save_nonces()


def _req(i):
    body = json.dumps({"i": i}).encode()
    return body, sign(TOKEN, "POST", PATH, body)


def test_verify_cost_does_not_grow_with_the_cache(d):
    fm = _fm(d)
    _fill(fm, 50_000)
    reqs = [_req(i) for i in range(200)]
    t0 = time.perf_counter()
    for body, h in reqs:
        fm.verify("POST", PATH, body, h)
    per = (time.perf_counter() - t0) / len(reqs)
    assert per < 0.005, f"{per * 1000:.1f} ms per verify at 50k live nonces"  # was ~42 ms


def test_big_cache_replay_refused_by_another_instance(d):
    a, b = _fm(d), _fm(d)
    _fill(a, 3000)
    sent = [_req(i) for i in range(30)]
    for i, (body, h) in enumerate(sent):
        (a if i % 2 else b).verify("POST", PATH, body, h)
    assert (d / "fleet_nonces.json.journal").stat().st_size > 0  # took the journal path
    for body, h in sent:
        for fm in (a, b):
            with pytest.raises(GatewayRefused) as e:
                fm.verify("POST", PATH, body, h)
            assert e.value.code == "replay"


def test_restart_reads_snapshot_and_journal(d):
    a = _fm(d)
    _fill(a, 3000)
    sent = [_req(i) for i in range(20)]
    for body, h in sent:
        a.verify("POST", PATH, body, h)
    c = _fm(d)  # a later process: must refuse every one of them
    for body, h in sent:
        with pytest.raises(GatewayRefused):
            c.verify("POST", PATH, body, h)
    assert c._boot_guard is None


def test_compaction_keeps_every_nonce(d, monkeypatch):
    monkeypatch.setattr(fleet_mod, "NONCE_JOURNAL_MIN", 50)
    a, b = _fm(d), _fm(d)
    _fill(a, 60)
    sent = [_req(i) for i in range(300)]  # several compactions (journal > live set)
    for i, (body, h) in enumerate(sent):
        (a if i % 3 else b).verify("POST", PATH, body, h)
    snap = json.loads((d / "fleet_nonces.json").read_text())
    jl = (d / "fleet_nonces.json.journal").read_text().split()
    on_disk = set(snap) | set(jl[0::2])
    assert all(h["x-avrone-nonce"] in on_disk for _, h in sent)
    assert len(jl) // 2 < 300  # it was compacted, not just appended to
    c = _fm(d)
    for body, h in sent:
        with pytest.raises(GatewayRefused):
            c.verify("POST", PATH, body, h)


def test_torn_and_junk_journal_lines_are_ignored(d):
    a = _fm(d)
    _fill(a, 1500)
    body, h = _req(1)
    a.verify("POST", PATH, body, h)
    with open(d / "fleet_nonces.json.journal", "ab") as fh:
        fh.write(b"bad/nonce 9e99\nok-nonce notanumber\nnan-n nan\n\xff\xfe 1\npartial-tail 99999999")
    c = _fm(d)
    assert "partial-tail" not in c._nonces and "bad/nonce" not in c._nonces and "nan-n" not in c._nonces
    with pytest.raises(GatewayRefused):
        c.verify("POST", PATH, body, h)
    body2, h2 = _req(2)
    c.verify("POST", PATH, body2, h2)  # still accepts new requests


def test_cache_full_still_fails_closed_on_journal_path(d, monkeypatch):
    monkeypatch.setattr(fleet_mod, "NONCE_JOURNAL_MIN", 10)
    monkeypatch.setattr(fleet_mod, "NONCE_CACHE_MAX", 40)
    a = _fm(d)
    _fill(a, 39)
    body, h = _req(0)
    a.verify("POST", PATH, body, h)
    body2, h2 = _req(1)
    with pytest.raises(GatewayRefused) as e:
        a.verify("POST", PATH, body2, h2)
    assert "full" in str(e.value)


def test_expired_unpruned_nonce_does_not_block_reuse(d, monkeypatch):
    monkeypatch.setattr(fleet_mod, "NONCE_JOURNAL_MIN", 10)
    a = _fm(d)
    _fill(a, 20)
    a._next_prune = time.time() + 1.0  # prune throttled
    a._nonces["reused"] = time.time() - 1  # expired but still in the dict
    body = b"{}"
    a.verify("POST", PATH, body, sign(TOKEN, "POST", PATH, body, nonce="reused"))
