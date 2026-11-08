"""Round 107: stat-then-read_bytes/read_text windows (checkpoint, agentpack, .env, searchproc).

v0.99: a symlink or FIFO swapped in between a stat()/is_file() check and read_bytes()/read_text()
was followed or blocked forever. The worst was agentpack: a symlink swapped in after _walk's lstat
packed a host file into the agent export that migrates to another node. Each test swaps at the
exact gap and runs the op in a bounded thread; FIFOs are opened for write afterwards.
"""
from __future__ import annotations

import io
import json
import os
import tarfile
import threading
from pathlib import Path

import pytest

from avrone import agentfs, agentpack, checkpoint, config, searchproc


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


def _fifo(p: Path):
    os.unlink(p)
    os.mkfifo(p)


def _unblock(p: Path):
    try:
        os.close(os.open(p, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


class _StatSwap:
    """Path.stat wrapper: return the real stat for `target`, then swap it (once)."""

    def __init__(self, monkeypatch, target: Path, swap, nth: int = 1):
        self.real, self.target, self.swap, self.done = Path.stat, target, swap, False
        self.nth, self.seen = nth, 0
        monkeypatch.setattr(Path, "stat", self)

    def __get__(self, obj, cls):
        return lambda *a, **k: self(obj, *a, **k)

    def __call__(self, obj, *a, **k):
        st = self.real(obj, *a, **k)
        if not self.done and Path(obj) == self.target:
            self.seen += 1
            if self.seen == self.nth:  # the stat right before the read (earlier ones: resolve, is_file)
                self.done = True
                self.swap(self.target)
        return st


def test_checkpoint_load_fifo_swap(tmp_path, monkeypatch):
    p = tmp_path / "run.json"
    checkpoint.Checkpoint(p).put("k", 1)
    _StatSwap(monkeypatch, p, _fifo, nth=2)
    try:
        done, box = _bounded(lambda: checkpoint.Checkpoint(p))
    finally:
        monkeypatch.undo()
        _unblock(p)
    assert done, "opening a checkpoint blocked forever on a swapped-in FIFO"


def test_checkpoint_load_symlink_swap_not_followed(tmp_path, monkeypatch):
    p = tmp_path / "run.json"
    checkpoint.Checkpoint(p).put("k", 1)
    host = tmp_path / "host.json"
    host.write_text(json.dumps({"version": checkpoint.VERSION, "steps": {"stolen": {"v": "HOST"}}}))

    def to_link(t):
        os.unlink(t)
        os.symlink(host, t)
    _StatSwap(monkeypatch, p, to_link, nth=2)
    ck = checkpoint.Checkpoint(p)
    monkeypatch.undo()
    assert "HOST" not in json.dumps(ck._store.data if hasattr(ck, "_store") else ck.__dict__, default=str)


def test_checkpoint_read_helper_fifo(tmp_path):
    p = tmp_path / "x.json"
    os.mkfifo(p)
    done, box = _bounded(lambda: checkpoint._read(p))
    _unblock(p)
    assert done and box.get("v") is None


def _home(ws, aid="builder"):
    d = agentfs.agents_root(ws) / aid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _swap_after_walk(monkeypatch, name, swap):
    real = agentpack._walk

    def racing(root):
        out = real(root)
        for rel, p, st in out:
            if p.name == name:
                swap(p)
        return out
    monkeypatch.setattr(agentpack, "_walk", racing)


def test_agentpack_symlink_swap_never_packs_host_file(tmp_path, monkeypatch):
    d = _home(tmp_path / "ws")
    (d / "notes.txt").write_text("hello")
    host = tmp_path / "host-secret"
    host.write_text("hostkey")  # same length as "hello"? no: size check also guards

    def to_link(p):
        os.unlink(p)
        os.symlink(host, p)
    _swap_after_walk(monkeypatch, "notes.txt", to_link)
    with pytest.raises(agentpack.PackError, match="changed while packing"):
        agentpack.export_pack(tmp_path / "ws", tmp_path / "data", "builder", "edge")


def test_agentpack_symlink_swap_same_size(tmp_path, monkeypatch):
    d = _home(tmp_path / "ws")
    (d / "notes.txt").write_text("hello")
    host = tmp_path / "host-secret"
    host.write_text("HOSTS")  # same size: the old length check could not catch it

    def to_link(p):
        os.unlink(p)
        os.symlink(host, p)
    _swap_after_walk(monkeypatch, "notes.txt", to_link)
    try:
        bundle, _ = agentpack.export_pack(tmp_path / "ws", tmp_path / "data", "builder", "edge")
    except agentpack.PackError:
        return
    with tarfile.open(fileobj=io.BytesIO(bundle)) as tf:
        blobs = b"".join(tf.extractfile(m).read() for m in tf.getmembers() if m.isfile())
    assert b"HOSTS" not in blobs, "a host file was packed into the agent export"


def test_agentpack_fifo_swap(tmp_path, monkeypatch):
    d = _home(tmp_path / "ws")
    (d / "notes.txt").write_text("hello")
    _swap_after_walk(monkeypatch, "notes.txt", _fifo)
    target = d / "notes.txt"
    try:
        done, box = _bounded(lambda: agentpack.export_pack(tmp_path / "ws", tmp_path / "data", "builder", "edge"))
    finally:
        _unblock(target)
    assert done, "export_pack blocked forever on a swapped-in FIFO"
    assert isinstance(box.get("e"), agentpack.PackError)


def test_dotenv_fifo_swap(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("AVRONE_R107_X=1\n")
    real = Path.is_file

    def racing(self):
        ok = real(self)
        if self == env and ok:
            _fifo(env)
        return ok
    monkeypatch.setattr(Path, "is_file", racing)
    try:
        done, _ = _bounded(lambda: config._load_dotenv(env))
    finally:
        monkeypatch.undo()
        _unblock(env)
    assert done, ".env load blocked startup forever"


def test_dotenv_oversized_ignored(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    with open(env, "wb") as f:
        f.write(b"AVRONE_R107_BIG=1\n")
        f.truncate(config.DOTENV_MAX_BYTES + 10)
    monkeypatch.delenv("AVRONE_R107_BIG", raising=False)
    config._load_dotenv(env)
    assert "AVRONE_R107_BIG" not in os.environ


def test_dotenv_symlink_followed(tmp_path, monkeypatch):
    real = tmp_path / "real.env"
    real.write_text("AVRONE_R107_LINK=yes\n")
    link = tmp_path / ".env"
    link.symlink_to(real)
    monkeypatch.delenv("AVRONE_R107_LINK", raising=False)
    config._load_dotenv(link)
    assert os.environ.get("AVRONE_R107_LINK") == "yes"
    monkeypatch.delenv("AVRONE_R107_LINK", raising=False)


def test_searchproc_root_file_fifo_swap(tmp_path, monkeypatch):
    f = tmp_path / "one.txt"
    f.write_text("needle\n")
    _StatSwap(monkeypatch, f, _fifo, nth=2)
    out = io.StringIO()
    try:
        done, box = _bounded(lambda: searchproc.run({"pattern": "needle", "root": str(f)}, out))
    finally:
        monkeypatch.undo()
        _unblock(f)
    assert done, "search over a single-file root blocked forever"
    assert '"done": true' in out.getvalue()


def test_searchproc_root_file_normal(tmp_path):
    f = tmp_path / "one.txt"
    f.write_text("needle\n")
    out = io.StringIO()
    searchproc.run({"pattern": "needle", "root": str(f)}, out)
    assert '"line": 1' in out.getvalue()
