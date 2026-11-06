"""Squad round 8 coverage lane, part A: codex_stats, memory_peek, agentfs, ctxspawn edge branches.

Offline only: every test works on tmp_path files and in-process objects.
"""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import agentfs, ctxspawn
from avrone import tools_codexstats as cs
from avrone import tools_extra as tx
from avrone import tools_mempeek as mp
from avrone.codex import Codex


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- codex_stats
@pytest.fixture()
def cctx(tmp_path):
    codex = Codex(tmp_path)
    core = SimpleNamespace(codex=codex, settings=SimpleNamespace())
    return SimpleNamespace(core=core, run_id="run1", name="Worker-1", depth=0, codex=codex)


def test_codex_iso_rejects_negative_nan_inf_and_garbage():
    assert cs._iso(-1) == "?"
    assert cs._iso(float("nan")) == "?"
    assert cs._iso(float("inf")) == "?"
    assert cs._iso("not-a-number") == "?"
    assert cs._iso(None) == "?"
    assert cs._iso(0) == "1970-01-01T00:00:00Z"


def test_codex_log_path_oserror_on_resolve_is_refused(cctx, monkeypatch):
    def boom(self, *a, **k):
        raise OSError("loop")
    monkeypatch.setattr(Path, "resolve", boom)
    assert cs._log_path(cctx) is None


def test_codex_log_path_rejects_dot_and_bad_run_ids(cctx):
    for rid in (".", "..", "a/b", "", "x" * 200):
        cctx.run_id = rid
        assert cs._log_path(cctx) is None
    cctx.core.codex = None
    cctx.run_id = "run1"
    assert cs._log_path(cctx) is None


def test_codex_read_tail_missing_file_returns_empty(tmp_path):
    assert cs._read_tail(tmp_path / "nope.jsonl") == ([], False)


def test_codex_read_tail_symlink_refused_by_nofollow(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_text('{"kind":"x"}\n')
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    if not getattr(os, "O_NOFOLLOW", 0):  # pragma: no cover - non-POSIX
        pytest.skip("no O_NOFOLLOW")
    assert cs._read_tail(link) == ([], False)


def test_codex_skips_blank_bad_json_non_dict_and_foreign_run(cctx):
    p = Path(cctx.core.codex.log_dir) / "run1.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "",
        "   ",
        "{not json",
        "[1, 2, 3]",
        json.dumps({"run_id": "other", "agent": "Worker-1", "kind": "tool.call", "ts": 1}),
        json.dumps({"run_id": "run1", "agent": "Worker-1", "kind": "tool.call", "ts": 2,
                    "data": {"tool": "shell"}}),
        json.dumps({"agent": "Worker-1", "kind": "note", "ts": 3}),  # no run_id -> treated as this run
    ]
    p.write_text("\n".join(lines) + "\n")
    rep = cs.codex_stats(cctx, "self", "", 10)
    assert rep["events"] == 2
    assert rep["by_kind"] == {"tool.call": 1, "note": 1}
    assert rep["tools_called"] == {"shell": 1}
    assert rep["first"] == cs._iso(2) and rep["last"] == cs._iso(3)


def test_codex_read_tail_truncation_drops_cut_first_line(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "CODEX_STATS_MAX_BYTES", 64)
    p = tmp_path / "r.jsonl"
    rec = json.dumps({"kind": "k", "ts": 1})
    p.write_text("\n".join([rec] * 20) + "\n")
    evs, trunc = cs._read_tail(p)
    assert trunc is True
    assert evs and all(e == {"kind": "k", "ts": 1} for e in evs)


def test_codex_int_parsing_rules():
    assert cs._int(None, 10, 0, 50) == 10
    assert cs._int("", 10, 0, 50) == 10
    assert cs._int(True, 10, 0, 50) is None
    assert cs._int("abc", 10, 0, 50) is None
    assert cs._int([1], 10, 0, 50) is None
    assert cs._int("999", 10, 0, 50) == 50
    assert cs._int(-5, 10, 0, 50) == 0


def test_codex_tool_rejects_non_integer_last(cctx):
    assert run(cs.t_codex_stats(cctx, {"last": "lots"})) == "ERROR: last must be an integer"
    assert run(cs.t_codex_stats(cctx, {"last": False})) == "ERROR: last must be an integer"


def test_codex_tool_missing_log_dir_reports_error(cctx):
    cctx.core.codex = None
    out = run(cs.t_codex_stats(cctx, {}))
    assert out.startswith("ERROR:") and "not available" in out


def test_codex_tool_last_zero_lists_no_recent(cctx):
    cctx.codex.emit("run1", "tool.call", "Worker-1", tool="shell", args={})
    out = run(cs.t_codex_stats(cctx, {"last": 0, "format": "json"}))
    rep = json.loads(out)
    assert rep["recent"] == [] and rep["events"] == 1


def test_codex_cap_truncates_long_text():
    s = "x" * (cs.CODEX_STATS_MAX_CHARS + 10)
    out = cs._cap(s)
    assert out.endswith("...[truncated]") and len(out) < len(s) + 20


# ----------------------------------------------------------------------------- memory_peek
def test_mempeek_secretish_glued_token_prefix():
    assert mp._secretish("prefixsk-abcdefghijklmnop") is True
    assert mp._secretish("xxghp_abcdefghijklmnopq") is True
    assert mp._secretish("plain words only") is False
    # long blob that is not mixed case + digit is fine
    assert mp._secretish("a" * 40) is False
    assert mp._secretish("Ab1" * 12) is True


def test_mempeek_iso_edges():
    assert mp._iso("garbage") == "?"
    assert mp._iso(None) == "?"
    assert mp._iso(0) == "?"
    assert mp._iso(-3) == "?"
    assert mp._iso(float("nan")) == "?"
    assert mp._iso(float("inf")) == "?"
    assert mp._iso(1) == "1970-01-01T00:00:01Z"


def test_mempeek_preview_hides_glued_token_under_innocent_key():
    out = mp._preview("note", "see zzsk-abcdefghijklmnopqrstu for access", 120)
    assert "abcdefghij" not in out
    assert "redacted" in out


def test_mempeek_refuses_non_local_namespace(tmp_path, monkeypatch):
    ctx = SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path))),
                          name="Worker-1", run_id="r")
    monkeypatch.setattr(tx, "_mem_ns", lambda c: "fleet:shared")
    with pytest.raises(PermissionError):
        mp.memory_peek(ctx)


# ----------------------------------------------------------------------------- agentfs
def test_agentfs_escape_detected_when_sanitizer_is_subverted(tmp_path, monkeypatch):
    monkeypatch.setattr(agentfs, "sanitize_agent_id", lambda raw: "../outside")
    with pytest.raises(agentfs.AgentFSError, match="escapes"):
        agentfs.agent_dir(tmp_path, "x", create=False)


def test_agentfs_root_symlink_and_dir_symlink_refused(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / agentfs.AGENTS_SUBDIR).symlink_to(other)
    with pytest.raises(agentfs.AgentFSError, match="root"):
        agentfs.agent_dir(ws, "a")
    ws2 = tmp_path / "ws2"
    (ws2 / agentfs.AGENTS_SUBDIR).mkdir(parents=True)
    (ws2 / agentfs.AGENTS_SUBDIR / "bob").symlink_to(other)
    with pytest.raises(agentfs.AgentFSError, match="symlink"):
        agentfs.agent_dir(ws2, "bob")


def test_agentfs_is_agent_dir_resolve_oserror(tmp_path, monkeypatch):
    def boom(self, *a, **k):
        raise OSError("loop")
    monkeypatch.setattr(Path, "resolve", boom)
    assert agentfs.is_agent_dir(tmp_path, tmp_path / "agents" / "a") is False


def test_agentfs_is_agent_dir_true_false(tmp_path):
    d = agentfs.agent_dir(tmp_path, "Alice")
    assert agentfs.is_agent_dir(tmp_path, d) is True
    assert agentfs.is_agent_dir(tmp_path, None) is False
    assert agentfs.is_agent_dir(tmp_path, tmp_path) is False
    assert agentfs.is_agent_dir(tmp_path, d / "sub") is False


def test_agentfs_confine_refuses_nul_absolute_dotdot(tmp_path):
    base = tmp_path / "b"
    base.mkdir()
    with pytest.raises(agentfs.AgentFSError, match="NUL"):
        agentfs.confine(base, "a\x00b")
    for bad in ("/etc/passwd", "\\x", "~/x", "C:foo"):
        with pytest.raises(agentfs.AgentFSError, match="absolute"):
            agentfs.confine(base, bad)
    with pytest.raises(agentfs.AgentFSError, match=r"'\.\.'"):
        agentfs.confine(base, "a/../../b")
    assert agentfs.confine(base, None) == base.resolve()
    assert agentfs.confine(base, "./") == base.resolve()
    assert agentfs.confine(base, "x/y.txt") == base.resolve() / "x" / "y.txt"


def test_agentfs_confine_symlink_escape(tmp_path):
    base = tmp_path / "b"
    base.mkdir()
    (base / "out").symlink_to(tmp_path)
    with pytest.raises(agentfs.AgentFSError, match="escapes"):
        agentfs.confine(base, "out/secret")


# ----------------------------------------------------------------------------- ctxspawn
def test_ctxspawn_new_ring_is_bounded():
    r = ctxspawn.new_ring()
    for i in range(ctxspawn.RECENT_CALLS + 5):
        r.append(i)
    assert len(r) == ctxspawn.RECENT_CALLS and r[0] == 5


def test_ctxspawn_env_int_clamps_and_ignores_garbage(monkeypatch):
    monkeypatch.setenv("AVRONE_SPAWN_CONTEXT_CHARS", "nope")
    assert ctxspawn.context_chars() == ctxspawn.DEFAULT_CONTEXT_CHARS
    monkeypatch.setenv("AVRONE_SPAWN_CONTEXT_CHARS", "5")
    assert ctxspawn.context_chars() == ctxspawn.MIN_CONTEXT_CHARS
    monkeypatch.setenv("AVRONE_SPAWN_CONTEXT_CHARS", "99999999")
    assert ctxspawn.context_chars() == ctxspawn.HARD_CONTEXT_CHARS
    monkeypatch.setenv("AVRONE_MAX_AGENTS_PER_RUN", "0")
    assert ctxspawn.max_agents_per_run() == 1
    monkeypatch.setenv("AVRONE_MAX_AGENTS_PER_RUN", "   ")
    assert ctxspawn.max_agents_per_run() == ctxspawn.DEFAULT_MAX_AGENTS


def test_ctxspawn_redact_survives_env_lookup_failure(monkeypatch):
    def boom():
        raise RuntimeError("env broken")
    monkeypatch.setattr(ctxspawn, "_env_secret_values", boom)
    out = ctxspawn.redact("Authorization: Bearer abcdefghijklmnop and sk-abcdefghijklmnopqrstuv")
    assert "abcdefghijklmnop" not in out and ctxspawn.REDACTED in out
    assert ctxspawn.redact(None) == ""


def test_ctxspawn_redact_masks_env_secret_values(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "zz-very-literal-value-42")
    out = ctxspawn.redact("value is zz-very-literal-value-42 here")
    assert "zz-very-literal-value-42" not in out


def test_ctxspawn_memory_best_effort_on_failure(monkeypatch):
    def boom(ctx):
        raise RuntimeError("no store")
    monkeypatch.setattr(tx, "_mem_file", boom)
    assert ctxspawn._memory(SimpleNamespace(name="a")) == {}


def test_ctxspawn_dir_files_oserror_and_cap(tmp_path, monkeypatch):
    d = tmp_path / "ad"
    d.mkdir()
    for i in range(ctxspawn.DIR_FILES + 3):
        (d / f"f{i:03d}.txt").write_text("x")
    names = ctxspawn._dir_files(SimpleNamespace(agent_dir=d))
    assert len(names) == ctxspawn.DIR_FILES + 1 and names[-1] == "..."
    assert ctxspawn._dir_files(SimpleNamespace(agent_dir=None)) == []

    def boom(*a, **k):  # round 79: _dir_files walks with os.scandir instead of Path.rglob
        raise OSError("gone")
    monkeypatch.setattr(ctxspawn.os, "scandir", boom)
    assert ctxspawn._dir_files(SimpleNamespace(agent_dir=d)) == []


def test_ctxspawn_build_pack_none_mode_is_bare():
    ctx = SimpleNamespace(run_task="do it", name="P", lineage=["Root"])
    pack = ctxspawn.build_pack(ctx, "none")
    assert pack.sections == [] and pack.lineage == ["Root", "P"]
    assert pack.render() == ""


def test_ctxspawn_bool_parsing():
    assert ctxspawn._bool(True) is True and ctxspawn._bool(False) is False
    for v in ("1", "true", " YES ", "on"):
        assert ctxspawn._bool(v) is True
    for v in ("0", "no", "", None, "maybe"):
        assert ctxspawn._bool(v) is False


def test_ctxspawn_spawn_share_dir_string_and_bad_mode(tmp_path):
    seen = {}

    async def run_agent(**kw):
        seen.update(kw)
        return "child-done"
    core = SimpleNamespace(run_agents={}, run_agent=run_agent, squad_roles={})
    ctx = SimpleNamespace(core=core, run_id="r", name="P", depth=0, agent_dir=tmp_path,
                          run_task="t", lineage=[])
    assert run(ctxspawn.spawn(ctx, {"task": "x", "context": "none", "share_dir": "yes"})) == "child-done"
    assert seen["parent_dir"] == tmp_path and seen["context_pack"] == ""
    run(ctxspawn.spawn(ctx, {"task": "x", "context": "none", "share_dir": "off"}))
    assert seen["parent_dir"] is None
    out = run(ctxspawn.spawn(ctx, {"task": "x", "context": "weird"}))
    assert out.startswith("ERROR: context must be one of")
