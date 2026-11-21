"""Round 91: logsafe redacts templates and bytes args; fsunify folds HFS+-ignorable code points;
search_files escapes a child crash line."""
from __future__ import annotations

import asyncio
import io
import logging
import sys
from types import SimpleNamespace

import pytest

from avrone import agentfs, fsunify, logsafe
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner

TOK = "ghp_aB3dE5fG7hJ9kL1mN3pQ5rS7xx"


# ------------------------------------------------------------------ logsafe
@pytest.fixture()
def lg():
    logsafe.install()
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    log = logging.getLogger("avrone.r91test")
    log.addHandler(h)
    old = log.level
    log.setLevel(logging.DEBUG)
    log.propagate = False
    yield log, buf
    log.removeHandler(h)
    log.setLevel(old)


def _out(buf):
    return buf.getvalue()


@pytest.mark.parametrize("tmpl,args,leak", [
    ("peer used password=hunter2 for %s", ("node-a",), "hunter2"),
    ("Authorization: Bearer abcdefghijklmnop %d", (5,), "abcdefghijklmnop"),
    ("using " + TOK + " for %s", ("x",), TOK),
    ("url https://u:pw12345@h/x %s", ("y",), "pw12345"),
    ("password=%s", ("hunter2",), "hunter2"),
    ("api_key: %s", ("zzTopSecret99",), "zzTopSecret99"),
])
def test_secret_in_template_with_args_masked(lg, tmpl, args, leak):
    log, buf = lg
    log.warning(tmpl, *args)
    assert leak not in _out(buf)


def test_template_args_still_rendered(lg):
    log, buf = lg
    log.warning("peer used password=hunter2 for %s (%d tries)", "node-a", 3)
    out = _out(buf)
    assert "node-a" in out and "3 tries" in out and "hunter2" not in out


def test_template_with_percent_literal(lg):
    log, buf = lg
    log.warning("100%% done password=x1y2z3 %s", "n")
    out = _out(buf)
    assert out.strip() == "100% done password=[REDACTED] n"


def test_broken_template_with_secret_masked_no_crash(lg, capsys):
    log, buf = lg
    log.warning("password=hunter2 %d %d", 5)
    out = _out(buf)
    assert "hunter2" not in out and "%%" not in out
    assert "Logging error" not in capsys.readouterr().err


def test_dict_args_template_secret(lg):
    log, buf = lg
    log.warning("token=abcdef123456XYZ %(a)s", {"a": "fine"})
    out = _out(buf)
    assert "abcdef123456XYZ" not in out and "fine" in out


def test_args_newline_still_escaped_after_template_redaction(lg):
    log, buf = lg
    log.warning("password=hunter2 %s", "a\nWARNING avrone.fleet: trusted")
    out = _out(buf)
    assert out.count("\n") == 1 and "hunter2" not in out


def test_template_unsafe_still_escaped(lg):
    log, buf = lg
    log.warning("peer \x1b[31m password=hunter2 %s", "n")
    out = _out(buf)
    assert "\x1b" not in out and "hunter2" not in out


def test_plain_template_unchanged(lg):
    log, buf = lg
    log.info("ran %d step(s) in %.1fs\n\tok %s", 3, 1.25, "done")
    assert _out(buf) == "ran 3 step(s) in 1.2s\n\tok done\n"


@pytest.mark.parametrize("val", [TOK.encode(), bytearray(TOK.encode()), memoryview(TOK.encode()),
                                 b"password=hunter2", b"Bearer abcdefghijklmnop"])
def test_bytes_args_redacted(lg, val):
    log, buf = lg
    log.warning("stderr: %s", val)
    out = _out(buf)
    assert "hunter2" not in out and "aB3dE5fG7h" not in out and "abcdefghijklmnop" not in out
    assert out.startswith("stderr: b'")


def test_bytes_plain_repr_kept(lg):
    log, buf = lg
    log.warning("stderr: %s %r", b"hello\nworld", b"x")
    assert _out(buf) == "stderr: b'hello\\nworld' b'x'\n"


def test_bytes_controls_stay_escaped(lg):
    log, buf = lg
    log.warning("e %s", b"\x1b[2J\r")
    out = _out(buf)
    assert "\x1b" not in out and "\r" not in out


def test_numbers_untouched(lg):
    log, buf = lg
    from decimal import Decimal
    log.warning("%d %.2f %s %s", 7, 2.5, Decimal("1.10"), None)
    assert _out(buf) == "7 2.50 1.10 None\n"


def test_non_avrone_logger_untouched():
    logsafe.install()
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    log = logging.getLogger("other.r91")
    log.addHandler(h)
    log.propagate = False
    log.warning("password=hunter2 %s", b"x")
    log.removeHandler(h)
    assert "hunter2" in buf.getvalue()


def test_redact_template_direct():
    rec = logging.LogRecord("avrone.x", 30, __file__, 1, "password=hunter2 %s", ("a",), None)
    out = logsafe._redact_template(rec.msg, rec)
    assert "hunter2" not in out and rec.args == () and out.endswith(" a")


def test_redact_template_noop_keeps_args():
    rec = logging.LogRecord("avrone.x", 30, __file__, 1, "plain %s", ("a",), None)
    assert logsafe._redact_template(rec.msg, rec) == "plain %s" and rec.args == ("a",)


def test_redact_template_huge_line_capped(monkeypatch):
    monkeypatch.setenv("AVRONE_LOG_ARG_MAX_CHARS", "256")
    rec = logging.LogRecord("avrone.x", 30, __file__, 1, "password=hunter2 %s", ("z" * 100_000,), None)
    rec.args = logsafe.clean_args(rec.args)
    out = logsafe._redact_template(rec.msg, rec)
    assert len(out) < 4096 and "hunter2" not in out


# ------------------------------------------------------------------ fsunify _fold
@pytest.mark.parametrize("name", ["agents", "Agents", "AGENTS", "agents.", "agents ", "agents::$DATA",
                                  "agen\u200cts", "agen\u200dts", "\ufeffagents", "agents\u200e",
                                  "a\u202agents", "ag\u206aents", "ag\u206fents", "agents\u200b",
                                  "ag\u2060ents", "AGEN\u200cTS."])
def test_fold_matches_agents(name):
    assert fsunify._fold(name) == "agents"


@pytest.mark.parametrize("name", ["agent", "agentsx", "my-agents", "agents2", "\u0430gents"])
def test_fold_other_names(name):
    assert fsunify._fold(name) != "agents"


def test_fold_nfc():
    assert fsunify._fold("cafe\u0301") == fsunify._fold("caf\u00e9")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in ("AVRONE_AGENT_FS_QUOTA_MB", "AVRONE_AGENT_FS_MAX_FILE_MB", "AVRONE_AGENT_FS",
              "AVRONE_AGENT_DOCKER_ISOLATE"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data")
    runner = LocalRunner(s)
    asyncio.run(runner.start())
    core = SimpleNamespace(settings=s, runner=runner, codex=Codex(s.data_dir))
    d = agentfs.agent_dir(s.workspace, "alpha")
    ctx = SimpleNamespace(core=core, run_id="run1", name="Alpha-1", depth=0, agent_id="alpha", agent_dir=d)
    return SimpleNamespace(ctx=ctx, ws=s.workspace.resolve())


@pytest.mark.parametrize("ch", ["\u200c", "\u200d", "\ufeff", "\u206a", "\u200b", "\u2060"])
def test_shared_agents_ignorable_refused(env, ch):
    with pytest.raises(agentfs.AgentFSError, match="off limits"):
        fsunify.resolve(env.ctx, f"shared/agen{ch}ts/beta/secret.txt", "read_file")


def test_shared_other_dirs_allowed(env):
    r = fsunify.resolve(env.ctx, "shared/reports/a.txt", "read_file")
    assert r.path == env.ws / "reports" / "a.txt"


def test_shared_agents_plain_still_refused(env):
    with pytest.raises(agentfs.AgentFSError):
        fsunify.resolve(env.ctx, "shared/agents/beta/x", "write_file")


def test_touches_agents_variants(env):
    for v in ("agen\u200cts", "Agents", "agents."):
        assert fsunify._touches_agents(env.ws, env.ws / v / "beta")
    assert not fsunify._touches_agents(env.ws, env.ws / "agentsx" / "beta")


# ------------------------------------------------------------------ search_files crash line
def test_search_failure_line_escaped(env, monkeypatch):
    from avrone import tools_extra as te
    (env.ws / "f.txt").write_text("x\n")
    legacy = SimpleNamespace(core=env.ctx.core, run_id="run1", name="L", depth=0, agent_id=None, agent_dir=None)
    real = asyncio.create_subprocess_exec

    async def fake(*a, **k):
        code = "import sys; sys.stdin.read(); sys.stderr.write('boom \\x1b[31m\\u202e evil\\n'); sys.exit(3)"
        return await real(sys.executable, "-c", code, **k)
    monkeypatch.setattr(te.asyncio, "create_subprocess_exec", fake)
    out = asyncio.run(te.t_search_files(legacy, {"pattern": "x", "path": "."}))
    assert out.startswith("ERROR: search failed: boom")
    assert "\x1b" not in out and "\u202e" not in out
