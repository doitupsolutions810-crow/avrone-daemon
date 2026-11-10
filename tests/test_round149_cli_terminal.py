"""Round 149: the CLI never writes raw controls to the operator's terminal.

v0.141: avrone-cli's live event lines replaced only "\\n". ESC sequences in a tool's output, a
model's role name or an error message reached stderr as they were: clear screen, OSC 0 window
title, OSC 52 clipboard writes, plus "\\r" line overwrites and U+202E. `avrone-cli runs`
printed HTTP clients' prompts raw, `tools` printed MCP servers' descriptions raw, and `evolve`
printed model-written goals raw. The final answer is escaped too when stdout is a terminal.
"""
from __future__ import annotations

import re

import pytest

from avrone import cli
from avrone.codex import Event

BAD = "ok\x1b]52;c;ZXZpbA==\x07 \x1b[2J\rFAKE\u202eR\nNEXT\x85\u2028"
CTRL = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


@pytest.mark.parametrize("kind,data,agent", [
    ("tool.result", {"output": BAD}, "a"),
    ("agent.done", {"result": BAD}, "a"),
    ("agent.spawn", {"role": BAD, "parent": BAD}, "a"),
    ("lead.planned", {"subtasks": [{"role": BAD}]}, "a"),
    ("error", {"message": BAD}, "a"),
    ("tool.call", {"tool": BAD, "args": {"x": 1}}, "a"),
    ("weird.kind", {"x": BAD}, BAD),
    ("run.start", {"mode": BAD}, "a"),
])
def test_event_lines_have_no_controls(kind, data, agent):
    line = cli._event_line(Event(run_id="r1", kind=kind, agent=agent, data=data))
    assert not CTRL.search(line), repr(line)


def test_one_line_helper():
    one = getattr(cli, "_one", None)
    if one is None:
        pytest.fail("cli._one missing")
    assert not CTRL.search(one(BAD))
    assert "FAKE" in one(BAD) and "NEXT" in one(BAD)


def test_hud_agents_map_has_no_prototype():
    from pathlib import Path
    html = (Path(cli.__file__).parent / "static" / "index.html").read_text()
    assert "const agents = Object.create(null)" in html
