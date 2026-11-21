"""Round 146: names in listings are shown escaped, so they can't forge rows.

v0.138: list_files, search_files, read_file's "is a directory" hint and proc_status printed file
names, matched lines and process argv raw. A file named "a\\nFORGED" (or a child process with that
argv) printed a line of its own that read as another entry, and U+202E reversed the rest of the
row (Trojan Source). Names now show each unsafe character as a visible escape (\\x0a, \\u202e).
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time

import pytest

from avrone import tools_hostview as hv
from tests.test_tools_extra import call, env  # noqa: F401  (fixture)

FORGED = re.compile(r"(^|\n)\s*FORGED")
UNSAFE = re.compile("[\u202e\r\x1b\u2028\x85]")


def test_textsafe_escape():
    from avrone import textsafe
    esc = getattr(textsafe, "escape", None)
    if esc is None:
        pytest.fail("textsafe.escape missing")
    assert esc("a\nb\u202ec") == "a\\x0ab\\u202ec"
    assert esc("x\ty", keep="\t") == "x\ty"
    assert esc("plain é 日本") == "plain é 日本"


def _plant(ws):
    for d in (ws, ws / "sub"):
        d.mkdir(parents=True, exist_ok=True)
        for n in ("a\nFORGED1", "b\u202eFORGED2", "c\rFORGED3", "d\x1b[31mX", "e\u2028FORGED5"):
            (d / f"{n}.txt").write_text("FORGEDCONTENT\u202e\n")


def test_list_files_escapes_names(env):  # noqa: F811
    _plant(env.ws)
    out = call(env, "list_files")
    assert not FORGED.search(out) and not UNSAFE.search(out)
    assert "a\\x0aFORGED1.txt" in out and "b\\u202eFORGED2.txt" in out


def test_search_files_escapes_names_and_lines(env):  # noqa: F811
    _plant(env.ws)
    (env.ws / "t.py").write_text("FORGEDCONTENT\ttab\n")
    out = call(env, "search_files", pattern="FORGEDCONTENT")
    assert not FORGED.search(out) and not UNSAFE.search(out)
    assert "t.py:1: FORGEDCONTENT\ttab" in out  # tabs in a matched line stay


def test_read_file_directory_hint_escapes(env):  # noqa: F811
    _plant(env.ws)
    out = call(env, "read_file", path="sub")
    assert not FORGED.search(out) and not UNSAFE.search(out)


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="Linux /proc only")
def test_proc_status_escapes_argv():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "x\nFORGED7 [R]", "\u202eY"])
    try:
        time.sleep(0.5)
        out = asyncio.run(hv.t_proc_status(None, {"all": True}))
        assert f"- {p.pid} [" in out
        assert not FORGED.search(out) and "\u202e" not in out
    finally:
        p.kill()
        p.wait()
