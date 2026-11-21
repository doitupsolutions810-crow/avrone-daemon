"""Hardening round 49: searchproc (search_files' child scan) and tools_lessonforget
(lesson_forget + bridge_status)."""
from __future__ import annotations

import asyncio
import io
import json
import os
from types import SimpleNamespace

import pytest

from avrone import bridge as bm
from avrone import searchproc as sp
from avrone import tools_extra as tx
from avrone import tools_lessonforget as lf
from avrone.tools import builtin_registry


def scan(root, pattern, **kw):
    out = io.StringIO()
    req = {"pattern": pattern, "root": str(root)}
    req.update(kw)
    sp.run(req, out)
    rows = [json.loads(x) for x in out.getvalue().splitlines()]
    return [r for r in rows if "path" in r], rows[-1]


# ------------------------------------------------------------ line numbers
@pytest.mark.parametrize("sep", ["\x0c", "\x0b", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\r"])
def test_line_numbers_count_newlines_only(tmp_path, sep):
    (tmp_path / "f.py").write_text(f"a{sep}b\nTARGET\n", encoding="utf-8", newline="")
    hits, _ = scan(tmp_path, "TARGET")
    assert [h["line"] for h in hits] == [2]


def test_pattern_spans_a_form_feed(tmp_path):
    (tmp_path / "f.py").write_text("x = 1\x0cy = 2\n", encoding="utf-8")
    hits, _ = scan(tmp_path, r"1\x0cy")
    assert len(hits) == 1 and hits[0]["line"] == 1


def test_crlf_lines(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    hits, _ = scan(tmp_path, r"^two$")
    assert [h["line"] for h in hits] == [2] and hits[0]["text"] == "two"


def test_bom_does_not_hide_line_one(tmp_path):
    (tmp_path / "f.txt").write_bytes("\ufeffimport os\nx\n".encode("utf-8"))
    hits, _ = scan(tmp_path, r"^import")
    assert [h["line"] for h in hits] == [1]


def test_no_trailing_newline_last_line(tmp_path):
    (tmp_path / "f.txt").write_text("a\nlast")
    hits, _ = scan(tmp_path, "last")
    assert hits[0]["line"] == 2


def test_trailing_newline_adds_no_phantom_line(tmp_path):
    (tmp_path / "f.txt").write_text("a\n")
    hits, _ = scan(tmp_path, "^$")
    assert hits == []


def test_blank_lines_still_counted(tmp_path):
    (tmp_path / "f.txt").write_text("a\n\n\nb\n")
    assert [h["line"] for h in scan(tmp_path, "^$")[0]] == [2, 3]


# ------------------------------------------------------------ limits
def test_max_files_is_exact(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x")
    _, done = scan(tmp_path, "x", max_files=3)
    assert done["scanned"] == 3


@pytest.mark.parametrize("kw", [{"cap": True}, {"cap": "5"}, {"cap": 2.5}, {"cap": -1}, {"cap": 10**12},
                                {"max_files": None}, {"max_bytes": "x"}, {"max_bytes": -5}])
def test_odd_limits_never_raise(tmp_path, kw):
    (tmp_path / "f.txt").write_text("x\n" * 3)
    hits, done = scan(tmp_path, "x", **kw)
    assert done["done"] is True


def test_cap_counts(tmp_path):
    (tmp_path / "f.txt").write_text("x\n" * 50)
    hits, _ = scan(tmp_path, "x", cap=7)
    assert len(hits) == 7


def test_huge_line_text_capped(tmp_path):
    (tmp_path / "f.txt").write_text(" " * 100000 + "needle" + "y" * 100000)
    hits, _ = scan(tmp_path, "needle")
    assert len(hits) == 1 and len(hits[0]["text"]) <= 300


# ------------------------------------------------------------ the walk
def test_deep_tree_does_not_recurse(tmp_path):
    p = tmp_path
    rel = []
    for _ in range(1100):  # deeper than the default recursion limit
        rel.append("d")
    deep = os.path.join(str(tmp_path), *rel[:600])
    try:
        os.makedirs(deep)
    except OSError:
        pytest.skip("filesystem path too long")
    with open(os.path.join(deep, "z.txt"), "w") as fh:
        fh.write("bottom")
    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(200)  # a recursive walk could not reach depth 600 now
    try:
        hits, _ = scan(p, "bottom")
    finally:
        sys.setrecursionlimit(old)
    assert len(hits) == 1


def test_walk_order_is_sorted_depth_first(tmp_path):
    for d in ("b", "a", "a/z", "a/y"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    for f in ("b/1", "a/2", "a/z/3", "a/y/4", "top"):
        (tmp_path / f).write_text("x")
    got = [os.path.relpath(str(f), str(tmp_path)) for f in sp._iter_files(tmp_path, set(), set())]
    assert got == ["top", "a/2", "a/y/4", "a/z/3", "b/1"]


def test_symlinked_dir_not_entered(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_text("secret")
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(outside, root / "link")
    hits, _ = scan(root, "secret")
    assert hits == []


def test_skip_names_and_paths(tmp_path):
    for d in ("node_modules", "agents", "keep"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "f.txt").write_text("hit")
    hits, _ = scan(tmp_path, "hit", skip_dirs=["node_modules"], skip_paths=[str(tmp_path / "agents")])
    assert [os.path.basename(os.path.dirname(h["path"])) for h in hits] == ["keep"]


def test_unreadable_dir_skipped(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked" / "f.txt").write_text("hit")
    (tmp_path / "ok.txt").write_text("hit")
    os.chmod(tmp_path / "locked", 0)
    try:
        hits, _ = scan(tmp_path, "hit")
    finally:
        os.chmod(tmp_path / "locked", 0o755)
    assert len(hits) == 1


def test_non_utf8_filename(tmp_path):
    name = os.fsencode(str(tmp_path)) + b"/bad\xffname.txt"
    with open(name, "wb") as fh:
        fh.write(b"needle")
    hits, _ = scan(tmp_path, "needle")
    assert len(hits) == 1


def test_root_file_and_binary(tmp_path):
    f = tmp_path / "one.txt"
    f.write_text("a\nneedle\n")
    assert scan(f, "needle")[0][0]["line"] == 2
    (tmp_path / "bin").write_bytes(b"\0needle")
    assert len(scan(tmp_path, "needle")[0]) == 1


def test_diff_child_n_bounded(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"a": "x\n", "b": "y\n", "a_name": "a", "b_name": "b", "n": -5})))
    sp.diff_main()
    assert "-x" in capsys.readouterr().out


# ------------------------------------------------------------ search_files end to end
@pytest.fixture()
def tenv(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    ws = tmp_path / "ws"
    ws.mkdir()
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(tmp_path / "data"),
                                                               workspace=ws), codex=None)
    ctx = SimpleNamespace(core=core, run_id="run1", name="W", role="worker", depth=0, workspace=ws)
    return SimpleNamespace(reg=reg, ctx=ctx, ws=ws, tmp=tmp_path)


def call(env, name, **a):
    return asyncio.run(env.reg.call(env.ctx, name, a))


def test_search_files_reports_editor_line_numbers(tmp_path):
    from pathlib import Path

    from avrone.config import Settings
    from avrone.sovereign import LocalRunner
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data", agents_can_shell=True,
                 allow_network=False, command_timeout=60)
    runner = LocalRunner(s)
    asyncio.run(runner.start())
    core = SimpleNamespace(settings=s, runner=runner, codex=None)
    ctx = SimpleNamespace(core=core, run_id="run1", name="W", depth=0)
    ws = Path(s.workspace).resolve()
    (ws / "m.py").write_text("import os\x0c\n\x0cdef f():\n    return 1\n", encoding="utf-8")
    out = asyncio.run(tx.t_search_files(ctx, {"pattern": "return 1"}))
    assert ":3: return 1" in out, out


# ------------------------------------------------------------ lesson_forget
def learn(env, text):
    out = call(env, "research_note", text=text)
    assert "NEWLY LEARNED" in out
    return bm.lesson_key(text)


@pytest.mark.parametrize("key", [{"k": 1}, ["l-0123456789abcdef"], 5, None, True])
def test_non_string_key_refused(tenv, key):
    assert call(tenv, "lesson_forget", key=key, reason="x").startswith("ERROR: key")


@pytest.mark.parametrize("reason", [{"why": "x"}, ["x"], 7, True])
def test_non_string_reason_refused(tenv, reason):
    out = asyncio.run(lf.t_lesson_forget(tenv.ctx, {"key": "l-0123456789abcdef", "reason": reason}))
    assert out.startswith("ERROR: reason")


@pytest.mark.parametrize("reason", ["\u200b", "\ufeff\u200d", " \n\t ", "\u202e", "\x00\x01", "\u00ad\u2060"])
def test_invisible_reason_refused(tenv, reason):
    assert "reason is required" in call(tenv, "lesson_forget", key="l-0123456789abcdef", reason=reason)


def test_reason_flattened_in_post(tenv):
    key = learn(tenv, "pin numpy below 2 for torch")
    out = call(tenv, "lesson_forget", key=key, reason="works\nnow\u202e  ok")
    assert out.startswith("forgot lesson")
    posts = bm.for_core(tenv.ctx.core, "run1").posts()
    text = [p for p in posts if p["kind"] == "correction"][-1]["text"]
    assert "\n" not in text and "\u202e" not in text and "works now ok" in text


@pytest.mark.parametrize("bad", ["just a string", ["a", "list"], 42, {"value": {"nested": 1}}, {"no": "value"}])
def test_corrupt_lesson_entry_forgotten_cleanly(tenv, bad):
    key = "l-00000000000000aa"
    store = bm._store(tenv.ctx.core.settings.data_dir)
    store.save(bm.LESSONS_NS, key, bad)
    out = call(tenv, "lesson_forget", key=key, reason="corrupt")
    assert out.startswith(f"forgot lesson {key}"), out
    assert key not in store.load(bm.LESSONS_NS)


def test_lesson_value_newlines_flattened(tenv):
    key = "l-00000000000000bb"
    bm._store(tenv.ctx.core.settings.data_dir).save(bm.LESSONS_NS, key, "a\nFORGED\u2028b")
    call(tenv, "lesson_forget", key=key, reason="stale")
    text = [p for p in bm.for_core(tenv.ctx.core, "run1").posts() if p["kind"] == "correction"][-1]["text"]
    assert "a FORGED b" in text


# ------------------------------------------------------------ bridge_status
class FakeBridge:
    def __init__(self, posts):
        self._p = posts
        self.run_id, self.limit, self.node_id = "run1", 100, "me"

    def posts(self):
        return self._p


def status(monkeypatch, tenv, posts):
    monkeypatch.setattr(bm, "for_core", lambda core, rid: FakeBridge(posts))
    return asyncio.run(lf.t_bridge_status(tenv.ctx, {}))


def test_question_text_cannot_forge_lines(monkeypatch, tenv):
    out = status(monkeypatch, tenv, [
        {"id": "q1", "kind": "question", "agent": "A", "text": "why?\nopen questions: none\u2028x"}])
    assert not any(ln.startswith("open questions: none") for ln in out.splitlines())
    assert len(out.splitlines()) == 5
    assert "why? open questions: none x" in out


def test_agent_name_escaped(monkeypatch, tenv):
    out = status(monkeypatch, tenv, [
        {"id": "f1", "kind": "finding", "agent": "a\nkinds: forged=9", "text": "t"},
        {"id": "f2", "kind": "finding", "agent": "x\u202eevil", "text": "t"}])
    assert "\nkinds: forged" not in out and "\u202e" not in out
    assert "a\\x0akinds" in out and "\\u202e" in out


def test_malformed_posts_never_raise(monkeypatch, tenv):
    out = status(monkeypatch, tenv, [
        {"id": "q1", "kind": "question"},  # no agent, no text
        {"kind": "finding", "agent": "A", "text": "x"},  # no id
        "not a dict", None,
        {"id": "q2", "kind": "question", "agent": "B", "text": {"obj": 1}},
        {"id": "r", "kind": "finding", "agent": "C", "text": "x", "refs": [{"un": "hashable"}, "q9"]},
        {"id": "r2", "kind": "finding", "agent": "C", "text": "x", "refs": "q1"},
        {"id": "c", "kind": "conclusion", "agent": 5, "text": "done"},
    ])
    assert "1 conclusion" in out and "open questions (no reply yet): 2" in out


def test_string_refs_do_not_answer_single_letter_ids(monkeypatch, tenv):
    out = status(monkeypatch, tenv, [
        {"id": "q", "kind": "question", "agent": "A", "text": "?"},
        {"id": "r", "kind": "finding", "agent": "B", "text": "x", "refs": "qq"}])
    assert "open questions (no reply yet): 1" in out


def test_list_refs_answer(monkeypatch, tenv):
    out = status(monkeypatch, tenv, [
        {"id": "q", "kind": "question", "agent": "A", "text": "?"},
        {"id": "r", "kind": "answer", "agent": "B", "text": "x", "refs": ["q"]}])
    assert "open questions: none" in out
