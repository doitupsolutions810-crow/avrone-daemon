"""Round 81: predictable temp files and oversized codex events.

1. fleet.json, the fleet nonce snapshot, the bridge journal, evolve loop/history files, agentkit's
   task board and agent pack exports were written through temp names built from the pid and
   thread id with write_text(), which follows a symlink: a link planted at that name had its
   target overwritten. safeio.atomic_write uses mkstemp (random, O_EXCL). The nonce journal
   truncate no longer follows a symlinked journal.
2. Every codex event string went through ~10 redaction regexes on the event loop with no size
   cap: one tool.call carrying a 20 MB write_file content took 10.9 s to emit. Strings are now
   clipped (AVRONE_CODEX_FIELD_MAX_CHARS, 1,000,000; tool.call args AVRONE_CODEX_ARG_CHARS, 65,536).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from avrone import codex as codexmod


def _victim(tmp_path):
    v = tmp_path / "victim.txt"
    v.write_text("keep")
    return v


def _plant(target: Path, victim: Path, names):
    for n in names:
        try:
            os.symlink(victim, target.with_name(n))
        except FileExistsError:
            pass


def _pid_names(p: Path, dotted=True):
    pid, tid = os.getpid(), threading.get_ident()
    pre = "." if dotted else ""
    return [f"{pre}{p.name}.{pid}.{tid}.tmp", f"{pre}{p.name}.{pid}.tmp", f"{p.name}.{pid}.{tid}.tmp",
            p.with_suffix(".json.tmp").name]


def test_atomic_write_ignores_planted_links_and_keeps_mode(tmp_path):
    safeio = pytest.importorskip("avrone.safeio")
    v = _victim(tmp_path)
    p = tmp_path / "state.json"
    _plant(p, v, _pid_names(p))
    safeio.atomic_write(p, "{}")
    assert p.read_text() == "{}" and not p.is_symlink() and v.read_text() == "keep"
    um = os.umask(0)
    os.umask(um)
    assert (p.stat().st_mode & 0o777) == (0o666 & ~um)
    assert [x.name for x in tmp_path.iterdir() if x.name.endswith(".tmp") and not x.is_symlink()] == []


def test_atomic_write_failure_leaves_no_temp(tmp_path, monkeypatch):
    safeio = pytest.importorskip("avrone.safeio")
    p = tmp_path / "s.json"

    def boom(a, b):
        raise OSError("nope")
    monkeypatch.setattr(safeio.os, "replace", boom)
    with pytest.raises(OSError):
        safeio.atomic_write(p, "x")
    assert list(tmp_path.iterdir()) == []


def test_fleet_registry_and_nonce_snapshot_never_follow_tmp_links(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from avrone.fleet import FleetManager
    v = _victim(tmp_path)
    core = SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None),
                           settings=SimpleNamespace(data_dir=str(tmp_path)), active={})
    f = FleetManager(core, data_dir=tmp_path)
    _plant(f.path, v, _pid_names(f.path))
    f._write_registry(json.dumps({"nodes": {}}))
    assert v.read_text() == "keep"
    np = f._nonce_path
    _plant(np, v, _pid_names(np, dotted=False))
    f._nonces["n" * 16] = time.time() + 60
    f._save_nonces()
    assert v.read_text() == "keep" and "n" * 16 in json.loads(np.read_text())


def test_evolve_history_and_finish_never_follow_tmp_links(tmp_path):
    from avrone import evolve
    v = _victim(tmp_path)
    d = tmp_path / "loops"
    d.mkdir()
    h = d / "history.jsonl"
    _plant(h, v, _pid_names(h))
    evolve._record_history_at(h, {"loop_id": "L1", "status": "done", "goal": "g", "ts": 1})
    assert v.read_text() == "keep"
    assert [json.loads(x)["loop_id"] for x in h.read_text().splitlines()] == ["L1"]


def test_reconcile_never_follows_json_tmp_link(tmp_path):
    from types import SimpleNamespace
    from avrone import evolve
    v = _victim(tmp_path)
    d = tmp_path / "evolve" / "loops"
    d.mkdir(parents=True)
    f = d / "L2.json"
    f.write_text(json.dumps({"loop_id": "L2", "status": "running", "pid": 2 ** 22 + 12345,
                             "host": os.uname().nodename, "iterations": []}))
    os.symlink(v, d / "L2.json.tmp")
    evolve.reconcile_interrupted(SimpleNamespace(data_dir=str(tmp_path)))
    assert v.read_text() == "keep"


def test_big_tool_call_event_is_fast_and_clipped(tmp_path):
    c = codexmod.Codex(tmp_path)
    big = "x" * 20_000_000
    t0 = time.perf_counter()
    ev = c.emit("r1", "tool.call", "A", tool="write_file", args={"path": "a", "content": big})
    el = time.perf_counter() - t0
    content = ev.data["args"]["content"]
    assert len(content) < 1_100_000 and "clipped" in content
    assert el < 3.0


def test_clip_strings_and_env(monkeypatch):
    clip = getattr(codexmod, "clip_strings", None)
    assert clip is not None
    out = clip({"a": "y" * 50, "b": ["z" * 50, 5, None], "k" * 50: 1}, 10)
    assert out["a"].startswith("y" * 10) and "clipped 40" in out["a"]
    assert out["b"][1:] == [5, None] and "clipped" in out["b"][0]
    assert any("clipped" in k for k in out)
    assert clip("short", 10) == "short"
    monkeypatch.setenv("AVRONE_CODEX_FIELD_MAX_CHARS", "0")
    assert codexmod.field_max_chars() == 0
    monkeypatch.setenv("AVRONE_CODEX_FIELD_MAX_CHARS", "5")
    assert codexmod.field_max_chars() == 1024
    monkeypatch.setenv("AVRONE_CODEX_ARG_CHARS", "junk")
    assert codexmod.codex_arg_chars() == codexmod.DEFAULT_ARG_CHARS


def test_small_events_unchanged(tmp_path):
    c = codexmod.Codex(tmp_path)
    ev = c.emit("r1", "note", "A", text="hello", n=3, items=[1, "two"])
    assert ev.data == {"text": "hello", "n": 3, "items": [1, "two"]}
