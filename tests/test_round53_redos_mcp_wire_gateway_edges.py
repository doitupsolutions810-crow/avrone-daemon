"""Round 53: catastrophic regexes, MCP wire encoding, fallback specs, python/evolve/git inputs.

  * search_files: an agent's `(a+)+b` against a line of a's held the event loop for ~17 s (32 a's:
    minutes) and froze every agent, the API and heartbeats on the node. The scan now runs in a child
    process with a deadline (avrone.searchproc) and the loop keeps running;
  * MCP tools/call: NaN/inf or a lone surrogate in a model's tool arguments raised a raw ValueError /
    UnicodeEncodeError over HTTP and sent a bare NaN token (invalid JSON-RPC) over stdio;
  * AVRONE_LLM_FALLBACKS: model ids with NUL/newlines/5000 chars, duplicates and hundreds of specs
    each became an upstream;
  * python: a surrogate in code raised after the snippet file was created (leaked);
  * evolve_propose: edits=[1] was found only after cloning the repo (~16 s, a branch left behind);
  * git: a 1 MB args string was split and handed to git.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import AgentContext, TrinityCore


class _LLM:
    async def chat(self, messages, **kw):
        return '{"final": "ok"}'


def _core(tmp_path):
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"
    core = TrinityCore(s, _LLM(), LocalRunner(s), Codex(s.data_dir))
    from avrone.tools_extra import register_extra_tools
    register_extra_tools(core.tools)
    return core


def _ctx(core):
    return AgentContext(core=core, run_id="r53", name="Fuzzer", depth=0)


# ------------------------------------------------------------------ search_files ReDoS
def test_redos_pattern_does_not_freeze_the_event_loop(tmp_path, monkeypatch):
    from avrone import tools_extra
    monkeypatch.setattr(tools_extra, "SEARCH_TIMEOUT_S", 3.0)
    core = _core(tmp_path)
    ws = Path(core.settings.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "evil.txt").write_text("a" * 26 + "\n")  # ~4 s of backtracking in-process
    (ws / "ok.txt").write_text("needle here\n")

    async def go():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1
        t = asyncio.create_task(ticker())
        t0 = time.monotonic()
        out = await core.tools.call(_ctx(core), "search_files", {"pattern": "(a+)+b"})
        dt = time.monotonic() - t0
        t.cancel()
        return out, dt, ticks
    out, dt, ticks = asyncio.run(go())
    assert ticks >= 10, f"event loop was blocked ({ticks} ticks in {dt:.1f}s)"
    assert dt < 10
    assert "No matches" in out or "stopped after" in out


def test_redos_timeout_reports_and_keeps_partial_hits(tmp_path, monkeypatch):
    from avrone import tools_extra
    monkeypatch.setattr(tools_extra, "SEARCH_TIMEOUT_S", 1.5)
    core = _core(tmp_path)
    ws = Path(core.settings.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "a_first.txt").write_text("b\n")  # matches (a+)+b|b quickly
    (ws / "b_evil.txt").write_text("a" * 40 + "\n")
    t0 = time.monotonic()
    out = asyncio.run(core.tools.call(_ctx(core), "search_files", {"pattern": "(a+)+c|^b$"}))
    assert time.monotonic() - t0 < 8
    assert "stopped after" in out and "a_first.txt:1" in out


def test_search_files_still_finds_and_caps(tmp_path):
    core = _core(tmp_path)
    ws = Path(core.settings.workspace)
    (ws / "sub").mkdir(parents=True, exist_ok=True)
    (ws / "sub" / "x.py").write_text("\n".join(f"hit {i}" for i in range(300)))
    (ws / ".git").mkdir()
    (ws / ".git" / "y").write_text("hit secret")
    out = asyncio.run(core.tools.call(_ctx(core), "search_files", {"pattern": r"hit \d+"}))
    assert "sub/x.py:1: hit 0" in out and "capped at 200" in out
    assert ".git" not in out
    out = asyncio.run(core.tools.call(_ctx(core), "search_files", {"pattern": "[[x"}))
    assert out.startswith("ERROR: bad regex") or "No matches" in out
    out = asyncio.run(core.tools.call(_ctx(core), "search_files", {"pattern": "("}))
    assert out.startswith("ERROR: bad regex")
    out = asyncio.run(core.tools.call(_ctx(core), "search_files", {"pattern": "x" * 3000}))
    assert out.startswith("ERROR") and "2000" in out


# ------------------------------------------------------------------ MCP wire encoding
def test_wire_safe_makes_strict_utf8_json():
    from avrone.mcp_client import wire_safe
    v = wire_safe({"a": float("nan"), "b": [float("inf"), (1, 2), {3}], "\ud800k": "x\udfffy", "n": 10 ** 30})
    data = json.dumps(v, allow_nan=False, ensure_ascii=False).encode("utf-8")
    back = json.loads(data)
    assert back["a"] is None and back["b"][0] is None and back["\ufffdk"] == "x\ufffdy"
    assert back["n"] == 10 ** 30


STRICT_SERVER = textwrap.dedent(r'''
    import json, sys
    def strict(x):
        raise ValueError("non-JSON constant " + x)
    def send(o):
        sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line, parse_constant=strict)
        except ValueError as e:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(e)}})
            continue
        if "id" not in m:
            continue
        mid, meth, p = m["id"], m["method"], m.get("params") or {}
        if meth == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": p["protocolVersion"],
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "strict", "version": "1"}}})
        elif meth == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}})
        elif meth == "tools/call":
            send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text",
                  "text": json.dumps(p.get("arguments"), sort_keys=True)}]}})
''')


def test_mcp_stdio_call_with_nan_and_surrogate_args(tmp_path):
    from avrone.mcp_client import MCPClient
    script = tmp_path / "strict.py"
    script.write_text(STRICT_SERVER)

    async def go():
        c = MCPClient("strict", {"command": sys.executable, "args": [str(script)]}, timeout=10)
        await c.connect()
        try:
            return await c.call_tool("echo", {"x": float("nan"), "s": "a\ud800b", "l": [float("-inf")]})
        finally:
            await c.close()
    out = asyncio.run(go())
    assert json.loads(out) == {"l": [None], "s": "a\ufffdb", "x": None}


def test_mcp_http_call_with_nan_args_is_encodable():
    """Over HTTP, httpx serializes with allow_nan=False and UTF-8: the args must survive that."""
    import httpx
    from avrone.mcp_client import wire_safe
    req = httpx.Request("POST", "http://127.0.0.1:9/mcp",
                        json={"params": {"arguments": wire_safe({"x": float("nan"), "s": "\ud800"})}})
    assert b"null" in req.content and b"\xef\xbf\xbd" in req.content


# ------------------------------------------------------------------ fallback specs
def test_fallback_specs_reject_junk_model_ids_dupes_and_floods():
    from avrone import gateway
    st = SimpleNamespace(provider="mock", llm_base_url="", llm_model="m", llm_api_key="", lead_model="")
    env = {"AVRONE_LLM_FALLBACKS": ",".join([
        "ollama:bad\x00model", "ollama:new\nline", "ollama:" + "a" * 5000, "ollama:ok|lead\x01",
        "ollama:qwen2.5:3b", "ollama:qwen2.5:3b", "ollama:has space"])}
    ups = gateway.parse_upstreams(st, env)
    assert [u.model for u in ups[1:]] == ["qwen2.5:3b"]
    env = {"AVRONE_LLM_FALLBACKS": ",".join(f"ollama:m{i}" for i in range(500))}
    ups = gateway.parse_upstreams(st, env)
    assert len(ups) - 1 <= gateway.MAX_FALLBACKS + 1


# ------------------------------------------------------------------ python / evolve / git
def test_python_surrogate_code_does_not_leak_snippet(tmp_path):
    core = _core(tmp_path)
    out = asyncio.run(core.tools.call(_ctx(core), "python", {"code": "print('x\ud800y')"}))
    assert "UnicodeEncodeError" not in out, out
    leftovers = list(Path(core.settings.workspace).rglob("*snippet_*.py"))
    assert leftovers == []


def test_python_non_ascii_code_runs(tmp_path):
    core = _core(tmp_path)
    out = asyncio.run(core.tools.call(_ctx(core), "python", {"code": "print('caf\u00e9 \u2713')"}))
    assert "caf\u00e9" in out


@pytest.mark.parametrize("edits", [[1], [{"content": "x"}], [{"path": 5, "content": "x"}],
                                   [{"path": "a.py"}], [{"path": "a.py", "content": None}]])
def test_evolve_propose_refuses_malformed_edits_before_cloning(tmp_path, edits, monkeypatch):
    from avrone import evolve
    called = []

    async def no_clone(*a, **k):
        called.append(1)
        raise AssertionError("cloned for a malformed edit list")
    monkeypatch.setattr(evolve, "_ensure_worktree", no_clone)
    core = _core(tmp_path)
    r = asyncio.run(evolve._propose_once(_ctx(core), "goal", edits))
    assert r["passed"] is False and r["error"] and not called


def test_git_huge_args_refused(tmp_path):
    from avrone import tools_extra
    core = _core(tmp_path)
    t0 = time.monotonic()
    out = asyncio.run(core.tools.call(_ctx(core), "git", {"args": "log " + "z" * 1_000_000}))
    assert out.startswith("ERROR") and str(tools_extra.MAX_GIT_ARGS_CHARS) in out
    assert time.monotonic() - t0 < 2
