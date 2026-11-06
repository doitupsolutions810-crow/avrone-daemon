"""Round 80: memory store read path, oversized unknown tool names, temp-file symlinks.

1. MemoryStore._records re-parsed the whole memory.jsonl on every load (~0.9 s at 200,000 lines;
   ctxspawn calls it on the event loop per spawn). Parsed records are now cached per file
   identity (dev, inode, size, mtime_ns), and callers get copies.
2. A FIFO at memory.jsonl blocked read_text() / open("a") forever; a hand-edited key over
   MAX_KEY (which no save can make) was served to every recall.
3. A 1 MB "tool" name came back whole in the unknown-tool error, the codex events and the next
   prompt. It is clipped to UNKNOWN_NAME_MAX chars plus a count.
4. capgate's head temp file and memstore's rewrite temp file followed a symlink planted at their
   pid-predictable names and overwrote its target.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from avrone import capgate, memstore, tools
from avrone.memstore import MemoryStore


def _in_thread(fn, timeout=5.0):
    box = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise AssertionError("hung (blocked on a FIFO)")
    if "e" in box:
        raise box["e"]
    return box["v"]


def _unblock(fifo):
    for flags in (os.O_WRONLY | os.O_NONBLOCK, os.O_RDONLY | os.O_NONBLOCK):
        try:
            os.close(os.open(fifo, flags))
        except OSError:
            pass


def _big_store(tmp_path, n=60_000):
    p = tmp_path / "memory.jsonl"
    with open(p, "w") as f:
        for i in range(n):
            f.write(json.dumps({"ns": f"n{i % 50}", "key": f"k{i}", "value": "v" * 50, "ts": i}) + "\n")
    return MemoryStore(p)


def test_repeated_load_is_cached(tmp_path):
    s = _big_store(tmp_path)
    t0 = time.perf_counter()
    first = s.load("n1")
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(3):
        again = MemoryStore(s.path).load("n1")
    warm = (time.perf_counter() - t0) / 3
    assert again.keys() == first.keys()
    assert warm < cold * 0.5


def test_cache_sees_save_rewrite_and_hand_edit(tmp_path):
    p = tmp_path / "memory.jsonl"
    s = MemoryStore(p)
    s.save("a", "k", "one")
    assert s.load("a")["k"]["value"] == "one"
    s.save("a", "k", "two")
    assert MemoryStore(p).load("a")["k"]["value"] == "two"
    s.delete("a", "k")
    assert "k" not in s.load("a")
    time.sleep(0.01)
    p.write_text(json.dumps({"ns": "a", "key": "hand", "value": "edited", "ts": 1}) + "\n")
    assert s.load("a")["hand"]["value"] == "edited"


def test_mutating_a_result_never_touches_the_cache(tmp_path):
    s = MemoryStore(tmp_path / "memory.jsonl")
    s.save("a", "k", "v")
    got = s.load("a")
    got["k"]["value"] = "MUTATED"
    got.clear()
    assert s.load("a")["k"]["value"] == "v"


def test_fifo_memory_file_never_hangs(tmp_path):
    p = tmp_path / "memory.jsonl"
    os.mkfifo(p)
    s = MemoryStore(p)
    try:
        assert _in_thread(lambda: s.load("a")) == {}
        with pytest.raises(memstore.MemoryRefused):
            _in_thread(lambda: s.save("a", "k", "v"))
    finally:
        _unblock(p)


def test_overlong_hand_edited_key_is_dropped(tmp_path):
    # big values stay readable on purpose (test_robust_v020_homes); keys over MAX_KEY never could be saved
    p = tmp_path / "memory.jsonl"
    rows = [{"ns": "a", "key": "k" * (memstore.MAX_KEY + 1), "value": "v", "ts": 1},
            {"ns": "a", "key": "ok", "value": "fine", "ts": 1}]
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert list(MemoryStore(p).load("a")) == ["ok"]


def test_huge_unknown_tool_name_is_clipped():
    r = tools.builtin_registry()
    name = "x" * 1_000_000
    msg = r.unknown_message(name)
    assert len(msg) < 5000 and "(+" in msg
    reason, text = r.check(name, {})
    assert reason == "unknown_tool" and len(text) < 5000
    assert r.unknown_message("zz").startswith("ERROR: unknown tool 'zz'.")


def test_trinity_precheck_clips_unknown_tool_name():
    from avrone.trinity import TrinityCore as Trinity
    events = []
    t = Trinity.__new__(Trinity)
    t.tools = tools.builtin_registry()
    t.codex = type("C", (), {"emit": lambda self, *a, **k: events.append(k)})()
    budget = {"arg_errors": 0, "spent": 0}
    [c] = t._prechecked("r", "A", [{"tool": "y" * 500_000, "args": {}}], 1, budget)
    assert len(c["tool"]) < 200 and c["error"] is not None
    assert all(len(str(e.get("tool", ""))) < 200 for e in events)


def test_capgate_head_tmp_symlink_not_followed(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE_KEY", "k" * 32)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    p = capgate.receipts_path(tmp_path)
    p.parent.mkdir(parents=True)
    tmp = p.with_name(p.name + ".head").with_name(f"{p.name}.head.{os.getpid()}.tmp")
    os.symlink(victim, tmp)
    capgate.record(tmp_path, caller="a", tool="t", args={}, outcome="ok")
    assert victim.read_text() == "keep"
    assert capgate.verify_chain(tmp_path)["ok"] is True


def test_memstore_rewrite_tmp_symlink_not_followed(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep")
    p = tmp_path / "memory.jsonl"
    s = MemoryStore(p)
    s.save("a", "k", "v")
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    os.symlink(victim, tmp)
    assert s.delete("a", "k") is True  # delete always rewrites
    assert victim.read_text() == "keep"
    assert s.load("a") == {}
