"""Round 56: hostile stdio MCP servers, memory store stalls, fleet memory merge rewrites.

  * StdioTransport._send awaited stdin.drain() with no deadline: a server that stops reading its
    input (2 MB tool args) hung the call FOREVER, past the request timeout, holding the write lock so
    every later call on that server hung too. Now the send counts against the request timeout; a
    half-written line drops the connection; a call queued behind another write fails, nothing sent;
  * stderr was line-iterated: the first line over the stream limit (32 MB) raised inside a
    suppress() and the drain stopped for good -> the pipe filled -> the server blocked on its next
    stderr write and the connection hung. Whole lines (a 12 MB one) were kept and quoted into the
    "process exited" error. Now stderr is read in chunks, lines clipped to STDERR_LINE_CHARS;
  * a request after the server closed stdout now fails at once instead of waiting out its timeout;
  * fleet receive_memory merged each namespace separately: every one re-read and rewrote the whole
    store (32 namespaces x 200k lines: 24 s, on the event loop). MemoryStore.merge_many does one
    read + one rewrite (0.76 s), and /api/fleet/memory runs it in a worker thread;
  * memory_save and lesson_forget read the whole store (and took its file lock) on the loop
    -> off_loop; memory_sync reads via to_thread (200k lines: 0.85 s stall -> 0.13 s).
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import memstore
from avrone.mcp_client import MCPClient, MCPError

SERVER = textwrap.dedent('''
    import sys, json, os, time
    mode = sys.argv[1]
    def out(o): sys.stdout.write(json.dumps(o) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        if m["method"] == "initialize":
            if mode == "stderr2":
                sys.stderr.write("E" * 40_000_000); sys.stderr.write("\\n")
                for _ in range(40):
                    sys.stderr.write("L" * 100_000 + "\\n")
                sys.stderr.flush()
            out({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": "2025-06-18",
                 "capabilities": {}, "serverInfo": {"name": "x"}}})
            if mode == "noread":
                time.sleep(3600)
            continue
        if m["method"] == "tools/call":
            if mode == "dieerr":
                sys.stderr.write("boom" * 3_000_000 + "\\n"); sys.stderr.flush(); sys.exit(1)
            out({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": "ok"}]}})
''')


@pytest.fixture
def server(tmp_path):
    p = tmp_path / "srv.py"
    p.write_text(SERVER)
    return p


def _client(server, mode, timeout=3):
    return MCPClient("t", {"command": sys.executable, "args": [str(server), mode], "timeout": timeout})


async def _run(c, coro_fn, outer=25):
    try:
        await asyncio.wait_for(c.connect(), 30)
        return await asyncio.wait_for(coro_fn(), outer)
    finally:
        await asyncio.wait_for(c.close(), 15)


# ------------------------------------------------------------------ MCP stdio
def test_server_that_stops_reading_times_out(server):
    c = _client(server, "noread", timeout=2)

    async def go():
        t0 = time.monotonic()
        with pytest.raises(MCPError, match="stopped reading"):
            await c.call_tool("x", {"a": "y" * 2_000_000})
        first = time.monotonic() - t0
        t0 = time.monotonic()
        with pytest.raises(MCPError):  # the connection is gone: fail now, not hang or wait again
            await c.call_tool("x", {"a": "small"})
        return first, time.monotonic() - t0
    first, second = asyncio.run(_run(c, go))
    assert first < 5 and second < 3


def test_concurrent_calls_behind_a_stuck_write_do_not_hang(server):
    c = _client(server, "noread", timeout=2)

    async def go():
        t0 = time.monotonic()
        res = await asyncio.gather(*(c.call_tool("x", {"a": "y" * 1_000_000}) for _ in range(4)),
                                   return_exceptions=True)
        return res, time.monotonic() - t0
    res, dt = asyncio.run(_run(c, go))
    assert all(isinstance(r, MCPError) for r in res), res
    assert dt < 8


def test_dying_server_error_quotes_a_clipped_stderr_line(server):
    c = _client(server, "dieerr")

    async def go():
        with pytest.raises(MCPError) as ei:
            await c.call_tool("x", {})
        return str(ei.value), c.transport.stderr_tail
    msg, tail = asyncio.run(_run(c, go))
    assert "process exited: boom" in msg and len(msg) < 500
    assert all(len(line) <= 2000 for line in tail)


def test_stderr_over_the_line_limit_keeps_draining(server, monkeypatch):
    # a 40 MB stderr line, then 4 MB more: the old drain died at the long line, the pipe filled and
    # the server blocked before it could answer initialize
    monkeypatch.setenv("AVRONE_MCP_MAX_RESPONSE_BYTES", str(1 << 20))
    c = _client(server, "stderr2", timeout=20)

    async def go():
        return await c.call_tool("x", {}), c.transport.stderr_tail
    out, tail = asyncio.run(_run(c, go, outer=40))
    assert out == "ok"
    assert tail and tail[-1].startswith("L") and len(tail[-1]) == 2000


def test_normal_server_unaffected(server):
    c = _client(server, "ok")

    async def go():
        return await asyncio.gather(*(c.call_tool("x", {"a": "z" * 200_000}) for _ in range(8)))
    assert asyncio.run(_run(c, go)) == ["ok"] * 8


# ------------------------------------------------------------------ memstore merge_many
def _store(tmp_path, n=0):
    p = tmp_path / "memory.jsonl"
    with open(p, "w") as f:
        for i in range(n):
            f.write(json.dumps({"ns": f"agent:a{i % 20}", "key": f"k{i}", "value": "v", "ts": i}) + "\n")
    return memstore.MemoryStore(p)


def test_merge_many_reads_and_rewrites_once(tmp_path, monkeypatch):
    st = _store(tmp_path, 500)
    reads = []
    real = memstore.MemoryStore._records
    monkeypatch.setattr(memstore.MemoryStore, "_records", lambda self: (reads.append(1), real(self))[1])
    now = time.time()
    groups = {f"fleet:edge1/agent:x{j}": [{"key": f"q{i}", "value": "v", "ts": now} for i in range(5)]
              for j in range(30)}
    stats = st.merge_many(groups, source="edge1")
    assert stats == {"merged": 150, "skipped": 0, "rejected": 0}
    assert len(reads) == 1


def test_merge_many_matches_sequential_merge(tmp_path):
    now = time.time()
    groups = {"fleet:e/agent:a": [{"key": "x", "value": "1", "ts": now}, {"key": "x", "value": "2", "ts": now - 5},
                                  {"key": "y", "value": "v", "ts": float("nan")}, "junk", {"key": "", "value": "v"}],
              "fleet:e/agent:b": [{"key": "x", "value": "3", "ts": now}, {"key": "z", "value": "\ud800", "ts": now}]}
    (tmp_path / "a").mkdir(); (tmp_path / "b").mkdir()
    a, b = _store(tmp_path / "a", 50), _store(tmp_path / "b", 50)
    seq = {"merged": 0, "skipped": 0, "rejected": 0}
    for ns, items in groups.items():
        for k, v in a.merge(ns, items).items():
            seq[k] += v
    assert b.merge_many(groups) == seq
    assert a.namespaces() == b.namespaces() or \
        {n: {k: r["value"] for k, r in s.items()} for n, s in a.namespaces().items()} == \
        {n: {k: r["value"] for k, r in s.items()} for n, s in b.namespaces().items()}


def test_fleet_receive_memory_one_rewrite(tmp_path, monkeypatch):
    from avrone import fleet as fleet_mod
    from avrone.fleet import FleetManager
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)
    core = MagicMock(); core.active = {}
    core.settings = SimpleNamespace(workspace=str(tmp_path / "ws"), data_dir=str(tmp_path / "d"))
    fm = FleetManager(core, tmp_path / "d")
    (tmp_path / "d").mkdir(exist_ok=True)
    commits = []
    real = memstore.MemoryStore._commit
    monkeypatch.setattr(memstore.MemoryStore, "_commit", lambda self, *a: (commits.append(1), real(self, *a))[1])
    now = time.time()
    entries = [{"ns": f"w{j}", "key": f"k{i}", "value": "v", "ts": now} for j in range(20) for i in range(5)]
    out = fm.receive_memory({"capability": "memory:sync", "from": "edge1", "entries": entries})
    assert out["merged"] == 100 and len(out["namespaces"]) == 20
    assert len(commits) == 1


# ------------------------------------------------------------------ memory tools off the loop
def test_memory_save_and_lesson_forget_run_off_the_loop():
    from avrone.tools_extra import t_memory_save
    from avrone.tools_lessonforget import t_lesson_forget
    assert getattr(t_memory_save, "off_loop", False)
    assert getattr(t_lesson_forget, "off_loop", False)


def test_memory_sync_reads_the_store_off_the_loop(tmp_path):
    from avrone.codex import Codex
    from avrone.config import Settings
    from avrone.sovereign import LocalRunner
    from avrone.tools_extra import register_extra_tools
    from avrone.trinity import AgentContext, TrinityCore

    class L:
        async def chat(self, m, **k):
            return '{"final":"ok"}'
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"
    s.data_dir.mkdir(); s.workspace.mkdir()
    core = TrinityCore(s, L(), LocalRunner(s), Codex(s.data_dir)); register_extra_tools(core.tools)

    async def push(recs, who):
        return {"merged": len(recs)}
    core.fleet = SimpleNamespace(enabled=True, parent="http://p", node_id="n1", push_memory=push)
    ns = memstore.agent_namespace("Fuzzer")
    with open(s.data_dir / "memory.jsonl", "w") as f:
        f.write(json.dumps({"ns": ns, "key": "mine", "value": "v", "ts": 1}) + "\n")
        for i in range(120_000):
            f.write(json.dumps({"ns": f"agent:a{i % 100}", "key": f"k{i}", "value": "v" * 80, "ts": i}) + "\n")

    async def go():
        gaps, last, stop = [], time.monotonic(), False

        async def tick():
            nonlocal last
            while not stop:
                await asyncio.sleep(0.01)
                n = time.monotonic(); gaps.append(n - last); last = n
        t = asyncio.create_task(tick())
        await asyncio.sleep(0.03)
        out = await core.tools.call(AgentContext(core=core, run_id="m", name="Fuzzer", depth=0), "memory_sync", {})
        stop = True
        await asyncio.sleep(0.02)
        t.cancel()
        return out, max(gaps)
    out, gap = asyncio.run(go())
    assert out.startswith("synced 1 entries")
    assert gap < 0.3, f"loop blocked {gap:.2f}s"
