"""Round 84: every append/lock/read of a state file refuses symlinks and special files.

Before: the bridge journal and its .lock, agentkit's resources.jsonl ledger and task board,
capgate's .torn sidecar, the fleet nonce journal, memstore's memory.lock and memory.jsonl append
(os.stat followed a link to a regular file) all followed a planted symlink, appending to or locking
its target; FIFOs made several of them block forever (Bridge() at boot, the resource ledger).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


def _victim(tmp_path: Path) -> Path:
    v = tmp_path / "victim.txt"
    v.write_text("keep")
    return v


def _in_thread(fn, timeout=5.0):
    out: dict = {}

    def go():
        try:
            out["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            out["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "blocked"
    return out


def test_safeio_open_append_refuses_links_and_fifos(tmp_path):
    safeio = pytest.importorskip("avrone.safeio")
    if not hasattr(safeio, "open_append"):
        pytest.fail("open_append missing")
    v = _victim(tmp_path)
    os.symlink(v, tmp_path / "l")
    os.mkfifo(tmp_path / "f")
    for name in ("l", "f"):
        r = _in_thread(lambda: safeio.open_append(tmp_path / name, "ab"))
        assert isinstance(r.get("e"), safeio.NotRegularFile)
        assert safeio.read_regular(tmp_path / name) is None
    with safeio.open_append(tmp_path / "ok.log", "a") as f:
        f.write("x\n")
    assert (tmp_path / "ok.log").read_text() == "x\n" and v.read_text() == "keep"


def test_read_regular_tail_whole_lines(tmp_path):
    safeio = pytest.importorskip("avrone.safeio")
    if not hasattr(safeio, "read_regular"):
        pytest.fail("read_regular missing")
    p = tmp_path / "j"
    p.write_bytes(b"aaaa\nbbbb\ncccc\n")
    assert safeio.read_regular(p, 7) == b"cccc\n"
    assert safeio.read_regular(p) == p.read_bytes()


def test_bridge_journal_and_lock_links_not_followed(tmp_path):
    from avrone.bridge import Bridge
    v = _victim(tmp_path)
    b = Bridge("r84", data_dir=tmp_path)
    j = b.path
    os.symlink(v, j)
    os.symlink(v, j.with_name(j.name + ".lock"))
    try:
        b.post("A", "note", "hello")
    except Exception:  # noqa: BLE001
        pass
    assert v.read_text() == "keep"


def test_bridge_boot_with_fifo_journal_does_not_hang(tmp_path):
    from avrone.bridge import Bridge
    j = Bridge("r84f", data_dir=tmp_path).path
    os.mkfifo(j)
    r = _in_thread(lambda: Bridge("r84f", data_dir=tmp_path))
    assert "e" not in r, r


def test_memstore_append_through_link_refused(tmp_path):
    from avrone.memstore import MemoryRefused, MemoryStore
    v = _victim(tmp_path)
    ms = MemoryStore(tmp_path / "memory.jsonl")
    os.symlink(v, ms.path)
    with pytest.raises(MemoryRefused):
        ms.save("ns", "k", "val")
    assert v.read_text() == "keep"


def test_memstore_lock_link_refused(tmp_path):
    from avrone.memstore import MemoryRefused, MemoryStore
    v = _victim(tmp_path)
    ms = MemoryStore(tmp_path / "memory.jsonl")
    os.symlink(v, ms.lock_path)
    r = _in_thread(lambda: ms.save("ns", "k", "val"))
    assert isinstance(r.get("e"), MemoryRefused)
    assert v.read_text() == "keep"


def test_fleet_nonce_journal_link_not_appended(tmp_path, monkeypatch):
    from avrone import fleet as fl
    from avrone.fleet import FleetManager
    v = _victim(tmp_path)
    core = SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None),
                           settings=SimpleNamespace(data_dir=str(tmp_path)), active={})
    f = FleetManager(core, data_dir=tmp_path)
    jp = f._journal_path()
    if jp is None:
        pytest.skip("no journal path")
    monkeypatch.setattr(fl, "NONCE_JOURNAL_MIN", 0)
    for i in range(3):
        f._nonces[f"n{i:015d}"] = 9e9
    os.symlink(v, jp)
    f._persist_nonce("z" * 16, 9e9)
    assert v.read_text() == "keep"


def _kctx(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path)),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r84", name="A")


def test_resource_ledger_link_and_fifo(tmp_path):
    from avrone import agentkit
    v = _victim(tmp_path)
    ctx = _kctx(tmp_path)
    os.symlink(v, tmp_path / "resources.jsonl")
    agentkit.record_change(ctx, "install", "pkg", True)
    assert v.read_text() == "keep"
    os.unlink(tmp_path / "resources.jsonl")
    os.mkfifo(tmp_path / "resources.jsonl")
    r = _in_thread(lambda: agentkit.record_change(ctx, "install", "pkg", True))
    assert "e" not in r, r
    r = _in_thread(lambda: agentkit._installed_before(ctx, "pip", "pkg"))
    assert "e" not in r and r.get("v") is None
