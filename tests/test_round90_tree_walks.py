"""Round 90: fs_list and secret_scan walk trees lazily and within a budget.

Before: fs_list (recursive) and secret_scan both did sorted(root.rglob("*")): the whole tree was
listed and sorted before 500 lines were shown or 2000 files scanned. Empty files cost no agent-fs
quota, so an agent dir could hold millions; secret_scan walked node_modules and .git too and only
then dropped .git paths. secret_scan also opened files after an is_file() check (FIFO race).
"""
from __future__ import annotations

import os
import random
import threading
from pathlib import Path

import pytest


def _tw():
    from avrone import safeio
    if not hasattr(safeio, "TreeWalk"):
        pytest.fail("safeio.TreeWalk missing")
    return safeio.TreeWalk


def _tree(root: Path, seed=90, n=300):
    rng = random.Random(seed)
    root.mkdir(parents=True)
    dirs = [root]
    for i in range(n):
        d = rng.choice(dirs)
        name = rng.choice(["a", "B", "c-", "z", "_", "a.b", "Ä", "10", "9"]) + str(i)
        if rng.random() < 0.3:
            (d / name).mkdir()
            dirs.append(d / name)
        else:
            (d / name).write_text("x")
    return root


def test_treewalk_matches_sorted_rglob(tmp_path):
    TreeWalk = _tw()
    root = _tree(tmp_path / "t")
    want = [str(p.relative_to(root)) for p in sorted(root.rglob("*"))]
    got = [rel for rel, _ in TreeWalk(root, 10 ** 6)]
    assert got == want


def test_treewalk_budget_and_symlinked_dir(tmp_path):
    TreeWalk = _tw()
    root = tmp_path / "t"
    (root / "d").mkdir(parents=True)
    for i in range(50):
        (root / "d" / f"f{i}").touch()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").touch()
    os.symlink(outside, root / "link")
    w = TreeWalk(root, 20)
    rels = [r for r, _ in w]
    assert w.truncated and len(rels) <= 20
    assert not any(r.startswith("link/") for r in [r for r, _ in TreeWalk(root, 1000)])


def test_secret_scan_prunes_git_and_bounds_walk(tmp_path, monkeypatch):
    _tw()
    from avrone import tools_secrets as ts
    root = tmp_path / "ws"
    (root / ".git" / "objects").mkdir(parents=True)
    for i in range(200):
        (root / ".git" / "objects" / f"o{i}").touch()
    (root / "a.py").write_text('aws_secret_access_key = "AKIAABCDEFGHIJKLMNOP"\n')
    seen = []
    real = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p=".": (seen.append(os.fspath(p)), real(p))[1])
    out = ts.scan(root)
    assert not any(".git" in s for s in seen)
    monkeypatch.setattr(ts, "SCAN_ENTRIES_MAX", 1)
    for i in range(5):
        (root / f"z{i}.py").write_text("x\n")
    assert ts.scan(root)["truncated"] is True


def test_secret_scan_fifo_race(tmp_path, monkeypatch):
    _tw()
    from avrone import tools_secrets as ts
    root = tmp_path / "ws"
    root.mkdir()
    fifo = root / "f.py"
    os.mkfifo(fifo)
    real = os.DirEntry.is_file if hasattr(os, "DirEntry") else None
    out: dict = {}
    # make the FIFO look like a file to the type check, as if swapped in right after it
    import avrone.safeio as sio
    orig_iter = sio.TreeWalk.__iter__

    class FakeDE:
        def __init__(self, de):
            self._de, self.name, self.path = de, de.name, de.path

        def is_file(self, follow_symlinks=True):
            return True

        def is_dir(self, follow_symlinks=True):
            return False
    monkeypatch.setattr(sio.TreeWalk, "__iter__", lambda self: ((r, FakeDE(d)) for r, d in orig_iter(self)))

    def go():
        out["v"] = ts.scan(root)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(4)
    if t.is_alive():
        with open(fifo, "wb"):
            pass
        pytest.fail("secret_scan blocked on a FIFO")
    assert out["v"]["files"] == 0


def test_fs_list_recursive_bounded(tmp_path, monkeypatch):
    _tw()
    import asyncio
    from avrone import agentfs_tools as at
    root = tmp_path / "agent"
    (root / "d").mkdir(parents=True)
    for i in range(600):
        (root / "d" / f"f{i:04d}").touch()
    monkeypatch.setattr(at, "_resolve", lambda ctx, tool, raw: (root, root))
    monkeypatch.setattr(at, "dir_usage", lambda r: 0)
    out = asyncio.run(at.t_fs_list(None, {"recursive": True}))
    assert "d/f0000  0 bytes" in out and "more entries not shown" in out
    assert out.count("\n") <= at.LIST_MAX_ENTRIES + 3
    flat = asyncio.run(at.t_fs_list(None, {}))
    assert "d/" in flat and "f0000" not in flat
