"""Round 148: errors that echo an argument show it escaped.

v0.140: read_file, list_files, search_files, edit_file, diff_files and the fs_* tools put the raw
path into "ERROR: <path> not found". search_files echoed the regex the same way, and evolve_cancel
echoed the loop id. A path holding "\\nFORGED ..." added a line of its own to the error, and U+202E
reversed the rest of the line. A probe (forgeprobe3.py) passes the same hostile string as every
string argument of every tool; nothing echoes it raw now.
"""
from __future__ import annotations

import re

import pytest

from tests.test_tools_extra import call, env  # noqa: F401  (fixture)

B = "q\nFORGED x \u202eR"
FORGED = re.compile(r"(^|\n)[^\n]{0,4}FORGED")


def _clean(out: str) -> None:
    assert not FORGED.search(out) and "\u202e" not in out, out


@pytest.mark.parametrize("tool,args", [
    ("read_file", {"path": B}),
    ("list_files", {"path": B}),
    ("search_files", {"pattern": "x", "path": B}),
    ("search_files", {"pattern": B}),
    ("edit_file", {"path": B, "old": "a", "new": "b"}),
    ("diff_files", {"path_a": B, "text_b": "x"}),
])
def test_errors_escape_echoed_args(env, tool, args):  # noqa: F811
    out = call(env, tool, **args)
    _clean(out)
    assert "q\\x0aFORGED" in out or "No matches" in out or "ERROR" in out


def test_evolve_error_escapes_loop_id(tmp_path):
    from avrone import evolve
    src = open(evolve.__file__).read()
    assert "no loop {loop_id}." not in src and "no loop {lid}." not in src
