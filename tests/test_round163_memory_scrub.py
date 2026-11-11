"""Round 163: secret_scan fix=true in scope data masks secrets in stored memory values.

memory_save stored values raw and fleet memory sync shipped them upward; secret_scan flagged
memory.jsonl but fix refused it. A masked value gets a newer LWW ts, so a peer still holding the
raw copy at the old ts cannot win the equal-ts tie-break and bring it back.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from avrone import memstore
from avrone import tools_secrets as ts


def _tok() -> str:  # built at run time: no token-shaped literal for push protection
    return "gh" + "p_" + "D" * 36


def _ctx(d):
    return types.SimpleNamespace(core=types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=d), runner=None))


def test_fix_masks_memory_values_and_drops_old_lines(tmp_path):
    st = memstore.MemoryStore(tmp_path / "memory.jsonl")
    st.save("agent-a", "cfg", "old token " + _tok())
    st.save("agent-a", "cfg", "newer token " + _tok())
    st.save("agent-a", "plain", "hello")
    out = asyncio.run(ts.t_secret_scan(_ctx(tmp_path), {"scope": "data", "fix": True}))
    assert "memory: masked 1 value(s)" in out and "0 line(s) still flagged" in out
    raw = (tmp_path / "memory.jsonl").read_text()
    assert _tok() not in raw
    got = memstore.MemoryStore(tmp_path / "memory.jsonl").load("agent-a")
    assert got["plain"]["value"] == "hello" and "[REDACTED]" in got["cfg"]["value"]


def test_masked_value_beats_raw_peer_copy(tmp_path):
    scrub = getattr(memstore.MemoryStore, "scrub", None)
    if scrub is None:
        pytest.fail("MemoryStore.scrub missing")
    st = memstore.MemoryStore(tmp_path / "memory.jsonl")
    rec = st.save("agent-a", "cfg", "token " + _tok())
    raw_copy = dict(rec)  # what a peer kept from an earlier sync
    st.scrub()
    st.merge("agent-a", [raw_copy])
    assert _tok() not in st.load("agent-a")["cfg"]["value"]


def test_secret_in_key_reported_not_rewritten(tmp_path):
    st = memstore.MemoryStore(tmp_path / "memory.jsonl")
    st.save("agent-a", "token=" + "abc123def456", "x")
    r = st.scrub()
    assert r["keys"] == 1 and r["values"] == 0
    assert "token=abc123def456" in st.load("agent-a")
