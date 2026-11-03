"""Round 42: capgate — capability-scoped tool calls and a tamper-evident receipt chain.

Covers: off by default (no receipts, no behaviour change); audit records but never refuses;
enforce refuses tools outside the policy; amplification forbidden on delegation; expiry and
audience; forged/edited capabilities; malformed policy fails closed; receipt chain detects
edited, deleted, reordered and forged lines; concurrent writers keep one valid chain; spawn
hands children an attenuated capability; seeded fuzz of capabilities never verifies junk.
"""
from __future__ import annotations

import asyncio
import json
import random
import threading
import time
from types import SimpleNamespace

import pytest

from avrone import capgate
from avrone.tools import Tool, ToolRegistry


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AVRONE_CAPGATE", "AVRONE_CAPGATE_POLICY", "AVRONE_CAPGATE_TTL", "AVRONE_FLEET_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_CAPGATE_KEY", "capgate-test-key-" + "x" * 32)


def _ctx(tmp_path, name="Agent1", role=None):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path)))
    return SimpleNamespace(core=core, name=name, role=role, run_id="run1", capability=None)


def _reg():
    r = ToolRegistry()

    async def echo(ctx, args):
        return "echo:" + json.dumps(args, sort_keys=True)

    async def boom(ctx, args):
        raise RuntimeError("kaboom")

    r.register(Tool("echo", "echo", "{}", echo))
    r.register(Tool("read_file", "read", "{}", echo))
    r.register(Tool("shell", "shell", "{}", echo))
    r.register(Tool("boom", "boom", "{}", boom))
    return r


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------------ modes
def test_off_by_default_no_receipts(tmp_path):
    assert capgate.mode() == "off"
    out = run(_reg().call(_ctx(tmp_path), "shell", {"cmd": "ls"}))
    assert out.startswith("echo:")
    assert not capgate.receipts_path(tmp_path).exists()


def test_junk_mode_is_off(monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE", "ENFORCE-ish")
    assert capgate.mode() == "off"


def test_enforce_refuses_outside_policy_and_records(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE", "enforce")
    monkeypatch.setenv("AVRONE_CAPGATE_POLICY", json.dumps({"*": ["read_*", "echo"]}))
    reg, ctx = _reg(), _ctx(tmp_path)
    assert run(reg.call(ctx, "read_file", {"path": "a"})).startswith("echo:")
    denied = run(reg.call(ctx, "shell", {"cmd": "rm -rf /"}))
    assert denied.startswith("CAP DENIED (shell)") and "did not run" in denied
    chain = capgate.verify_chain(tmp_path)
    assert chain["ok"] and chain["count"] == 2
    lines = [json.loads(x) for x in capgate.receipts_path(tmp_path).read_text().splitlines()]
    assert [x["outcome"] for x in lines] == ["ok", "denied"]
    # receipts hold digests, never the raw arguments
    assert "rm -rf" not in capgate.receipts_path(tmp_path).read_text()


def test_audit_never_refuses_but_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE", "audit")
    monkeypatch.setenv("AVRONE_CAPGATE_POLICY", json.dumps({"*": ["echo"]}))
    out = run(_reg().call(_ctx(tmp_path), "shell", {"cmd": "x"}))
    assert out.startswith("echo:")
    rec = json.loads(capgate.receipts_path(tmp_path).read_text().splitlines()[-1])
    assert rec["outcome"] == "ok+audit"


def test_tool_error_is_receipted_as_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE", "enforce")
    out = run(_reg().call(_ctx(tmp_path), "boom", {}))
    assert out.startswith("ERROR: RuntimeError")
    rec = json.loads(capgate.receipts_path(tmp_path).read_text().splitlines()[-1])
    assert rec["outcome"] == "error"


@pytest.mark.parametrize("raw", ["{not json", "[1,2]", '"*"', json.dumps({"*": 7}), "[[[[" * 5000])
def test_malformed_policy_fails_closed(tmp_path, monkeypatch, raw):
    monkeypatch.setenv("AVRONE_CAPGATE", "enforce")
    monkeypatch.setenv("AVRONE_CAPGATE_POLICY", raw)
    assert run(_reg().call(_ctx(tmp_path), "echo", {})).startswith("CAP DENIED")


def test_role_and_name_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE", "enforce")
    monkeypatch.setenv("AVRONE_CAPGATE_POLICY", json.dumps({"*": ["echo"], "Researcher": ["read_file"]}))
    reg = _reg()
    r = _ctx(tmp_path, name="Researcher1", role="Researcher")
    assert run(reg.call(r, "read_file", {})).startswith("echo:")
    assert run(reg.call(r, "echo", {})).startswith("CAP DENIED")


# ------------------------------------------------------------------ capabilities
def test_expiry_audience_and_forgery():
    cap = capgate.issue("A", ["echo"], ttl_s=60)
    assert capgate.verify(cap, "echo", "A")[0]
    assert capgate.verify(cap, "echo", "B") == (False, "capability is for 'A', not 'B'")
    assert capgate.verify(cap, "echo", "A", now=time.time() + 61)[1] == "expired"
    forged = dict(cap, rights=["*"])
    assert capgate.verify(forged, "shell", "A") == (False, "bad signature")
    longer = dict(cap, expires_at=cap["expires_at"] + 10**6)
    assert not capgate.verify(longer, "echo", "A")[0]


def test_wrong_key_rejects(monkeypatch):
    cap = capgate.issue("A", ["*"])
    monkeypatch.setenv("AVRONE_CAPGATE_KEY", "another-key")
    assert capgate.verify(cap, "echo", "A") == (False, "bad signature")


def test_attenuation_never_amplifies():
    root = capgate.issue("Lead", ["read_*", "echo"])
    child = capgate.attenuate(root, "Kid", ["read_file"])
    assert child["depth"] == 1 and child["parent_id"] == root["id"]
    assert child["expires_at"] == root["expires_at"]
    assert capgate.verify(child, "read_file", "Kid")[0]
    assert not capgate.verify(child, "echo", "Kid")[0]
    for bad in (["shell"], ["*"], ["read_*", "shell"], ["r*"]):
        with pytest.raises(capgate.CapDenied):
            capgate.attenuate(root, "Kid", bad)
    with pytest.raises(capgate.CapDenied):  # cannot re-widen a narrowed child
        capgate.attenuate(child, "Grandkid", ["read_*"])


def test_delegation_depth_cap():
    c = capgate.issue("a", ["*"])
    for i in range(capgate.MAX_DEPTH):
        c = capgate.attenuate(c, f"a{i}")
    with pytest.raises(capgate.CapDenied):
        capgate.attenuate(c, "too-deep")


def test_attenuate_for_child_falls_back_instead_of_amplifying(monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE_POLICY", json.dumps({"*": ["echo"], "Hacker": ["shell"]}))
    parent = capgate.issue("Lead", ["echo"])
    kid = capgate.attenuate_for_child(parent, "Hacker1", "Hacker")
    assert kid["rights"] == ["echo"]  # policy wanted shell; parent never had it


@pytest.mark.parametrize("seed", [42, 4242, 20261103])
def test_fuzzed_capabilities_never_verify(seed):
    r = random.Random(seed)
    good = capgate.issue("A", ["*"])
    for i in range(500):
        cap = dict(good)
        k = r.choice(list(cap))
        cap[k] = r.choice([None, 0, "", "*", [], {}, "A", 10**30, float("nan"), cap[k]])
        if cap == good:
            continue
        ok, _ = capgate.verify(cap, "echo", "A")
        assert not ok, (seed, i, k)
    for junk in (None, 1, "cap", [], {"sig": 5}, {"sig": "0" * 64}):
        assert capgate.verify(junk, "echo", "A")[0] is False


# ------------------------------------------------------------------ receipt chain
def _fill(tmp_path, n=6):
    for i in range(n):
        capgate.record(tmp_path, caller="A", tool="echo", args={"i": i}, outcome="ok", output=str(i))


def test_chain_detects_edit_delete_reorder_forge(tmp_path, monkeypatch):
    _fill(tmp_path)
    p = capgate.receipts_path(tmp_path)
    good = p.read_text().splitlines()
    assert capgate.verify_chain(tmp_path) == {"ok": True, "count": 6, "head": json.loads(good[-1])["hash"]}

    edited = list(good)
    rec = json.loads(edited[2]); rec["tool"] = "shell"; edited[2] = json.dumps(rec)
    p.write_text("\n".join(edited) + "\n")
    assert capgate.verify_chain(tmp_path)["error"].startswith("hash mismatch")

    p.write_text("\n".join(good[:2] + good[3:]) + "\n")
    v = capgate.verify_chain(tmp_path)
    assert not v["ok"] and v["line"] == 3 and "broken link" in v["error"]

    p.write_text("\n".join([good[1], good[0]] + good[2:]) + "\n")
    assert not capgate.verify_chain(tmp_path)["ok"]

    # an attacker who recomputes hashes but lacks the key still fails on the signature
    import hashlib
    forged = list(good)
    rec = json.loads(forged[-1]); rec["outcome"] = "denied"
    body = {k: v for k, v in rec.items() if k not in ("hash", "sig")}
    rec["hash"] = hashlib.sha256(capgate._canon(body)).hexdigest()
    forged[-1] = json.dumps(rec)
    p.write_text("\n".join(forged) + "\n")
    assert "bad signature" in capgate.verify_chain(tmp_path)["error"]

    p.write_text("\n".join(good) + "\n{torn")
    assert capgate.verify_chain(tmp_path)["error"] == "unreadable line"


def test_chain_concurrent_writers_stay_linked(tmp_path):
    def w(k):
        for i in range(40):
            capgate.record(tmp_path, caller=f"W{k}", tool="echo", args=i, outcome="ok")

    ts = [threading.Thread(target=w, args=(k,)) for k in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    v = capgate.verify_chain(tmp_path)
    assert v["ok"] and v["count"] == 240


def test_empty_chain_ok(tmp_path):
    assert capgate.verify_chain(tmp_path) == {"ok": True, "count": 0, "head": capgate.GENESIS}


def test_ttl_clamped(monkeypatch):
    for raw, want in (("5", 30), ("999999", 86400), ("junk", 3600), ("120", 120)):
        monkeypatch.setenv("AVRONE_CAPGATE_TTL", raw)
        assert capgate.ttl() == want


# ------------------------------------------------------------------ spawn wiring
def test_spawn_passes_parent_capability_only_when_on(tmp_path, monkeypatch):
    from avrone import ctxspawn
    ctx = _ctx(tmp_path, name="Lead1")
    assert ctxspawn._cap_kw(ctx) == {}
    monkeypatch.setenv("AVRONE_CAPGATE", "enforce")
    kw = ctxspawn._cap_kw(ctx)
    assert capgate.check_sig(kw["parent_capability"])[0]
    assert kw["parent_capability"]["audience"] == "Lead1"


def test_non_path_data_dir_writes_nothing(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    monkeypatch.setenv("AVRONE_CAPGATE", "audit")
    monkeypatch.chdir(tmp_path)
    ctx = SimpleNamespace(core=MagicMock(), name="A", role=None, run_id="r", capability=None)
    assert run(_reg().call(ctx, "echo", {})).startswith("echo:")
    assert list(tmp_path.iterdir()) == []
