"""Round 94: state_health reads safely, stays bounded and runs off the event loop.

Before: _check_jsonl used stat() + read_bytes(), following a symlinked memory.jsonl or bridge log to
any host file and hanging on a FIFO swapped in after the listing; each area was sorted(glob) whole
before the 2000 cap; the *.corrupt-* sweep rglob'd the whole data_dir; and t_state_health did all of
it on the event loop.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from types import SimpleNamespace

import pytest

from avrone import tools_statehealth as sh


def _ctx(d):
    return SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=str(d))))


def test_symlinked_memory_not_followed(tmp_path):
    host = tmp_path / "host.txt"
    host.write_text("secret host file without newline")
    data = tmp_path / "data"
    data.mkdir()
    os.symlink(host, data / "memory.jsonl")
    st = sh.state_health(data)
    probs = {p["path"]: p["problem"] for p in st["problems"]}
    assert probs.get("memory.jsonl") == "not a regular file"  # was "torn last line" (read the host file)


def test_check_jsonl_fifo_does_not_hang(tmp_path):
    f = tmp_path / "b.jsonl"
    os.mkfifo(f)
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", sh._check_jsonl(f)), daemon=True)
    t.start()
    t.join(3)
    if t.is_alive():
        with open(f, "wb"):
            pass
        t.join(3)
        pytest.fail("_check_jsonl blocked on a FIFO")
    assert out["r"] == "not a regular file"


def test_corrupt_sweep_bounded(tmp_path, monkeypatch):
    if not hasattr(sh, "CORRUPT_SCAN_MAX"):
        pytest.fail("CORRUPT_SCAN_MAX missing")
    d = tmp_path / "evolve"
    d.mkdir()
    for i in range(60):
        (d / f"r{i:03d}.jsonl").touch()
    (tmp_path / "fleet.json.corrupt-1").touch()
    monkeypatch.setattr(sh, "CORRUPT_SCAN_MAX", 20)
    st = sh.state_health(tmp_path)
    assert st["set_aside_truncated"] is True
    out = asyncio.run(sh.t_state_health(_ctx(tmp_path), {}))
    assert "set aside as" in out


def test_corrupt_sweep_skips_run_logs(tmp_path, monkeypatch):
    if not hasattr(sh, "CORRUPT_SCAN_MAX"):
        pytest.fail("CORRUPT_SCAN_MAX missing")
    d = tmp_path / "runs"
    d.mkdir()
    for i in range(60):
        (d / f"r{i:03d}.jsonl").touch()
    (tmp_path / "zz.corrupt-1").touch()
    monkeypatch.setattr(sh, "CORRUPT_SCAN_MAX", 20)
    st = sh.state_health(tmp_path)
    assert st["set_aside_truncated"] is False and st["set_aside"] == ["zz.corrupt-1"]


def test_area_scan_bounded(tmp_path, monkeypatch):
    if not hasattr(sh, "AREA_SCAN_MAX"):
        pytest.fail("AREA_SCAN_MAX missing")
    d = tmp_path / "checkpoints"
    d.mkdir()
    for i in range(50):
        (d / f"c{i:03d}.json").write_text("{}")
    monkeypatch.setattr(sh, "AREA_SCAN_MAX", 10)
    st = sh.state_health(tmp_path)
    assert st["areas"]["checkpoints"]["files"] <= 10


def test_state_health_off_event_loop(tmp_path, monkeypatch):
    (tmp_path / "fleet.json").write_text("{}")
    real = sh._check_json
    monkeypatch.setattr(sh, "_check_json", lambda p: (time.sleep(0.4), real(p))[1])

    async def main():
        ticks, stop = 0, False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.02)
        t = asyncio.create_task(ticker())
        await sh.t_state_health(_ctx(tmp_path), {})
        stop = True
        await t
        return ticks
    assert asyncio.run(main()) >= 10


def test_fifo_in_area_reported_not_opened(tmp_path):
    d = tmp_path / "bridge"
    d.mkdir()
    os.mkfifo(d / "r.jsonl")
    (d / "ok.jsonl").write_text('{"a":1}\n')
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", sh.state_health(tmp_path)), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive()
    assert out["r"]["areas"]["bridges"]["files"] >= 1
