"""Round 157: secret_scan reads whole run logs, numbers lines like fix does, and escapes paths.

On v0.149:
- scan() read the first 2 MiB of every file and reported the rest CLEAN. Run logs rotate at
  64 MiB, so a secret logged after the first 2 MiB was never flagged, and `fix` (which rewrites
  only flagged logs) never removed it.
- Lines were split with splitlines(), which also splits on U+2028, \\x0b, \\x1c and others, so the
  reported line number did not match the file (fix splits on "\\n").
- Hit paths were printed raw, so a run log with a newline in its name forged report lines.
- A file read only in part was not reported as such.
"""
from __future__ import annotations

import asyncio
import json
import types

from avrone import tools_secrets as ts


def _tok() -> str:  # built at run time: no token-shaped literal for push protection to flag
    return "gh" + "p_" + "A" * 36


def test_secret_after_two_mib_is_found_and_fixed(tmp_path):
    pad = json.dumps({"kind": "x", "text": "a" * 1000}) + "\n"
    p = tmp_path / "run1.jsonl"
    with open(p, "w") as f:
        for _ in range(3000):
            f.write(pad)
        f.write(json.dumps({"text": "token " + _tok()}) + "\n")
    assert p.stat().st_size > 2 * 1024 * 1024
    r = ts.scan(tmp_path)
    assert [(h["path"], h["line"]) for h in r["hits"]] == [("run1.jsonl", 3001)]
    assert ts.fix_runs(tmp_path, {"run1.jsonl"}) == 1
    assert ts.scan(tmp_path)["hits"] == []
    assert _tok() not in p.read_text()


def test_long_line_and_piece_boundary(tmp_path):
    (tmp_path / "a.jsonl").write_text("a" * (5 * 1024 * 1024) + " " + _tok() + " b\nok\n")
    (tmp_path / "b.jsonl").write_text("a" * (ts.LINE_CHUNK - 10) + " " + _tok() + "\n")
    got = {(h["path"], h["line"]) for h in ts.scan(tmp_path)["hits"]}
    assert got == {("a.jsonl", 1), ("b.jsonl", 1)}


def test_line_numbers_split_on_newline_only(tmp_path):
    (tmp_path / "r.jsonl").write_text("x\u2028y\x0bz\x1cw\n" + json.dumps({"t": _tok()}) + "\n")
    assert [h["line"] for h in ts.scan(tmp_path)["hits"]] == [2]


def test_partial_file_reported(tmp_path):
    (tmp_path / "big.txt").write_text(("x" * 100_000 + "\n") * 30 + _tok() + "\n")
    r = ts.scan(tmp_path)
    assert r.get("partial") == ["big.txt"]


def _ctx(d):
    return types.SimpleNamespace(core=types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=d), runner=None))


def test_hostile_path_escaped(tmp_path):
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / "z\n- q.txt").write_text(_tok())
    out = asyncio.run(ts.t_secret_scan(_ctx(tmp_path), {"scope": "runs"}))
    assert "\n- q.txt" not in out and "z\\x0a- q.txt:1" in out


def test_partial_named_in_report(tmp_path):
    (tmp_path / "big.txt").write_text(("x" * 100_000 + "\n") * 30)
    out = asyncio.run(ts.t_secret_scan(_ctx(tmp_path), {"scope": "data"}))
    assert "only partly read" in out and "big.txt" in out
