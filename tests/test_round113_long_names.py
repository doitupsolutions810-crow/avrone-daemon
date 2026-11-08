"""Round 113: a file name near NAME_MAX (255 bytes) can be written and rewritten.

v0.105: every atomic writer named its temp ".<name>.XXXXXXXX.tmp", which is over NAME_MAX for a
legal name of 240+ bytes, so write_file / edit_file / fs_write on such a file failed with a raw
`OSError: [Errno 36] File name too long` carrying the host path. Found by an all-tools fuzz over
a workspace of hostile names (/workspace/work/fsfuzz.py): non-UTF-8 bytes, newlines, ANSI
escapes, RTL marks and 250-byte names; the long name was the only failure in 2,075 calls.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from avrone import agentfs_tools, safeio, tools

LONG = "x" * 250
LONG_UTF8 = "é" * 120 + "ab.txt"   # 246 bytes, multibyte cut point
LONG_BYTES = os.fsdecode(b"\xff" * 240 + b".bin")   # non-UTF-8


@pytest.mark.parametrize("name", [LONG, LONG_UTF8, LONG_BYTES])
def test_safeio_atomic_write_long_name(tmp_path, name):
    p = tmp_path / name
    safeio.atomic_write(p, "one")
    safeio.atomic_write(p, "two")
    assert p.read_text() == "two"
    assert os.listdir(tmp_path) == [name]  # no temp left behind


@pytest.mark.parametrize("name", [LONG, LONG_UTF8])
def test_tools_atomic_write_bytes_long_name(tmp_path, name):
    p = tmp_path / name
    tools.atomic_write_bytes(p, b"a")
    tools.atomic_write_bytes(p, b"b")
    assert p.read_bytes() == b"b" and os.listdir(tmp_path) == [name]


def test_quota_write_long_name(tmp_path):
    root = tmp_path / "agents" / "builder"
    root.mkdir(parents=True)
    p = root / LONG
    ctx = SimpleNamespace(core=SimpleNamespace(codex=None, settings=None), run_id="r", name="T")
    agentfs_tools.quota_write(ctx, root, p, b"one", False, "write_file")
    agentfs_tools.quota_write(ctx, root, p, b"two", False, "write_file")
    assert p.read_bytes() == b"two" and os.listdir(root) == [LONG]


def test_short_names_keep_old_temp_prefix():
    assert getattr(safeio, "tmp_prefix", lambda n: None)("fleet.json") == ".fleet.json."


def test_long_prefixes_distinct_and_bounded():
    tp = getattr(safeio, "tmp_prefix", None)
    if tp is None:
        pytest.fail("safeio.tmp_prefix missing")
    a, b = tp("x" * 250), tp("x" * 249 + "y")
    assert a != b
    for n in (LONG, LONG_UTF8, LONG_BYTES, "é" * 127):
        assert len(os.fsencode(tp(n))) + len(".XXXXXXXX.tmp") + 30 < 255
