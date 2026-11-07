"""Round 98: image_inspect / image_decode open and decode the image off the event loop.

On v0.90 _open_image ran on the loop: im.load() decoded up to 40 Mpx there, and is_file() followed
by Image.open(path) hung the loop forever when a FIFO was swapped in between the two calls.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from avrone import agentkit

PIL = pytest.importorskip("PIL")


def _ctx(tmp):
    ws = tmp / "ws"
    ws.mkdir(exist_ok=True)
    core = S(runner=S(workspace=str(ws)), settings=S(data_dir=tmp / "d"), codex=S(emit=lambda *a, **k: None))
    return S(core=core, run_id="r1", name="A", depth=0), ws


def _png(path: Path, w=40, h=30):
    from PIL import Image
    Image.new("RGB", (w, h), (10, 200, 30)).save(path)


def test_open_runs_off_loop(tmp_path, monkeypatch):
    c, ws = _ctx(tmp_path)
    real = agentkit._open_image

    def slow(*a, **k):
        time.sleep(0.4)
        return real(*a, **k)
    monkeypatch.setattr(agentkit, "_open_image", slow)
    _png(ws / "a.png")

    async def main():
        ticks, stop = 0, False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.02)
        t = asyncio.create_task(ticker())
        out = await agentkit.t_image_inspect(c, {"path": "a.png"})
        stop = True
        await t
        return ticks, out
    ticks, out = asyncio.run(main())
    assert "40x30" in out and ticks >= 10


def test_fifo_swapped_after_check_does_not_hang(tmp_path, monkeypatch):
    c, ws = _ctx(tmp_path)
    fifo = ws / "f.png"
    os.mkfifo(fifo)
    monkeypatch.setattr(Path, "is_file", lambda self: True)  # the FIFO appears after the check
    res = {}
    th = threading.Thread(target=lambda: res.setdefault("r", asyncio.run(
        agentkit.t_image_inspect(c, {"path": "f.png"}))), daemon=True)
    th.start()
    th.join(3)
    if th.is_alive():
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
        th.join(3)
        pytest.fail("image_inspect hung on a FIFO")
    assert res["r"].startswith("ERROR") and "regular file" in res["r"]


def test_size_reported_from_bytes_read(tmp_path):
    c, ws = _ctx(tmp_path)
    _png(ws / "b.png")
    out = asyncio.run(agentkit.t_image_inspect(c, {"path": "b.png"}))
    assert f"size={(ws / 'b.png').stat().st_size}B" in out


def test_symlinked_image_still_readable(tmp_path):
    c, ws = _ctx(tmp_path)
    _png(ws / "real.png")
    os.symlink(ws / "real.png", ws / "link.png")
    assert "40x30" in asyncio.run(agentkit.t_image_inspect(c, {"path": "link.png"}))


def test_directory_and_empty_and_oversize(tmp_path, monkeypatch):
    c, ws = _ctx(tmp_path)
    (ws / "d.png").mkdir()
    (ws / "e.png").write_bytes(b"")
    assert "regular file" in asyncio.run(agentkit.t_image_inspect(c, {"path": "d.png"}))
    assert "empty" in asyncio.run(agentkit.t_image_inspect(c, {"path": "e.png"}))
    _png(ws / "big.png", 200, 200)
    monkeypatch.setattr(agentkit, "MAX_IMAGE_BYTES", 50)
    assert "cap is 50" in asyncio.run(agentkit.t_image_inspect(c, {"path": "big.png"}))
