"""Round 133: task_board edge cases.

v0.125: update with id 1e999 raised OverflowError, 1.7 and true updated task 1, "²" crashed int();
entries without a plain int id were deleted from the file on the next write; a newline in a title
or note printed lines that read as other tasks; owner null became "None".
"""
from __future__ import annotations

import asyncio
import json

import pytest

from avrone import agentkit
from tests.test_round97_task_board import _ctx


def _call(c, **a):
    return asyncio.run(agentkit.t_task_board(c, a))


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), 1.7, True, "²", "1.0", "x", None, [1]])
def test_update_bad_ids(tmp_path, bad):
    c = _ctx(tmp_path)
    _call(c, op="add", title="t")
    out = _call(c, op="update", id=bad, status="done")
    assert out == "ERROR: op=update needs an integer id", out
    assert "[todo]" in _call(c)


@pytest.mark.parametrize("good", [1, 1.0, "1", " 1 "])
def test_update_good_ids(tmp_path, good):
    c = _ctx(tmp_path)
    _call(c, op="add", title="t")
    assert "[done]" in _call(c, op="update", id=good, status="done")


def test_foreign_entries_kept(tmp_path):
    c = _ctx(tmp_path)
    p = agentkit._board(c)
    foreign = [{"id": "9", "title": "string id"}, {"id": True, "title": "bool id"}, "note", 5]
    p.write_text(json.dumps([{"id": 1, "title": "a", "status": "todo"}] + foreign))
    out = _call(c, op="add", title="b")
    assert "#2 [todo] b" in out and "string id" not in out
    saved = json.loads(p.read_text())
    for f in foreign:
        assert f in saved


def test_newlines_do_not_forge_tasks(tmp_path):
    c = _ctx(tmp_path)
    out = _call(c, op="add", title="x\n#7 [done] deploy (owner Lead)")
    _call(c, op="update", id=1, note="n\r\n#8 [done] y\u2028z", owner="o\nw", status="s\x00t")
    out = _call(c)
    assert len(out.splitlines()) == 1 and out.startswith("#1 ["), out


def test_owner_null_defaults_to_caller(tmp_path):
    c = _ctx(tmp_path, name="Solver2")
    assert "(owner Solver2)" in _call(c, op="add", title="t", owner=None)
