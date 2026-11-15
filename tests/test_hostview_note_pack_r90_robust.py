"""Round 90: research_note redaction (all invisible format chars, fullwidth prefixes), hostview
token strength / btime / pid parsing, agentpack content_stamp clamp and incomplete imports."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import time
from pathlib import Path

import pytest

from avrone import agentpack, memstore
from avrone import tools_hostview as hv
from avrone.tools_researchnote import REDACTED, _INVISIBLE_RE, redact_text

TOK = "ghp_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7"
INVIS = ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad", "\u180e",  # old set
         "\u2061", "\u2062", "\u2063", "\u2064", "\u200e", "\u200f", "\u202a", "\u202c", "\u202e",
         "\u2066", "\u2069", "\ufe00", "\ufe0f", "\u034f", "\u180b", "\u180d", "\u3164", "\u115f",
         "\uffa0", "\u061c", "\U000e0001", "\U000e0041", "\u206f", "\u17b4"]


# ------------------------------------------------------------------ research_note redaction
@pytest.mark.parametrize("ch", INVIS)
def test_invisible_split_token_masked(ch):
    out = redact_text("key " + TOK[:4] + ch + TOK[4:])
    assert REDACTED in out and "aB3dE5fG7hJ9" not in out


@pytest.mark.parametrize("ch", INVIS)
@pytest.mark.parametrize("pos", [1, 8, 15])
def test_invisible_anywhere_in_token(ch, pos):
    out = redact_text(TOK[:pos] + ch + TOK[pos:])
    assert "aB3dE5fG7hJ9" not in out


@pytest.mark.parametrize("ch", INVIS)
def test_invisible_class_covers(ch):
    assert _INVISIBLE_RE.fullmatch(ch)


@pytest.mark.parametrize("ch", ["a", " ", "\n", "\u00e9", "\u65e5", "\u2028", "\U0001f600"])
def test_visible_chars_kept(ch):
    assert not _INVISIBLE_RE.fullmatch(ch)


@pytest.mark.parametrize("prefix", ["\uff47\uff48\uff50_", "\uff47hp_", "gh\uff50_"])
def test_fullwidth_prefix_masked(prefix):
    out = redact_text("token " + prefix + "aB3dE5fG7hJ9kL1mN3pQ5rS7")
    assert REDACTED in out and "aB3dE5fG7hJ9" not in out


def test_fullwidth_bearer_masked():
    out = redact_text("\uff22\uff45\uff41\uff52\uff45\uff52 abcdef123456")
    assert "abcdef123456" not in out


@pytest.mark.parametrize("text", ["\uff08\u65e5\u672c\u8a9e\uff09\u30c6\u30b9\u30c8",
                                  "\uff21\uff22\uff23 plain fullwidth words",
                                  "price \uff04\uff11\uff10\uff10"])
def test_fullwidth_prose_kept_verbatim(text):
    assert redact_text(text) == text


def test_plain_prose_unchanged():
    t = "The cache misses dropped after we pinned the index to 4 shards."
    assert redact_text(t) == t


def test_plain_token_still_masked():
    assert redact_text("use " + TOK) == "use " + REDACTED


def test_emoji_text_kept_minus_selectors():
    out = redact_text("ship it \U0001f680 done")
    assert out == "ship it \U0001f680 done"


def test_invisible_only_note_is_empty():
    assert not _INVISIBLE_RE.sub("", "\u2062\u200e\ufe0f\u3164").strip()


# ------------------------------------------------------------------ token strength
@pytest.mark.parametrize("tok", ['"changemechangeme"', "'secretsecretsecret'",
                                 "\uff43\uff48\uff41\uff4e\uff47\uff45\uff4d\uff45\uff43\uff48\uff41\uff4e\uff47\uff45\uff4d\uff45",
                                 "chan\u200bgemechangeme12", "(changeme)(changeme)", "[password][password]",
                                 "<token><token><token>", "admin,admin,admin;admin", "$$changeme$$changeme$$",
                                 "`passwordpassword`", "change\u2062me\u2062change\u2062me"])
def test_placeholder_shapes(tok):
    assert hv._token_strength(tok) == "placeholder"


@pytest.mark.parametrize("tok", ["Xk9#mPq2$vL7nR4w", "f3Kq9-ZpL2_wT8vR6mYc", "Hn4!Qz8&Lp2*Wx6^"])
def test_real_tokens_ok(tok):
    assert hv._token_strength(tok) == "ok"


@pytest.mark.parametrize("tok", ["\u200b\u200b\u200b", "\ufeff", "\u2062\u2063"])
def test_invisible_only_token_weak(tok):
    assert hv._token_strength(tok) == "weak"


@pytest.mark.parametrize("tok,want", [(None, "unset"), ("", "unset"), ("   ", "weak"), (5, "unknown"),
                                      ("short1", "weak")])
def test_token_strength_basics(tok, want):
    assert hv._token_strength(tok) == want


def test_fullwidth_strong_token_still_ok():
    t = "".join(chr(ord(c) + 0xFEE0) for c in "Xk9mPq2vL7nR4wZt")
    assert hv._token_strength(t) == "ok"


# ------------------------------------------------------------------ fake /proc
def _proc(tmp_path, stat_text="cpu 1 2 3\nbtime 1700000000\n", uptime="1000.0 900.0\n"):
    root = tmp_path / "proc"
    root.mkdir()
    (root / "stat").write_text(stat_text)
    (root / "uptime").write_text(uptime)
    return root


@pytest.mark.parametrize("stat,want", [
    ("btime 1700000000\n", 1700000000.0),
    ("cpu 1\nbtime 1700000000", 1700000000.0),
    ("btime\n", None),
    ("btime \n", None),
    ("btime nan\n", None),
    ("btime inf\n", None),
    ("btime -5\n", None),
    ("btime 1e9\n", None),
    ("btime \u0661\u0667\u0660\u0660\n", None),
    ("btime 99999999999999999999\n", None),
    ("nothing here\n", None),
    ("x\u2028btime 1700000000\n", None),
])
def test_boot_time(tmp_path, monkeypatch, stat, want):
    monkeypatch.setattr(hv, "PROC", _proc(tmp_path, stat_text=stat))
    assert hv._boot_time() == want


def test_boot_time_binary_file(tmp_path, monkeypatch):
    root = _proc(tmp_path)
    (root / "stat").write_bytes(b"\xff\xfe btime 1\n")
    monkeypatch.setattr(hv, "PROC", root)
    assert hv._boot_time() is None


def test_boot_time_missing(tmp_path, monkeypatch):
    root = _proc(tmp_path)
    (root / "stat").unlink()
    monkeypatch.setattr(hv, "PROC", root)
    assert hv._boot_time() is None


def _child(root, pid, ppid, cmd=b"sleep\x00100", state="S"):
    d = root / str(pid)
    d.mkdir()
    fields = [state, str(ppid)] + ["0"] * 17 + ["50", "0", "10"]
    (d / "stat").write_text(f"{pid} (sleep) " + " ".join(fields) + "\n")
    (d / "cmdline").write_bytes(cmd)


@pytest.mark.parametrize("name", ["\u00b2", "\u0661\u0662", "\uff11\uff12", "\u2463"])
def test_non_ascii_digit_dirs_ignored(tmp_path, monkeypatch, name):
    root = _proc(tmp_path)
    (root / name).mkdir()
    _child(root, 4242, os.getpid())
    monkeypatch.setattr(hv, "PROC", root)
    kids = hv._children(os.getpid())
    assert list(kids) == [4242]


def test_children_bare_btime_no_crash(tmp_path, monkeypatch):
    root = _proc(tmp_path, stat_text="btime\n")
    _child(root, 4243, os.getpid())
    monkeypatch.setattr(hv, "PROC", root)
    kids = hv._children(os.getpid())
    assert kids[4243]["started"] is None and kids[4243]["age"] is not None


def test_children_nan_btime_no_nan_start(tmp_path, monkeypatch):
    root = _proc(tmp_path, stat_text="btime nan\n")
    _child(root, 4244, os.getpid())
    monkeypatch.setattr(hv, "PROC", root)
    assert hv._children(os.getpid())[4244]["started"] is None


def test_proc_status_survives_bad_proc(tmp_path, monkeypatch):
    root = _proc(tmp_path, stat_text="btime\n")
    (root / "\u00b2").mkdir()
    _child(root, 4245, os.getpid())
    monkeypatch.setattr(hv, "PROC", root)
    out = asyncio.run(hv.t_proc_status(None, {"all": True}))
    assert out.startswith("proc_status: 1 child") and "4245" in out


# ------------------------------------------------------------------ agentpack content_stamp
def _home(tmp_path, name="a1"):
    ws, dd = tmp_path / "ws", tmp_path / "data"
    (ws / "agents" / name).mkdir(parents=True)
    dd.mkdir()
    (ws / "agents" / name / "f.txt").write_text("hi")
    return ws, dd


def _set_mem_ts(dd, ts):
    p = agentpack._memory_path(dd)
    rows = [json.loads(x) for x in p.read_text().split("\n") if x.strip()]
    rows[-1]["ts"] = ts
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.mark.parametrize("ts", [1e308, 2.0 ** 40, 9e15, 4102444800.0])
def test_stamp_future_memory_clamped(tmp_path, ts):
    ws, dd = _home(tmp_path)
    memstore.MemoryStore(agentpack._memory_path(dd)).save("a1", "k", "v")
    _set_mem_ts(dd, ts)
    now = time.time()
    assert agentpack.content_stamp(ws, dd, "a1", now=now) <= int(now)


def test_stamp_future_file_clamped(tmp_path):
    ws, dd = _home(tmp_path)
    f = ws / "agents" / "a1" / "f.txt"
    os.utime(f, (2 ** 33, 2 ** 33))
    now = time.time()
    assert agentpack.content_stamp(ws, dd, "a1", now=now) == int(now)


def test_stamp_negative_file_time(tmp_path):
    ws, dd = _home(tmp_path)
    f = ws / "agents" / "a1" / "f.txt"
    try:
        os.utime(f, (-100, -100))
    except (OSError, OverflowError):
        pytest.skip("filesystem refuses pre-1970 times")
    assert agentpack.content_stamp(ws, dd, "a1") == 0


def test_stamp_normal_values(tmp_path):
    ws, dd = _home(tmp_path)
    os.utime(ws / "agents" / "a1" / "f.txt", (1_600_000_000, 1_600_000_000))
    assert agentpack.content_stamp(ws, dd, "a1", now=1_700_000_000) == 1_600_000_000


@pytest.mark.parametrize("bad", ["abc", None, True, [1]])
def test_memory_records_bad_ts(tmp_path, bad):
    ws, dd = _home(tmp_path)
    memstore.MemoryStore(agentpack._memory_path(dd)).save("a1", "k", "v")
    _set_mem_ts(dd, bad)
    recs = agentpack._memory_records(dd, "a1")
    assert all(isinstance(r["ts"], float) for r in recs)


def _pack(tmp_path, created=None):
    src = tmp_path / "src"
    src.mkdir()
    ws, dd = _home(src)
    memstore.MemoryStore(agentpack._memory_path(dd)).save("a1", "k", "v")
    bundle, man = agentpack.export_pack(ws, dd, "a1", "nodea", created=created)
    return bundle, man


def test_future_memory_no_longer_pins_agent(tmp_path):
    bundle, _ = _pack(tmp_path, created=int(time.time()) + 5)
    dst = tmp_path / "dst"
    dst.mkdir()
    ws, dd = _home(dst)
    memstore.MemoryStore(agentpack._memory_path(dd)).save("a1", "k0", "v0")
    _set_mem_ts(dd, 1e308)
    agentpack.record(dd, "a1", home="nodea")
    res = agentpack.import_pack(ws, dd, bundle, "nodeb")
    assert res["agent_id"] == "a1"


def test_older_pack_still_refused(tmp_path):
    bundle, _ = _pack(tmp_path, created=1_000_000)
    dst = tmp_path / "dst"
    dst.mkdir()
    ws, dd = _home(dst)
    agentpack.record(dd, "a1", home="nodea")
    with pytest.raises(agentpack.PackError, match="not newer"):
        agentpack.import_pack(ws, dd, bundle, "nodeb")


def test_refusal_message_has_no_huge_number(tmp_path):
    bundle, _ = _pack(tmp_path, created=1_000_000)
    dst = tmp_path / "dst"
    dst.mkdir()
    ws, dd = _home(dst)
    memstore.MemoryStore(agentpack._memory_path(dd)).save("a1", "k0", "v0")
    _set_mem_ts(dd, 1e308)
    agentpack.record(dd, "a1", home="nodea")
    with pytest.raises(agentpack.PackError) as ei:
        agentpack.import_pack(ws, dd, bundle, "nodeb")
    assert len(str(ei.value)) < 300


# ------------------------------------------------------------------ incomplete imports
def _fresh_dst(tmp_path):
    ws, dd = tmp_path / "dws", tmp_path / "ddata"
    ws.mkdir()
    dd.mkdir()
    return ws, dd


def test_merge_oserror_reported_not_raised(tmp_path, monkeypatch):
    bundle, _ = _pack(tmp_path)
    ws, dd = _fresh_dst(tmp_path)

    def boom(self, *a, **k):
        raise OSError(errno.ENOSPC, "No space left", str(dd / "memory.jsonl"))
    monkeypatch.setattr(memstore.MemoryStore, "merge", boom)
    res = agentpack.import_pack(ws, dd, bundle, "nodeb")
    assert res["incomplete"] == {"memory": "OSError (ENOSPC)"}
    assert (ws / "agents" / "a1" / "f.txt").read_text() == "hi"
    assert str(dd) not in json.dumps(res)


def test_merge_refused_reported(tmp_path, monkeypatch):
    bundle, _ = _pack(tmp_path)
    ws, dd = _fresh_dst(tmp_path)

    def refuse(self, *a, **k):
        raise memstore.MemoryRefused("namespace full at /secret/path")
    monkeypatch.setattr(memstore.MemoryStore, "merge", refuse)
    res = agentpack.import_pack(ws, dd, bundle, "nodeb")
    assert res["incomplete"] == {"memory": "MemoryRefused"}
    assert "/secret/path" not in json.dumps(res)


def test_record_oserror_reported(tmp_path, monkeypatch):
    bundle, _ = _pack(tmp_path)
    ws, dd = _fresh_dst(tmp_path)

    def boom(*a, **k):
        raise PermissionError(errno.EACCES, "denied", "/x")
    monkeypatch.setattr(agentpack, "record", boom)
    res = agentpack.import_pack(ws, dd, bundle, "nodeb")
    assert res["incomplete"] == {"home_record": "PermissionError (EACCES)"}


def test_clean_import_has_no_incomplete(tmp_path):
    bundle, _ = _pack(tmp_path)
    ws, dd = _fresh_dst(tmp_path)
    res = agentpack.import_pack(ws, dd, bundle, "nodeb")
    assert "incomplete" not in res and res["memory"]["merged"] == 1


def test_local_home_skips_record(tmp_path, monkeypatch):
    bundle, _ = _pack(tmp_path)
    ws, dd = _fresh_dst(tmp_path)
    monkeypatch.setattr(agentpack, "record", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "x")))
    res = agentpack.import_pack(ws, dd, bundle, "nodea")  # home == local node: no record written
    assert "incomplete" not in res


@pytest.mark.parametrize("e,want", [(OSError(errno.ENOSPC, "x", "/p"), "OSError (ENOSPC)"),
                                    (OSError("no errno"), "OSError"),
                                    (ValueError("/secret"), "ValueError"),
                                    (FileNotFoundError(errno.ENOENT, "x"), "FileNotFoundError (ENOENT)")])
def test_why_errno_only(e, want):
    assert agentpack._why(e) == want


def test_pack_import_tool_reports_incomplete(tmp_path, monkeypatch):
    from avrone import agentpack_tools as at
    bundle, man = _pack(tmp_path)
    ws, dd = _fresh_dst(tmp_path)
    pd = agentpack.pack_dir(dd)
    pd.mkdir(parents=True, exist_ok=True)
    name = f"a1-{man['created']}.tar.gz"
    (pd / name).write_bytes(bundle)
    monkeypatch.setattr(at, "_agent", lambda ctx: "a1")
    monkeypatch.setattr(at, "_settings", lambda ctx: (ws, dd))
    monkeypatch.setattr(at, "_node", lambda ctx: "nodeb")
    seen = []
    monkeypatch.setattr(at, "_audit", lambda ctx, kind, **kw: seen.append(kind))

    def boom(self, *a, **k):
        raise OSError(errno.ENOSPC, "full", "/p")
    monkeypatch.setattr(memstore.MemoryStore, "merge", boom)
    out = asyncio.run(at.t_agent_pack_import(None, {"name": name}))
    assert "INCOMPLETE" in out and "ENOSPC" in out and "unchanged" not in out
    assert "agentpack.incomplete" in seen


# ------------------------------------------------------------------ env_report keeps emoji, still hides tokens
@pytest.mark.parametrize("value", ["\U0001f3f3\ufe0f\u200d\U0001f308 ok", "\u2764\ufe0f team", "\u0915\u094d\u200d\u0937",
                                   "rtl \u200fmark", "flag \U0001f3f4\U000e0067\U000e0062\U000e007f"])
def test_env_report_plain_unicode_shown(value):
    from avrone import tools_introspect as ti
    assert ti.redact_value("AVRONE_SOMETHING", value) == ti._textsafe.UNSAFE_ONE.sub("?", value)


@pytest.mark.parametrize("ch", ["\u2062", "\u200e", "\ufe0f", "\u3164", "\U000e0041"])
def test_env_report_split_token_hidden(ch):
    from avrone import tools_introspect as ti
    out = ti.redact_value("AVRONE_SOMETHING", "ghp_" + ch + "aB3dE5fG7hJ9kL1mN3pQ5rS7")
    assert out.startswith("***") and "aB3dE5" not in out
