"""Round 86: pack and search reads stay inside their bounds.

Before: agent_pack_import and pack_list read a pack with read_bytes() (a multi-GB file loaded whole
before read_pack refused it; a FIFO named like a pack parked a worker thread forever), and the
search_files child followed a symlink in the tree to any file on the host and printed its lines.
"""
from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path

import pytest


def _bg(fn, timeout=5.0):
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


def _read_pack_file():
    from avrone import agentpack
    f = getattr(agentpack, "read_pack_file", None)
    if f is None:
        pytest.fail("agentpack.read_pack_file missing")
    return agentpack, f


def test_pack_fifo_refused_without_blocking(tmp_path):
    agentpack, f = _read_pack_file()
    p = tmp_path / "a-1.tar.gz"
    os.mkfifo(p)
    r = _bg(lambda: f(p))
    assert isinstance(r.get("e"), agentpack.PackError)


def test_pack_symlink_refused(tmp_path):
    agentpack, f = _read_pack_file()
    real = tmp_path / "real"
    real.write_bytes(b"x")
    os.symlink(real, tmp_path / "a-1.tar.gz")
    with pytest.raises(agentpack.PackError):
        f(tmp_path / "a-1.tar.gz")


def test_pack_oversized_refused_before_reading(tmp_path, monkeypatch):
    agentpack, f = _read_pack_file()
    monkeypatch.setattr(agentpack, "quota_bytes", lambda: 10)
    p = tmp_path / "a-1.tar.gz"
    with open(p, "wb") as fh:
        fh.truncate(4 * 1024 * 1024)
    import avrone.safeio as sio
    real = os.read
    seen = []
    monkeypatch.setattr(os, "read", lambda fd, n: (seen.append(n), real(fd, n))[1])
    with pytest.raises(agentpack.PackError, match="quota"):
        f(p)
    assert sum(seen) == 0


def test_pack_list_with_fifo_returns(tmp_path):
    _read_pack_file()
    from types import SimpleNamespace
    import asyncio
    from avrone import agentpack, tools_packpeer
    d = agentpack.pack_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    os.mkfifo(d / "a-1.tar.gz")
    ctx = SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path))))
    r = _bg(lambda: asyncio.run(tools_packpeer.t_pack_list(ctx, {})))
    assert "REFUSED" in r["v"]


def _search(root: Path, pattern: str) -> list[dict]:
    from avrone import searchproc
    out = io.StringIO()
    searchproc.run({"pattern": pattern, "root": str(root)}, out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_search_does_not_follow_file_symlinks(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("TOPSECRET=1\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.txt").write_text("TOPSECRET in workspace\n")
    os.symlink(outside, ws / "leak.txt")
    rows = _search(ws, "TOPSECRET")
    paths = {Path(r["path"]).name for r in rows if "path" in r}
    assert paths == {"ok.txt"}


def test_search_skips_fifo(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("needle\n")
    os.mkfifo(ws / "b.fifo")
    r = _bg(lambda: _search(ws, "needle"))
    assert any(x.get("done") for x in r["v"])
