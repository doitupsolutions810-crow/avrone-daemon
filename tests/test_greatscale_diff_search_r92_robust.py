"""Round 92: greatscale counts are true integers and rare p is bounded; diff_files counts '+++x' and
'----' body lines; search_files shows the text around a match past column 300."""
from __future__ import annotations

import asyncio
import io
import json
import math
from fractions import Fraction
from types import SimpleNamespace

import pytest

from avrone import greatscale as g
from avrone import searchproc
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools_extra import t_diff_files, t_search_files


# ------------------------------------------------------------------ greatscale
@pytest.mark.parametrize("f,r", [(2.5, 10), (1, 10.9), ("3", 10), (1, "10"), (2.0, 10), (1, 10.0),
                                 (None, 10), (1, None), (Fraction(1, 2), 4), (1, [10]), (1, b"10")])
def test_snapshot_rejects_non_integer_counts(f, r):
    with pytest.raises(ValueError, match="integers"):
        g.evidence_snapshot(f, r)


@pytest.mark.parametrize("f,r", [(True, 10), (1, True), (False, 1)])
def test_snapshot_rejects_bools(f, r):
    with pytest.raises(ValueError):
        g.evidence_snapshot(f, r)


def test_snapshot_accepts_index_types():
    class Idx:
        def __index__(self):
            return 4
    s = g.evidence_snapshot(1, Idx())
    assert s["runs"] == 4 and s["p"] == 0.25 and type(s["runs"]) is int


@pytest.mark.parametrize("f,r", [(0, 1), (1, 1), (1, 2), (3, 7), (0, 10**9), (10**9, 10**9), (1, 10**9)])
def test_snapshot_valid_and_json_safe(f, r):
    s = g.evidence_snapshot(f, r)
    json.dumps(s, allow_nan=False)
    assert s["failures"] == f and s["runs"] == r


@pytest.mark.parametrize("f,r", [(-1, 10), (11, 10), (0, 0), (0, 10**9 + 1), (0, -5)])
def test_snapshot_range(f, r):
    with pytest.raises(ValueError):
        g.evidence_snapshot(f, r)


@pytest.mark.parametrize("p", [5e-324, 1e-320, 2.2250738585072014e-308 / 2**40])
def test_runs_to_detect_subnormal_p_is_value_error(p):
    with pytest.raises(ValueError, match="too small"):
        g.runs_to_detect(p)


def test_runs_to_detect_tiny_normal_p_is_int():
    n = g.runs_to_detect(1e-300)
    assert isinstance(n, int) and n > 10**299


@pytest.mark.parametrize("p,alpha", [(True, 0.05), (0.5, True), ("0.5", 0.05), (0.5, "0.05"), (None, 0.05),
                                     (0.5, None), (float("nan"), 0.05), (0.5, float("nan")), (-0.1, 0.05),
                                     (1.1, 0.05), (0.5, 0.0), (0.5, 1.0), (0.5, float("inf")), (10**400, 0.05),
                                     (0.5, 10**400), ([0.5], 0.05)])
def test_runs_to_detect_bad_args_value_error(p, alpha):
    with pytest.raises(ValueError):
        g.runs_to_detect(p, alpha)


@pytest.mark.parametrize("p,want", [(0, None), (1, 1), (0.5, 5), (Fraction(1, 2), 5), (1 / 200, 598)])
def test_runs_to_detect_known(p, want):
    assert g.runs_to_detect(p) == want


def test_runs_to_detect_tiny_alpha():
    n = g.runs_to_detect(0.5, 5e-324)
    assert n == math.ceil(math.log(5e-324) / math.log(0.5) - 1e-12)


def test_runs_to_detect_monotone_in_p():
    ps = [10.0 ** -k for k in range(1, 15)]
    ns = [g.runs_to_detect(p) for p in ps]
    assert ns == sorted(ns)


@pytest.mark.parametrize("args", [{"failures": 2.5, "runs": 10}, {"failures": "1", "runs": 10},
                                  {"failures": 1, "runs": 10.0}])
def test_tool_rejects_float_counts(args):
    out = asyncio.run(g.t_great_scale(None, args))
    assert out.startswith("ERROR")


def test_tool_snapshot_text_and_json():
    out = asyncio.run(g.t_great_scale(None, {"failures": 1, "runs": 200}))
    assert "runs for 95%: 598" in out
    js = json.loads(asyncio.run(g.t_great_scale(None, {"failures": 1, "runs": 200, "format": " JSON "})))
    assert js["runsFor95"] == 598


def test_tool_tiny_alpha_ok():
    out = asyncio.run(g.t_great_scale(None, {"failures": 1, "runs": 10**9, "alpha": 5e-324}))
    assert not out.startswith("ERROR") and "runs for 95%" in out


def test_evidence_pool_still_pools():
    from avrone.evidence_pool import pool_trials
    r = pool_trials([{"source": "a", "trial_id": str(i), "failed": i % 2 == 0} for i in range(10)])
    assert r["runs"] == 10 and r["failures"] == 5 and r["evidence"]["p"] == 0.5


# ------------------------------------------------------------------ diff summary
@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("AVRONE_AGENT_FS", raising=False)
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data")
    runner = LocalRunner(s)
    asyncio.run(runner.start())
    c = SimpleNamespace(core=SimpleNamespace(settings=s, runner=runner, codex=None), run_id="r", name="n",
                        depth=0, agent_id=None, agent_dir=None)
    c.ws = s.workspace
    return c


def _diff(ctx, a, b):
    (ctx.ws / "a.txt").write_text(a)
    return asyncio.run(t_diff_files(ctx, {"path_a": "a.txt", "text_b": b}))


def _summary(out):
    return out.rsplit("\n", 1)[-1]


@pytest.mark.parametrize("a,b,want", [
    ("---\na: 1\n", "a: 1\n", "[0 added, 1 removed]"),
    ("a: 1\n", "---\na: 1\n", "[1 added, 0 removed]"),
    ("x\n", "++x\n", "[1 added, 1 removed]"),
    ("-- comment\nselect 1;\n", "select 1;\n", "[0 added, 1 removed]"),
    ("--- \n+++ \nk\n", "k\n", "[0 added, 2 removed]"),
    ("k\n", "+++ b\n--- a\nk\n", "[2 added, 0 removed]"),
    ("same\n", "same\n", None),
    ("a\nb\nc\n", "a\nB\nc\n", "[1 added, 1 removed]"),
])
def test_diff_counts_body_lines(ctx, a, b, want):
    out = _diff(ctx, a, b)
    if want is None:
        assert out.startswith("No differences")
    else:
        assert _summary(out) == want


def test_diff_counts_match_body_marks(ctx):
    a = "".join(f"{'-' * (i % 5)}line{i}\n" for i in range(50))
    b = "".join(f"{'+' * (i % 4)}line{i}\n" for i in range(0, 50, 2))
    out = _diff(ctx, a, b)
    body = out.split("\n")[2:-1]
    plus = sum(1 for ln in body if ln.startswith("+"))
    minus = sum(1 for ln in body if ln.startswith("-"))
    assert _summary(out) == f"[{plus} added, {minus} removed]"


def test_diff_big_path_counts_too(ctx):
    a = "".join(f"---{i}\n" for i in range(9000))
    b = "".join(f"+++{i}\n" for i in range(9000))
    out = _diff(ctx, a, b)
    assert _summary(out) == "[9000 added, 9000 removed]"


def test_diff_no_newline_marker_counts(ctx):
    out = _diff(ctx, "--x", "++y")
    assert _summary(out) == "[1 added, 1 removed]"


# ------------------------------------------------------------------ search snippet
def test_snippet_short_line_unchanged():
    assert searchproc._snippet("   hello world  ", 3) == "hello world"


def test_snippet_early_match_keeps_head():
    line = "abc NEEDLE " + "z" * 1000
    s = searchproc._snippet(line, 4)
    assert s.startswith("abc NEEDLE") and len(s) == searchproc.SNIPPET_CHARS


@pytest.mark.parametrize("col", [300, 1000, 5000, 99_000])
def test_snippet_far_match_contains_needle(col):
    line = "x" * col + "NEEDLE" + "y" * 500
    s = searchproc._snippet(line, col)
    assert "NEEDLE" in s and s.startswith("...") and len(s) <= searchproc.SNIPPET_CHARS


def test_snippet_leading_whitespace_offset():
    line = " " * 50 + "x" * 600 + "NEEDLE"
    s = searchproc._snippet(line, 650)
    assert "NEEDLE" in s


def test_snippet_huge_line_is_cheap():
    import time
    line = "\t" * 4 + "a" * 20_000_000
    t = time.perf_counter()
    for _ in range(50):
        searchproc._snippet(line, 10_000_000)
    assert time.perf_counter() - t < 1.0


def test_snippet_all_whitespace():
    assert searchproc._snippet(" " * 1000, 500) == ""


def test_run_reports_far_match(tmp_path):
    f = tmp_path / "min.js"
    f.write_text("var a=1;" * 1000 + "SECRET_MARK" + ";b=2\n")
    out = io.StringIO()
    searchproc.run({"pattern": "SECRET_MARK", "root": str(f)}, out)
    hit = json.loads(out.getvalue().splitlines()[0])
    assert "SECRET_MARK" in hit["text"] and hit["line"] == 1


def test_search_tool_far_match(ctx):
    (ctx.ws / "m.txt").write_text("q" * 4000 + "FINDME\n")
    out = asyncio.run(t_search_files(ctx, {"pattern": "FINDME", "path": "."}))
    assert "FINDME" in out and out.startswith("m.txt:1: ...")
