"""Hardening round 51: tools_researchnote (research_note, redact_text, learn) and tools_agenthome."""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from types import SimpleNamespace as S

import pytest

from avrone import bridge as bm
from avrone import tools_agenthome as ah
from avrone import tools_researchnote as rn


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------ redact_text
@pytest.mark.parametrize("sep", ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"])
def test_invisible_char_cannot_split_token(sep):
    tok = "ghp_" + "A1b2C3d4E5f6G7h8I9j0"
    split = tok[:6] + sep + tok[6:]
    out = rn.redact_text(f"use {split} here")
    assert "A1b2C3d4" not in out and rn.REDACTED in out


@pytest.mark.parametrize("url,leak", [
    ("https://user:p@ss@host.example/x", "ss"),
    ("https://u:a@b@c@host/x", "b@c"),
    ("postgres://admin:hunter2@db:5432/x", "hunter2"),
])
def test_userinfo_masked_to_last_at(url, leak):
    out = rn.redact_text(f"see {url} ok")
    assert leak not in out.split("://", 1)[1].split("@")[0] and "***@" in out
    assert "host" in out or "db" in out


def test_url_without_userinfo_untouched():
    s = "read https://example.com/a/b?q=1 and mail a@b.com"
    assert rn.redact_text(s) == s


def test_plain_text_kept():
    s = "numpy 2.0 broke the build; pin numpy<2"
    assert rn.redact_text(s) == s


# ------------------------------------------------------------ research_note
class FakeBridge:
    def __init__(self, exc=None):
        self.run_id, self.posts, self.exc = "r", [], exc

    def post(self, agent, kind, text, role=""):
        if self.exc:
            raise self.exc
        self.posts.append(text)
        return {"kind": kind, "id": "p1"}

    def __len__(self):
        return len(self.posts)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    b = FakeBridge()
    monkeypatch.setattr(bm, "for_core", lambda core, rid: b)
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    ctx = S(core=S(settings=S(data_dir=str(tmp_path))), run_id="r", name="W")
    return S(ctx=ctx, b=b, tmp=tmp_path)


def note(env, **a):
    return run(rn.t_research_note(env.ctx, a))


@pytest.mark.parametrize("t", ["\u200b", "\u200b\u200c \ufeff", "\u2060\n\u00ad"])
def test_invisible_only_text_refused(env, t):
    assert note(env, text=t).startswith("ERROR: text is required")
    assert env.b.posts == []


@pytest.mark.parametrize("topic", [{"a": 1}, ["x"], 5, True])
def test_non_text_topic_refused(env, topic):
    assert note(env, text="hello", topic=topic) == "ERROR: topic must be text"
    assert env.b.posts == []


@pytest.mark.parametrize("kind", [["finding"], 1, True, {"k": 1}])
def test_non_text_kind_refused(env, kind):
    assert note(env, text="hello", kind=kind).startswith("ERROR: kind must be")


def test_empty_kind_defaults(env):
    assert note(env, text="hello world", kind="").startswith("posted finding")


def test_topic_whitespace_and_invisibles_collapsed(env):
    out = note(env, text="hello", topic="a\u200b\n\n  b\u2028c")
    assert env.b.posts[0].startswith("[a b c] hello"), env.b.posts


def test_topic_secret_marks_masked(env):
    out = note(env, text="plain words", topic="ghp_A1b2C3d4E5f6G7h8I9j0")
    assert "secret-shaped tokens masked" in out
    assert "A1b2C3d4" not in env.b.posts[0]


def test_masking_growth_over_cap_refused_not_cut(env):
    # 2-char userinfo grows to "***@"; many of them push the masked note past the cap
    unit = "x://a@h "
    text = (unit * (rn.RESEARCH_NOTE_MAX_TEXT // len(unit)))[: rn.RESEARCH_NOTE_MAX_TEXT]
    out = note(env, text=text, kind="question")
    assert out.startswith("ERROR: after masking"), out
    assert env.b.posts == []


def test_oserror_from_bridge(env):
    env.b.exc = OSError("disk full")
    assert note(env, text="hello") == "ERROR: could not write the run bridge (OSError)"


def test_bridge_error_still_reported(env):
    env.b.exc = bm.BridgeError("nope")
    assert note(env, text="hello") == "ERROR: nope"


@pytest.mark.parametrize("exc", [ValueError("bad"), AttributeError("x"), TypeError("t")])
def test_corrupt_store_after_post_reports_not_raises(env, monkeypatch, exc):
    class St:
        def load(self, ns):
            raise exc
    monkeypatch.setattr(bm, "_store", lambda d: St())
    out = note(env, text="a finding worth keeping")
    assert out.startswith("posted finding") and f"not stored (refused: {type(exc).__name__})" in out
    assert len(env.b.posts) == 1


def test_learn_and_known_roundtrip(env):
    a = note(env, text="pin numpy below 2")
    b = note(env, text="pin   numpy below 2")
    assert "NEWLY LEARNED" in a and "already known" in b


def test_stored_lesson_has_no_invisibles(env):
    note(env, text="pin\u200b numpy")
    vals = [r["value"] for r in bm._store(str(env.tmp)).load(bm.LESSONS_NS).values()]
    assert vals == ["pin numpy"]


def test_question_not_learned(env):
    assert "a question is posted only" in note(env, text="why?", kind="question")


def test_surrogates_survive(env):
    out = note(env, text="bad \ud800 char")
    assert out.startswith("posted") and "\ud800" not in env.b.posts[0]


def test_real_bridge_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "off")
    ctx = S(core=S(settings=S(data_dir=str(tmp_path))), run_id="rx", name="W")
    out = run(rn.t_research_note(ctx, {"text": "line one\nline two", "topic": "t"}))
    assert out.startswith("posted finding") and "lessons: off" in out
    assert "\n".join(out.splitlines()[-1:]).startswith("note: [t] line one line two")


# ------------------------------------------------------------ agent_home
def ctx_for(tmp_path, aid="a1", fleet=None, runner=None):
    ws = tmp_path / "ws"
    data = tmp_path / "data"
    ws.mkdir(exist_ok=True)
    data.mkdir(exist_ok=True)
    return S(agent_id=aid, core=S(settings=S(workspace=str(ws), data_dir=str(data)), fleet=fleet, runner=runner))


def test_deep_home_no_recursion_error(tmp_path, monkeypatch):
    c = ctx_for(tmp_path)
    home = tmp_path / "ws" / "agents" / "a1"
    p = home
    for _ in range(150):
        p = p / "d"
    p.mkdir(parents=True)
    (p / "f").write_bytes(b"xyz")
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(120)  # os.walk recursed once per level
    try:
        r = ah.scan_home(home)
    finally:
        sys.setrecursionlimit(old)
    assert r["dirs"] == 150 and r["files"] == 1 and r["bytes"] == 3


def test_scan_never_follows_symlinked_dir(tmp_path):
    home = tmp_path / "h"
    (home / "sub").mkdir(parents=True)
    out = tmp_path / "outside"
    out.mkdir()
    (out / "big").write_bytes(b"x" * 1000)
    os.symlink(out, home / "sub" / "link")
    r = ah.scan_home(home)
    assert r["symlinks"] == 1 and r["bytes"] == 0


def test_scan_limit_and_special(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    for i in range(10):
        (home / f"f{i}").write_text("a")
    os.mkfifo(home / "fifo")
    assert ah.scan_home(home, limit=5)["truncated"] is True
    r = ah.scan_home(home)
    assert r["files"] == 10 and r["special"] == 1 and not r["truncated"]


def test_scan_unreadable_subdir_skipped(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    home = tmp_path / "h"
    (home / "locked").mkdir(parents=True)
    (home / "ok").write_text("ab")
    os.chmod(home / "locked", 0)
    try:
        r = ah.scan_home(home)
    finally:
        os.chmod(home / "locked", 0o755)
    assert r["bytes"] == 2 and r["dirs"] == 1


@pytest.mark.parametrize("v,want", [(math.inf, None), (-math.inf, None), (math.nan, None), (1.5, 1.5),
                                    (10**30, 10**30), (True, True), ("a\nb", "a?b")])
def test_clean(v, want):
    assert ah._clean(v) == want


class Pool:
    def __init__(self, homes, clock):
        self.homes, self.clock, self.node = homes, clock, None


@pytest.mark.parametrize("uses,last,clock", [
    ("abc", 0.0, lambda: 5.0), (math.inf, 0.0, lambda: 5.0), (1.5, 0.0, lambda: 5.0), (-3, 0.0, lambda: 5.0),
    (2, math.nan, lambda: 5.0), (2, 0.0, lambda: math.inf), (2, "x", lambda: 1.0), (True, 0.0, lambda: 1.0),
])
def test_odd_pool_record_json_valid(tmp_path, uses, last, clock):
    from avrone import agentcontainer
    name = agentcontainer.container_name(agentcontainer.local_node_id(), "a1")
    runner = S(kind="docker", agent_pool=Pool({name: S(uses=uses, last_used=last)}, clock))
    c = ctx_for(tmp_path, runner=runner)
    out = run(ah.t_agent_home(c, {"format": "json"}))
    rep = json.loads(out)  # strict: no NaN / Infinity
    live = rep["container"]["live"]
    assert live is not None
    assert live["uses"] is None or (isinstance(live["uses"], int) and live["uses"] >= 0)
    assert live["idle_s"] is None or math.isfinite(live["idle_s"])
    text = run(ah.t_agent_home(c, {}))
    assert "live(uses=" in text


def test_good_pool_record(tmp_path):
    from avrone import agentcontainer
    name = agentcontainer.container_name(agentcontainer.local_node_id(), "a1")
    runner = S(kind="docker", agent_pool=Pool({name: S(uses=3, last_used=1.0)}, lambda: 11.0))
    rep = ah.agent_home(ctx_for(tmp_path, runner=runner))
    assert rep["container"]["live"] == {"uses": 3, "idle_s": 10.0}


def test_node_id_with_newline_cannot_forge_lines(tmp_path):
    c = ctx_for(tmp_path, fleet=S(node_id="n1\nFAKE: line"))
    out = run(ah.t_agent_home(c, {}))
    assert not any(l.startswith("FAKE") for l in out.splitlines())


def test_long_node_id_still_born_here(tmp_path):
    nid = "n" * 150
    rep = ah.agent_home(ctx_for(tmp_path, fleet=S(node_id=nid)))
    assert rep["pack"]["born_here"] is True


@pytest.mark.parametrize("fmt", ["JSON", " json ", "Json"])
def test_json_format_spellings(tmp_path, fmt):
    json.loads(run(ah.t_agent_home(ctx_for(tmp_path), {"format": fmt})))


@pytest.mark.parametrize("fmt", [["json"], {"a": 1}, 5, None])
def test_non_string_format_is_text(tmp_path, fmt):
    out = run(ah.t_agent_home(ctx_for(tmp_path), {"format": fmt}))
    assert out.startswith("agent_home a1")


def test_json_over_cap_stays_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(ah, "OUT_MAX_CHARS", 300)
    out = run(ah.t_agent_home(ctx_for(tmp_path), {"format": "json"}))
    assert isinstance(json.loads(out), dict) and len(out) <= 300


def test_symlinked_home_refused(tmp_path):
    c = ctx_for(tmp_path)
    root = tmp_path / "ws" / "agents"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", root / "a1")
    out = run(ah.t_agent_home(c, {}))
    assert "home is a symlink" in out
