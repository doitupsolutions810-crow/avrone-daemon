"""Hardening round 44: trailing-newline ids (every ^...$ validator), blackboard field hygiene."""
from __future__ import annotations

import os
import pathlib
import re
import types

import pytest

from avrone import fusion_blackboard as fb


def _core(d):
    return types.SimpleNamespace(settings=types.SimpleNamespace(data_dir=str(d)))


def _post(**kw):
    kw.setdefault("text", "x")
    p, err = fb.normalize_blackboard_post(kw)
    assert err is None, err
    return p


# ------------------------------------------------------------ trailing newline ids
@pytest.mark.parametrize("bad", ["p1\n", "p1\n\n", "p1\r", "\np1", "p 1", "p1\x00"])
def test_post_id_with_newline_rejected(bad):
    p, err = fb.normalize_blackboard_post({"text": "x", "id": bad})
    assert p is None and err == "bad post id"


def test_newline_id_cannot_duplicate_a_stored_post(tmp_path):
    core = _core(tmp_path)
    assert fb.blackboard_accept(core, _post(id="p1")) == (False, "p1")
    with pytest.raises(Exception):
        fb.blackboard_accept(core, {"id": "p1\n", "text": "x", "kind": "finding"})
    assert [p["id"] for p in fb.blackboard_posts(core)] == ["p1"]


def test_newline_refs_dropped():
    p = _post(refs=["a\n", "b", "c\n\n"])
    assert p["refs"] == ["a", "b", "c"]  # refs are trimmed before the check, never stored with "\n"


# validators across avrone that used `$` (matches before a final "\n")
def _validators():
    from avrone import agentkit, agentpack_tools, bridge, checkpoint, evolve, fleet, gateway, memstore, netdiag, recon
    from avrone import tools_lessonforget
    return [
        (agentkit.PKG_RE, "requests"), (agentkit.SKILL_NAME_RE, "skill-a"),
        (agentpack_tools.PACK_NAME_RE, "pack-12.tar.gz"), (bridge._ID_RE, "p1"),
        (checkpoint.CKPT_ID_RE, "ck1"), (checkpoint._KEY_RE, "k1"), (evolve.SKILL_NAME_RE, "sk"),
        (fleet.ID_RE, "node-1"), (fleet.TS_RE, "1700000000"), (fleet.NONCE_RE, "abc"),
        (fleet.SIG_RE, "a" * 64), (gateway._NAME_OK, "name"), (memstore.NS_RE, "ns"),
        (netdiag.HOST_RE, "example.com"), (recon._LABEL, "www"), (recon._TLD, "com"),
        (tools_lessonforget.KEY_RE, "l-" + "0" * 16),
    ]


@pytest.mark.parametrize("idx", range(17))
@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\n\n"])
def test_validators_refuse_trailing_newline(idx, suffix):
    rx, good = _validators()[idx]
    assert rx.match(good), (rx.pattern, good)
    assert not rx.match(good + suffix), rx.pattern
    assert not rx.match(suffix + good)


def test_inline_validators_refuse_trailing_newline(tmp_path):
    src = (pathlib.Path(fb.__file__).parent / "checkpoint.py").read_text()
    assert r"{0,62}\Z" in src
    src = (pathlib.Path(fb.__file__).parent / "agentpack.py").read_text()
    assert r"{0,62}\Z" in src


def test_fleet_node_id_with_newline_refused():
    from avrone import fleet
    assert fleet.ID_RE.match("node-1") and not fleet.ID_RE.match("node-1\n")


def test_memstore_namespace_with_newline_refused():
    from avrone import memstore
    assert not memstore.NS_RE.match("ns\n")


def test_no_dollar_anchored_validators_left():
    """A whole-string validator must end in \\Z: `$` also matches just before a trailing newline,
    so "id\\n" passed and became a second id, file name or node. (?m) patterns are line scans."""
    pat = re.compile(r"""re\.(?:compile|match|search|fullmatch)\(\s*r?(['"])(\^.*?)\1""")
    hits = []
    root = pathlib.Path(fb.__file__).parent
    for p in sorted(root.glob("*.py")):
        for i, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
            for m in pat.finditer(line):
                body = m.group(2)
                if body.endswith("$") and not body.endswith("\\$") and "(?m)" not in body:
                    hits.append(f"{p.name}:{i}")
    assert not hits, f"use \\Z instead of $ in: {hits}"


# ------------------------------------------------------------ text
@pytest.mark.parametrize("bad", [{"a": 1}, ["x"], 5, 1.5, True])
def test_non_string_text_rejected(bad):
    p, err = fb.normalize_blackboard_post({"text": bad})
    assert p is None and err == "text must be a string"


def test_text_stored_as_written_and_display_flattens():
    """Contract (rounds 136/fusion_peer): the board keeps characters, displays flatten them."""
    from avrone.bridge import compact
    raw = "a\x1b[2Jb\r\nc\u2028d\u202ee"
    p = _post(text=raw)
    assert p["text"] == raw
    shown = compact([p])
    assert "\x1b" not in shown and "\u202e" not in shown and "\n" not in shown


def test_text_only_whitespace_rejected():
    p, err = fb.normalize_blackboard_post({"text": " \r\n\t "})
    assert p is None and err == "text is required"


def test_text_secret_still_redacted():
    t = _post(text="token=ghp_" + "A" * 36)["text"]
    assert "A" * 36 not in t


# ------------------------------------------------------------ labels
@pytest.mark.parametrize("field,default", [("agent", "agent"), ("role", ""), ("origin_node", "local")])
@pytest.mark.parametrize("bad", [{"k": "v"}, ["a"], 5, True, "   ", "\n\t"])
def test_label_non_string_or_blank_defaults(field, default, bad):
    assert _post(**{field: bad})[field] == default


@pytest.mark.parametrize("field,limit", [("agent", 80), ("role", 60), ("origin_node", 80)])
def test_label_trimmed_and_capped(field, limit):
    v = _post(**{field: "  lead" + "z" * 300})[field]
    assert v.startswith("lead") and len(v) <= limit


# ------------------------------------------------------------ run id
@pytest.mark.parametrize("raw,want", [("  ", "run"), (" r1 ", "r1"), ("", "run")])
def test_run_id_trimmed(raw, want):
    assert _post(run_id=raw)["run_id"] == want


def test_run_id_non_string_rejected():
    assert fb.normalize_blackboard_post({"text": "x", "run_id": 5}) == (None, "bad run id")


# ------------------------------------------------------------ order file
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads mode-000 files")
def test_unreadable_order_file_rebuilds(tmp_path):
    core = _core(tmp_path)
    fb.blackboard_accept(core, _post(id="a1", ts=2.0))
    fb.blackboard_accept(core, _post(id="a0", ts=1.0))
    order = tmp_path / "bridge" / "_fanin_order.json"
    os.chmod(order, 0)
    try:
        fresh = _core(tmp_path)
        assert [p["id"] for p in fb.blackboard_posts(fresh)] == ["a0", "a1"]
    finally:
        os.chmod(order, 0o644)


def test_order_path_is_directory(tmp_path):
    (tmp_path / "bridge" / "_fanin_order.json").mkdir(parents=True)
    core = _core(tmp_path)
    fb.blackboard_accept(core, _post(id="b1"))
    assert [p["id"] for p in fb.blackboard_posts(core)] == ["b1"]


def test_order_file_garbage(tmp_path):
    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "_fanin_order.json").write_bytes(b"\xff\xfe{not json")
    core = _core(tmp_path)
    fb.blackboard_accept(core, _post(id="c1"))
    assert [p["id"] for p in fb.blackboard_posts(core)] == ["c1"]


def test_order_file_newline_ids_ignored(tmp_path):
    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "_fanin_order.json").write_text('{"ids": ["x\\n", "c1"]}')
    core = _core(tmp_path)
    fb.blackboard_accept(core, _post(id="c1"))
    assert "x\n" not in fb._load_order(core)


def test_since_cursor_still_works(tmp_path):
    core = _core(tmp_path)
    for i in range(3):
        fb.blackboard_accept(core, _post(id=f"s{i}", ts=float(i + 1)))
    assert [p["id"] for p in fb.blackboard_posts(core, since="s0")] == ["s1", "s2"]
