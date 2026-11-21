"""Hardening round 62: flake_probe line numbers and digests, bridge_status against odd posts."""
import asyncio
from types import SimpleNamespace as S

import pytest

from avrone import bridge as bm
from avrone import tools_flakeprobe as fp
from avrone import tools_lessonforget as lf


def run(c):
    return asyncio.run(c)


# ---- _first_diff_line splits on \n only --------------------------------------------------------

@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\r"])
def test_diff_line_ignores_non_newline_breaks(sep):
    a = f"a{sep}b\nc"
    b = f"a{sep}b\nd"
    assert fp._first_diff_line(a, b) == 2


@pytest.mark.parametrize("sep", ["\u2028", "\x0c", "\x1c"])
def test_diff_line_inside_one_line(sep):
    assert fp._first_diff_line(f"x{sep}y", f"x{sep}z") == 1


@pytest.mark.parametrize("a,b,want", [
    ("x\n", "x", 1), ("x", "x\n", 1), ("x\r\n", "x\n", 1), ("a\nb\n", "a\nb\nc", 3),
    ("a\nb", "a\nc", 2), ("", "x", 1), ("\n", "\n\n", 2), ("a\n\nb", "a\n\nc", 3),
])
def test_diff_line_classic_cases(a, b, want):
    assert fp._first_diff_line(a, b) == want
    assert fp._first_diff_line(b, a) == want


def test_diff_line_never_past_both_ends():
    for a, b in [("x", "x\n"), ("x\u2028", "x\u2028\n"), ("a\nb\n", "a\nb")]:
        n = fp._first_diff_line(a, b)
        assert 1 <= n <= max(len(fp._lines(a)), len(fp._lines(b)))


@pytest.mark.parametrize("s", ["", "\n", "a", "a\n", "a\nb", "a\nb\n", "\n\n", "a\u2028b"])
def test_lines_matches_splitlines_without_extra_breaks(s):
    assert fp._lines(s) == s.split("\n")[:-1] + ([s.split("\n")[-1]] if s.split("\n")[-1] else [])
    assert "".join(fp._lines(s, True)) == s


# ---- digests: lone surrogates stay apart -------------------------------------------------------

class Reg:
    def __init__(self, outs):
        self.outs = list(outs)

    async def call(self, ctx, name, args):
        return self.outs.pop(0)


@pytest.mark.parametrize("outs", [["a\ud800", "a\ud801"], ["\udcff", "\udcfe"], ["x\ud800y", "x?y"]])
def test_surrogates_distinct_and_differing_agree(outs):
    r = run(fp.flake_probe(None, Reg(outs), "t", 2, {}))
    assert r["differing_runs"] == 1 and r["distinct_outputs"] == 2


def test_digest_valid_text_unchanged():
    import hashlib
    assert fp._digest("héllo") == hashlib.sha256("héllo".encode()).hexdigest()[:12]


def test_same_surrogate_output_is_deterministic():
    r = run(fp.flake_probe(None, Reg(["a\ud800"] * 3), "t", 3, {}))
    assert r["differing_runs"] == 0 and r["distinct_outputs"] == 1


def test_diff_report_line_number_with_form_feed():
    r = run(fp.flake_probe(None, Reg(["p\x0cq\nA", "p\x0cq\nB"]), "t", 2, {}))
    assert r["diffs"] == [{"run": 2, "first_diff_line": 2}]


# ---- bridge_status --------------------------------------------------------------------------------

class FakeBridge:
    def __init__(self, posts, run_id="r1", limit=50, node_id="n0"):
        self._p, self.run_id, self.limit, self.node_id = posts, run_id, limit, node_id

    def posts(self):
        return self._p


@pytest.fixture
def bridge(monkeypatch):
    box = {}

    def make(posts, **kw):
        box["b"] = FakeBridge(posts, **kw)
        monkeypatch.setattr(bm, "for_core", lambda core, rid: box["b"])
        return run(lf.t_bridge_status(S(core=None, run_id="r1"), {}))
    return make


@pytest.mark.parametrize("qid", [["x"], {"a": 1}, {1, 2}, None, 7, 1.5])
def test_unhashable_or_odd_question_id(bridge, qid):
    out = bridge([{"kind": "question", "id": qid, "agent": "a", "text": "why?"},
                  {"kind": "note", "id": "n1", "agent": "b", "refs": ["q9"]}])
    assert "open questions (no reply yet): 1" in out and "why?" in out


def test_answered_question_not_open(bridge):
    out = bridge([{"kind": "question", "id": "q1", "agent": "a", "text": "why?"},
                  {"kind": "answer", "id": "a1", "agent": "b", "refs": ["q1"]}])
    assert "open questions: none" in out


def test_refs_string_does_not_answer_letters(bridge):
    out = bridge([{"kind": "question", "id": "q", "agent": "a", "text": "t"},
                  {"kind": "answer", "id": "a1", "agent": "b", "refs": "q"}])
    assert "open questions (no reply yet): 1" in out


@pytest.mark.parametrize("rid", ["r1\n- FORGED", "r1\r\nkinds: x=9", "r\u202e1", "r\x1b[31m1"])
def test_run_id_escaped_in_header(bridge, rid):
    for posts in ([], [{"kind": "note", "id": "n", "agent": "a"}]):
        out = bridge(posts, run_id=rid)
        first = out.split("\n")[0]
        assert first.startswith("bridge_status run ") and "\u202e" not in out and "\x1b" not in out
        assert len(out.split("\n")) == (1 if not posts else 4)


def test_odd_limit_not_printed_raw(bridge):
    out = bridge([], limit="5\n- FORGED")
    assert out.count("\n") == 0 and "\\x0a" in out


def test_int_limit_kept(bridge):
    assert "(ring holds 50)" in bridge([])


def test_many_question_ids_mixed(bridge):
    posts = [{"kind": "question", "id": [i], "agent": "a", "text": f"q{i}"} for i in range(10)]
    out = bridge(posts)
    assert "open questions (no reply yet): 10" in out and out.count("\n- [") == 5
