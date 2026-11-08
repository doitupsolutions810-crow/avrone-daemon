"""Round 106: check-then-open races where a FIFO swapped in between parked a thread forever.

v0.98 sites: log_health's _scan_log (p.open after the listing), MemoryStore._append's torn-tail
probe (lstat, then path.open with the memory lock held), fix_runs' tail carry-over (lstat, then
open), MCP config load and the CLI --first-answer-file (is_file, then open). Each test swaps the
file for a FIFO at the exact gap and runs the op in a thread with a join timeout; the FIFO is
opened for write afterwards so a stuck old-code thread can finish.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import memstore, safeio, tools_limits, tools_secrets


def _bounded(fn, limit=4.0):
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


def _swap_to_fifo(p: Path):
    os.unlink(p)
    os.mkfifo(p)


def _unblock(p: Path):
    try:
        fd = os.open(p, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass


def test_log_health_fifo_swapped_after_listing(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    victim = runs / "r1.jsonl"
    victim.write_text(json.dumps({"kind": "run.start"}) + "\n")
    (runs / "r2.jsonl").write_text(json.dumps({"kind": "run.start"}) + "\n")
    real = safeio.newest_files

    def racing(*a, **k):
        out = real(*a, **k)
        _swap_to_fifo(victim)
        return out
    monkeypatch.setattr(safeio, "newest_files", racing)
    ctx = SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path))))
    try:
        done, box = _bounded(lambda: asyncio.run(tools_limits.t_log_health(ctx, {})))
    finally:
        _unblock(victim)
    assert done, "log_health blocked forever on a swapped-in FIFO"
    assert "r1" in box.get("v", "") and "unreadable" in box["v"]


def test_memstore_fifo_swapped_between_lstat_and_open(tmp_path, monkeypatch):
    ms = memstore.MemoryStore(tmp_path / "memory.jsonl")
    ms.save("ns", "a", "1")
    real = os.lstat
    fired = {}

    def racing(p, *a, **k):
        st = real(p, *a, **k)
        if Path(p) == ms.path and not fired:
            fired["x"] = 1
            _swap_to_fifo(ms.path)
        return st
    monkeypatch.setattr(memstore.os, "lstat", racing)
    try:
        done, box = _bounded(lambda: ms.save("ns", "b", "2"))
    finally:
        monkeypatch.undo()
        _unblock(ms.path)
    assert done, "memory save blocked forever (with the memory lock held)"
    assert isinstance(box.get("e"), memstore.MemoryRefused)
    # the lock was released: another op proceeds (and refuses the FIFO) instead of hanging
    done2, box2 = _bounded(lambda: ms.load("ns"))
    assert done2


def test_fix_runs_tail_fifo_swap(tmp_path, monkeypatch):
    p = tmp_path / "r.jsonl"
    p.write_text('{"note": "api_key=sk-' + "a" * 40 + '"}\n')
    real = os.lstat
    calls = {"n": 0}

    def racing(path, *a, **k):
        st = real(path, *a, **k)
        if Path(path) == p:
            calls["n"] += 1
            if calls["n"] == 2:  # st1 under the lock: grow the file, then swap for a FIFO
                fake = os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, st.st_uid, st.st_gid,
                                       st.st_size + 10, int(st.st_atime), int(st.st_mtime), int(st.st_ctime)))
                _swap_to_fifo(p)
                return fake
        return st
    monkeypatch.setattr(tools_secrets.os, "lstat", racing)
    try:
        done, box = _bounded(lambda: tools_secrets.fix_runs(tmp_path, {"r.jsonl"}))
    finally:
        monkeypatch.undo()
        _unblock(p)
    assert done, "fix_runs blocked forever on a FIFO swapped in under the codex lock"
    assert box.get("v") == 0
    assert not list(tmp_path.glob("r.jsonl.*.tmp"))


def test_mcp_config_fifo_swap(tmp_path, monkeypatch):
    from avrone import mcp_client
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    real = Path.is_file

    def racing(self):
        ok = real(self)
        if self == cfg and ok:
            _swap_to_fifo(cfg)
        return ok
    monkeypatch.setattr(Path, "is_file", racing)
    try:
        done, box = _bounded(lambda: mcp_client.load_config(cfg))
    finally:
        monkeypatch.undo()
        _unblock(cfg)
    assert done, "MCP config load blocked forever"
    assert isinstance(box.get("e"), ValueError)


def test_mcp_config_symlink_still_followed(tmp_path):
    from avrone import mcp_client
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"mcpServers": {}}))
    link = tmp_path / "mcp.json"
    link.symlink_to(real)
    assert mcp_client.load_config(link) == {}


def test_cli_answer_file_fifo_swap(tmp_path, monkeypatch):
    from avrone import cli
    f = tmp_path / "a.txt"
    f.write_text("hello")
    real = Path.is_file

    def racing(self):
        ok = real(self)
        if self == f and ok:
            _swap_to_fifo(f)
        return ok
    monkeypatch.setattr(Path, "is_file", racing)
    try:
        done, box = _bounded(lambda: cli._read_answer_file(str(f)))
    finally:
        monkeypatch.undo()
        _unblock(f)
    assert done, "--first-answer-file blocked forever"
    assert isinstance(box.get("e"), ValueError)


def test_cli_answer_file_normal(tmp_path):
    from avrone import cli
    f = tmp_path / "a.txt"
    f.write_text("hello")
    assert cli._read_answer_file(str(f)) == "hello"


def test_memstore_torn_tail_still_repaired(tmp_path):
    ms = memstore.MemoryStore(tmp_path / "memory.jsonl")
    ms.save("ns", "a", "1")
    with open(ms.path, "a") as fh:
        fh.write('{"torn": ')
    ms.save("ns", "b", "2")
    assert ms.load("ns")["b"]["value"] == "2"
