"""Round 62: edit_file's whitespace-tolerant fallback was quadratic, ran on the loop, and
replace_all spliced overlapping matches into corrupt output.

Found with a probe of edit_file on adversarial files (/workspace/work/editprobe.py):

- "a\\na\\na\\n" with old "a\\na  " (trailing spaces force the loose path) and replace_all gave
  "B\\n": two OVERLAPPING spans (lines 0-1 and 1-2) were spliced in reverse and a line vanished.
  str.replace semantics are non-overlapping, left to right; the loose path now matches that.
- _loose_span compared line by line: 60k lines with a 3000-line `old` took 13.3 s, on the event
  loop. Now one str.find over the right-stripped text, realigned on line starts (0.02 s).
- _nearest_hint ran difflib over every full line for a miss: 100k lines 3.8 s, 2000 lines of 5000
  chars 4.3 s. Lines are clipped to 200 chars and the first 20,000 distinct lines compared.
- the whole tool now runs off the loop; files over EDIT_MAX_BYTES (32 MB) are refused with a
  pointer to shell instead of being read whole.
"""
from __future__ import annotations

import asyncio
import random
import time
from types import SimpleNamespace

import pytest

from avrone import tools_extra as tx
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in ("AVRONE_AGENT_FS", "AVRONE_AGENT_DOCKER_ISOLATE"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "d")
    r = LocalRunner(s)
    asyncio.run(r.start())
    core = SimpleNamespace(settings=s, runner=r, codex=Codex(s.data_dir))
    ctx = SimpleNamespace(core=core, run_id="r", name="A", depth=0, agent_id=None, agent_dir=None)
    reg = builtin_registry()
    register_extra_tools(reg)
    return SimpleNamespace(ws=s.workspace, ctx=ctx, reg=reg)


def edit(env, **args):
    return asyncio.run(env.reg.call(env.ctx, "edit_file", args))


def _reference(text: str, old: str) -> list[tuple[int, int]]:
    """The old line-by-line algorithm, made non-overlapping: the oracle for the fast one."""
    olines = [ln.rstrip() for ln in old.strip("\n").split("\n")]
    if not any(olines):
        return []
    lines = text.split("\n")
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    k, spans, i = len(olines), [], 0
    while i <= len(lines) - k:
        if all(lines[i + j].rstrip() == olines[j] for j in range(k)):
            spans.append((starts[i], starts[i + k - 1] + len(lines[i + k - 1])))
            i += k
        else:
            i += 1
    return spans


def test_replace_all_never_splices_overlapping_matches(env):
    (env.ws / "o.txt").write_text("a\na\na\n")
    out = edit(env, path="o.txt", old="a\na  ", new="B", replace_all=True)
    assert out.startswith("edited o.txt (1 replacement")
    assert (env.ws / "o.txt").read_text() == "B\na\n"


def test_overlap_count_is_non_overlapping(env):
    (env.ws / "o.txt").write_text("a\na\na\na\n")
    out = edit(env, path="o.txt", old="a\na   ", new="B")
    assert "matches 2 times" in out  # used to say 3 (overlapping)
    assert (env.ws / "o.txt").read_text() == "a\na\na\na\n"


@pytest.mark.parametrize("seed", range(6))
def test_loose_span_matches_reference(seed):
    rnd = random.Random(seed)
    alphabet = ["a", "a ", "b", "", "  ", "a\t", "ab", "b  "]
    for _ in range(400):
        text = "\n".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 25)))
        if rnd.random() < 0.3:
            text = text.replace("\n", "\n", 1) + "\n"
        old = "\n".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 4)))
        assert tx._loose_span(text, old) == _reference(text, old), (text, old)


def test_loose_span_is_fast_on_adversarial_input():
    text = "x\n" * 60000
    old = "x \n" * 3000 + "y"
    t = time.monotonic()
    assert tx._loose_span(text, old) == []
    assert time.monotonic() - t < 1.0


def test_loose_span_cap():
    spans = tx._loose_span("a\n" * (tx.MAX_LOOSE_SPANS + 50), "a ")
    assert len(spans) == tx.MAX_LOOSE_SPANS


def test_nearest_hint_bounded_on_long_lines():
    text = ("q" * 5000 + "\n") * 2000
    t = time.monotonic()
    hint = tx._nearest_hint(text, "q" * 4999 + "z")
    assert time.monotonic() - t < 1.5
    assert "closest line is 1" in hint


def test_nearest_hint_still_points_at_the_right_line():
    text = "alpha\nbeta gamma delta\nomega\n"
    assert "closest line is 2" in tx._nearest_hint(text, "beta gamma delts")


def test_edit_file_runs_off_loop():
    assert getattr(tx.t_edit_file, "off_loop", False)


def test_edit_file_refuses_huge_file(env, monkeypatch):
    monkeypatch.setattr(tx, "EDIT_MAX_BYTES", 100)
    (env.ws / "h.txt").write_text("x" * 200)
    out = edit(env, path="h.txt", old="x", new="y")
    assert out.startswith("ERROR:") and "use shell" in out
    assert (env.ws / "h.txt").read_text() == "x" * 200


def test_crlf_loose_match_still_works(env):
    (env.ws / "c.txt").write_bytes(b"one  \r\ntwo\r\nthree\r\n")
    out = edit(env, path="c.txt", old="one\ntwo", new="ONE\nTWO")
    assert out.startswith("edited")
    assert (env.ws / "c.txt").read_bytes() == b"ONE\r\nTWO\r\nthree\r\n"
