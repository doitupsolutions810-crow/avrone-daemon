"""Round 88: CodeX log readers open first and check the open file.

Codex.replay/runs, codex_search, codex_stats and osint metrics checked is_file() (or nothing) and
then opened the log. A FIFO swapped in between parked the reader forever: an API request, or a pool
thread every off_loop tool shares. The race is simulated by making is_file() say True for a FIFO.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from avrone import codex as codex_mod
from avrone.codex import Codex


def _bg(fn, unblock: Path, timeout=4.0):
    out: dict = {}

    def go():
        try:
            out["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            out["e"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        with open(unblock, "wb"):
            pass
        t.join(2)
        pytest.fail("reader blocked on a FIFO")
    return out


@pytest.fixture()
def cx(tmp_path, monkeypatch):
    c = Codex(tmp_path)
    fifo = c.log_dir / "r.jsonl"
    os.mkfifo(fifo)
    real_is_file, real_is_link = Path.is_file, Path.is_symlink
    monkeypatch.setattr(Path, "is_file", lambda self: True if self == fifo else real_is_file(self))
    c.fifo = fifo
    return c


def test_open_log_refuses_fifo_and_symlink(tmp_path):
    f = getattr(codex_mod, "open_log", None)
    if f is None:
        pytest.fail("codex.open_log missing")
    os.mkfifo(tmp_path / "x.jsonl")
    r = _bg(lambda: f(tmp_path / "x.jsonl"), tmp_path / "x.jsonl")
    assert isinstance(r.get("e"), OSError)
    (tmp_path / "real").write_text("{}\n")
    os.symlink(tmp_path / "real", tmp_path / "l.jsonl")
    with pytest.raises(OSError):
        f(tmp_path / "l.jsonl")


def test_replay_fifo(cx):
    r = _bg(lambda: cx.replay("r"), cx.fifo)
    assert r.get("v") == []


def test_runs_fifo(cx):
    r = _bg(lambda: cx.runs(), cx.fifo)
    assert "e" not in r


def test_codex_stats_tail_fifo(cx):
    from avrone import tools_codexstats as cs
    r = _bg(lambda: cs._tail_bytes(cx.fifo, 1000), cx.fifo)
    assert r.get("v") == (b"", False)


def test_osint_iter_lines_fifo(cx):
    from avrone import osint
    r = _bg(lambda: osint._iter_lines(cx.fifo), cx.fifo)
    assert r.get("v") is None


def test_codex_search_fifo(cx, tmp_path):
    import asyncio
    from types import SimpleNamespace
    from avrone.tools_extra import t_codex_search
    ctx = SimpleNamespace(core=SimpleNamespace(codex=cx), run_id="x", name="A", depth=0)
    r = _bg(lambda: asyncio.run(t_codex_search(ctx, {"query": "x", "run_id": "r"})), cx.fifo)
    assert "e" not in r, r


def test_replay_reads_regular_logs(tmp_path):
    c = Codex(tmp_path)
    c.emit("r2", "run.start", "lead", prompt="hi")
    assert c.replay("r2")
