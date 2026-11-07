"""Round 97: task_board runs off the event loop under a per-board lock.

Bugs on v0.89: the read-modify-write relied on the event loop, so two threads or two nodes on one
data dir lost each other's tasks; a board over 8 MB came back as its tail (never valid JSON) and the
next write silently replaced every task; run ids that differ only in punctuation shared one board.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace as S

from avrone import agentkit


def _ctx(tmp, run_id="run1", name="A"):
    return S(run_id=run_id, name=name,
             core=S(settings=S(data_dir=tmp / "d"), codex=S(emit=lambda *a, **k: None)))


def test_task_board_is_off_loop():
    assert getattr(agentkit.t_task_board, "off_loop", False)


def test_concurrent_adds_are_not_lost(tmp_path, monkeypatch):
    from avrone import safeio
    real = safeio.atomic_write

    def slow(p, data, *a, **k):
        time.sleep(0.05)  # widen the read-modify-write window
        return real(p, data, *a, **k)
    monkeypatch.setattr(safeio, "atomic_write", slow)
    c = _ctx(tmp_path)
    errs = []

    def worker(i):
        try:
            asyncio.run(agentkit.t_task_board(c, {"op": "add", "title": f"t{i}"}))
        except Exception as e:  # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)
    assert not errs
    board = json.loads(agentkit._board(c).read_text())
    assert sorted(t["title"] for t in board) == sorted(f"t{i}" for i in range(8))
    assert len({t["id"] for t in board}) == 8


def test_oversized_board_is_kept_not_wiped(tmp_path, monkeypatch):
    monkeypatch.setattr(agentkit, "BOARD_READ_MAX", 1000)
    c = _ctx(tmp_path)
    p = agentkit._board(c)
    big = json.dumps([{"id": i, "title": "x" * 50, "status": "todo"} for i in range(1, 60)])
    assert len(big) > 1000
    p.write_text(big)
    asyncio.run(agentkit.t_task_board(c, {"op": "add", "title": "new"}))
    kept = list(p.parent.glob(p.name + ".corrupt-*"))
    assert kept and kept[0].read_text() == big


def test_corrupt_board_is_set_aside(tmp_path):
    c = _ctx(tmp_path)
    p = agentkit._board(c)
    p.write_text("{not json")
    out = asyncio.run(agentkit.t_task_board(c, {"op": "add", "title": "fresh"}))
    assert "fresh" in out
    assert [q.read_text() for q in p.parent.glob(p.name + ".corrupt-*")] == ["{not json"]


def test_list_on_corrupt_board_changes_nothing(tmp_path):
    c = _ctx(tmp_path)
    p = agentkit._board(c)
    p.write_text("{not json")
    assert asyncio.run(agentkit.t_task_board(c, {"op": "list"})) == "board empty"
    assert p.read_text() == "{not json" and not list(p.parent.glob("*.corrupt-*"))


def test_punctuation_run_ids_get_separate_boards(tmp_path):
    a, b = _ctx(tmp_path, "a.b"), _ctx(tmp_path, "ab")
    assert agentkit._board(a) != agentkit._board(b)
    assert agentkit._board(_ctx(tmp_path, "...")) != agentkit._board(_ctx(tmp_path, "!!!"))
    asyncio.run(agentkit.t_task_board(a, {"op": "add", "title": "only-a"}))
    assert "only-a" not in asyncio.run(agentkit.t_task_board(b, {"op": "list"}))
