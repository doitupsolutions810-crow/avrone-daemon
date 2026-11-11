"""Round 158: secret_scan fix=true in scope data scrubs bridge journals.

Bridge journals written before v0.147 hold posts unredacted, and v0.150 could only report them:
fix was refused outside scope runs. The fix now rewrites a flagged journal value by value (valid
JSONL, non-UTF-8 bytes kept) under the bridge's own locks, and reloads the open Bridge, whose
ring still served the raw text.
"""
from __future__ import annotations

import asyncio
import json
import types

import pytest

from avrone import bridge as br
from avrone import tools_secrets as ts


def _tok() -> str:  # built at run time: no token-shaped literal for push protection
    return "gh" + "p_" + "A" * 36


def _setup(tmp_path):
    b = br.Bridge("run-1", tmp_path)
    rec = {"id": "abcdef123456", "run_id": "run-1", "agent": "a", "role": "", "kind": "finding",
           "text": "config token " + _tok(), "refs": [], "ts": 1.0, "origin_node": "x"}
    with open(b.path, "ab") as f:
        f.write((json.dumps(rec) + "\n").encode() + b"\xff\xfe junk\n")
    b._load()
    core = types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=tmp_path), runner=None, _bridges={"run-1": b})
    return b, types.SimpleNamespace(core=core)


def test_data_fix_scrubs_journal_and_open_bridge(tmp_path):
    b, ctx = _setup(tmp_path)
    assert _tok() in b.read()[0]["text"]
    out = asyncio.run(ts.t_secret_scan(ctx, {"scope": "data", "fix": True}))
    assert "1 bridge journal(s)" in out and "0 line(s) still flagged" in out
    raw = b.path.read_bytes()
    assert _tok().encode() not in raw and b"\xff\xfe junk" in raw
    assert _tok() not in b.read()[0]["text"]
    assert br.Bridge("run-1", tmp_path).read()[0]["id"] == "abcdef123456"  # still a valid post


def test_fix_journal_clean_is_noop(tmp_path):
    fj = getattr(br, "fix_journal", None)
    if fj is None:
        pytest.fail("bridge.fix_journal missing")
    b = br.Bridge("run-2", tmp_path)
    b.post("a", "finding", "nothing secret here")
    before = b.path.read_bytes()
    assert fj(b.path) is False and b.path.read_bytes() == before


def test_fix_journal_refuses_symlink(tmp_path):
    fj = getattr(br, "fix_journal", None)
    if fj is None:
        pytest.fail("bridge.fix_journal missing")
    target = tmp_path / "outside.jsonl"
    target.write_text(json.dumps({"text": "token " + _tok()}) + "\n")
    (tmp_path / "bridge").mkdir()
    link = tmp_path / "bridge" / "x.jsonl"
    link.symlink_to(target)
    assert fj(link) is False and _tok() in target.read_text()


def test_workspace_fix_still_refused(tmp_path):
    _b, ctx = _setup(tmp_path)
    assert asyncio.run(ts.t_secret_scan(ctx, {"scope": "workspace", "fix": True})).startswith("ERROR")
