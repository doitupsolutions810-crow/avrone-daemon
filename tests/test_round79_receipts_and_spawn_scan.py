"""Round 79: capgate receipt chain under junk files, receipts off the loop, spawn dir scan.

1. verify_chain raised AttributeError on a receipt line that is valid JSON but not an object
   ([1], 5, "x"); it now reports "unreadable line". _last_hash returned a junk "hash" as is.
2. receipts.jsonl as a FIFO hung record() (every gated tool call) and verify_chain; as a symlink
   it appended receipts to the link's target. The chain files open O_NOFOLLOW|O_NONBLOCK, regular only.
3. capgate.record ran inline on the event loop for every gated call (flock, digests, two fsyncs),
   and an exception from it lost the output of a tool that had already run. It now runs in a
   thread and a failure is logged, not raised.
4. ctxspawn._dir_files sorted the whole rglob of the parent agent dir on every spawn
   (100,000 files: ~6 s on the loop). It now walks in the same order and stops at DIR_FILES.
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

from avrone import capgate, ctxspawn


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE_KEY", "k" * 32)


def _chain(tmp_path, n=2):
    for i in range(n):
        capgate.record(tmp_path, caller="a", tool=f"t{i}", args={}, outcome="ok")
    return capgate.receipts_path(tmp_path)


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


@pytest.mark.parametrize("junk", [b"[1]", b"5", b'"x"', b"null", b"[" * 50000 + b"]" * 50000])
def test_verify_chain_non_object_line_is_unreadable_not_a_crash(tmp_path, junk):
    p = _chain(tmp_path)
    with open(p, "ab") as f:
        f.write(junk + b"\n")
    r = capgate.verify_chain(tmp_path)
    assert r["ok"] is False and r["line"] == 3 and r["error"] == "unreadable line"


@pytest.mark.parametrize("junk", [b'{"hash": 5}', b'{"hash": "' + b"z" * 64 + b'"}', b"[1]"])
def test_last_hash_ignores_junk_hash(tmp_path, junk):
    p = _chain(tmp_path)
    good = json.loads(p.read_bytes().splitlines()[-1])["hash"]
    with open(p, "ab") as f:
        f.write(junk + b"\n")
    assert capgate._last_hash(p) == good


def test_fifo_receipts_never_hang_record_or_verify(tmp_path):
    p = capgate.receipts_path(tmp_path)
    p.parent.mkdir(parents=True)
    os.mkfifo(p)
    try:
        with pytest.raises(OSError):
            _in_thread(lambda: capgate.record(tmp_path, caller="a", tool="t", args={}, outcome="ok"))
        r = _in_thread(lambda: capgate.verify_chain(tmp_path))
    finally:
        _unblock(p)
    assert r["ok"] is False and "unreadable" in r["error"]


def test_symlinked_receipts_never_written_through(tmp_path):
    target = tmp_path / "victim.txt"
    target.write_text("keep\n")
    p = capgate.receipts_path(tmp_path)
    p.parent.mkdir(parents=True)
    os.symlink(target, p)
    with pytest.raises(OSError):
        capgate.record(tmp_path, caller="a", tool="t", args={}, outcome="ok")
    assert target.read_text() == "keep\n"
    assert capgate.verify_chain(tmp_path)["ok"] is False


def test_chain_still_verifies_after_changes(tmp_path):
    _chain(tmp_path, 5)
    r = capgate.verify_chain(tmp_path)
    assert r["ok"] is True and r["count"] == 5


def test_record_failure_does_not_lose_tool_output(tmp_path, monkeypatch):
    from avrone import tools
    calls = {}

    def boom(*a, **k):
        calls["n"] = calls.get("n", 0) + 1
        raise OSError("disk full")
    monkeypatch.setattr(capgate, "mode", lambda: "audit")
    monkeypatch.setattr(capgate, "gate", lambda ctx, name: (True, "", None))
    monkeypatch.setattr(capgate, "data_dir_of", lambda ctx: tmp_path)
    monkeypatch.setattr(capgate, "record", boom)
    rec = getattr(tools, "_receipt", None)
    assert rec is not None
    asyncio.run(rec(tmp_path, caller="a", tool="x", args={}, outcome="ok"))
    assert calls["n"] == 1


def test_record_runs_off_the_event_loop(tmp_path, monkeypatch):
    from avrone import tools
    seen = {}

    def slow(*a, **k):
        seen["thread"] = threading.current_thread() is not threading.main_thread()
        time.sleep(0.3)
    monkeypatch.setattr(capgate, "record", slow)
    rec = getattr(tools, "_receipt", None)
    assert rec is not None

    async def go():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)
        t = asyncio.create_task(ticker())
        t0 = time.perf_counter()
        await rec(tmp_path, caller="a", tool="x", args={}, outcome="ok")
        el = time.perf_counter() - t0
        t.cancel()
        return ticks, el
    ticks, el = asyncio.run(go())
    assert seen["thread"] is True
    assert ticks >= max(5, int(el / 0.01 * 0.3))


def _old_dir_files(root: Path):
    names = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            names.append(p.relative_to(root).as_posix())
            if len(names) >= ctxspawn.DIR_FILES:
                names.append("...")
                break
    return names


def test_dir_files_same_order_as_sorted_rglob(tmp_path):
    for rel in ["b/z.txt", "b/a/c", "a", "a b/x", "A/q", "b.txt", "_/1", "b/a/b/d", "c/e/f/g/h"]:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    (tmp_path / "empty").mkdir()
    assert ctxspawn._dir_files(SimpleNamespace(agent_dir=tmp_path)) == _old_dir_files(tmp_path)


def test_dir_files_stops_early_on_huge_tree(tmp_path, monkeypatch):
    for i in range(40):
        d = tmp_path / f"d{i:02d}"
        d.mkdir()
        for j in range(500):
            (d / f"f{j}").touch()
    t0 = time.perf_counter()
    _old_dir_files(tmp_path)
    old = time.perf_counter() - t0
    t0 = time.perf_counter()
    out = ctxspawn._dir_files(SimpleNamespace(agent_dir=tmp_path))
    new = time.perf_counter() - t0
    assert len(out) == ctxspawn.DIR_FILES + 1 and out[-1] == "..."
    assert new < old * 0.25


def test_dir_files_does_not_enter_symlinked_dirs(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("s")
    root = tmp_path / "agent"
    root.mkdir()
    (root / "ok").write_text("x")
    os.symlink(outside, root / "link")
    assert ctxspawn._dir_files(SimpleNamespace(agent_dir=root)) == ["ok"]
