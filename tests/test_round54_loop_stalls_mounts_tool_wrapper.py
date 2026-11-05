"""Round 54: event-loop stalls, docker bind paths, and the tool wrapper.

  * memory_recall / memory_list / memory_peek / lesson_recall / codex_search parsed whole logs on
    the event loop: with 200k memory lines memory_recall held it ~1.1 s per call (every agent, the
    API, WebSockets and heartbeats paused). They now run in a worker thread (tools.off_loop), and
    /api/telemetry, /api/runs and /api/runs/{id}/bridge read logs in a thread too;
  * docker `-v SRC:DST` split a workspace path containing ':' (container never started);
    agentfs.bind_args uses --mount with CSV quoting for ':'/',' and refuses newline/NUL/quote;
  * ToolRegistry.call: a tool calling sys.exit() raised SystemExit through the agent loop; a plugin's
    plain `def` tool failed every call ("can't be used in 'await' expression"); an output or
    exception whose __str__ raises escaped the wrapper.
"""
from __future__ import annotations

import asyncio
import csv
import json
import sys
import time
from pathlib import Path

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import Tool, ToolRegistry
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


def _ctx(core, name="Fuzzer"):
    return AgentContext(core=core, run_id="r54", name=name, depth=0)


# ------------------------------------------------------------------ event-loop stalls
@pytest.mark.parametrize("tool", ["memory_recall", "memory_list", "memory_peek", "lesson_recall", "codex_search"])
def test_log_scanning_tools_run_off_the_event_loop(tmp_path, tool):
    core = _core(tmp_path)
    t = core.tools.get(tool)
    assert t is not None
    assert getattr(t.fn, "off_loop", False), f"{tool} still parses logs on the event loop"


def test_big_memory_store_does_not_stall_the_loop(tmp_path):
    core = _core(tmp_path)
    ctx = _ctx(core)
    from avrone.tools_extra import _mem_file, _mem_ns
    path, ns = _mem_file(ctx), _mem_ns(ctx)
    now = time.time()
    with open(path, "w") as f:
        for i in range(200_000):  # overwrite history of an append-only store on a busy node
            f.write(json.dumps({"ns": ns, "key": f"k{i % 400}", "value": "v" * 100 + str(i), "ts": now}) + "\n")

    async def go():
        gaps, last, stop = [], time.monotonic(), False

        async def tick():
            nonlocal last
            while not stop:
                await asyncio.sleep(0.01)
                n = time.monotonic(); gaps.append(n - last); last = n
        t = asyncio.create_task(tick())
        await asyncio.sleep(0.03)
        out = await core.tools.call(ctx, "memory_recall", {"query": "k7"})
        stop = True
        await asyncio.sleep(0.02)
        t.cancel()
        return out, max(gaps)
    out, worst = asyncio.run(go())
    assert "k7" in out
    assert worst < 0.4, f"event loop blocked for {worst:.2f}s"


def test_off_loop_refuses_a_tool_that_awaits():
    from avrone.tools import off_loop

    @off_loop
    async def bad(ctx, args):
        await asyncio.sleep(0)
        return "x"
    reg = ToolRegistry()
    reg.register(Tool("bad_awaiter", "d", "{}", bad))
    out = asyncio.run(reg.call(None, "bad_awaiter", {}))
    assert out.startswith("ERROR: RuntimeError") and "awaited" in out


def _loop_probe():
    try:
        asyncio.get_running_loop()
        return "on-loop"
    except RuntimeError:
        return "thread"


def test_runs_and_bridge_endpoints_read_logs_in_a_thread(monkeypatch):
    from fastapi.testclient import TestClient
    from avrone import server
    seen = {}
    with TestClient(server.app) as c:
        cx = server.core().codex
        monkeypatch.setattr(cx, "runs", lambda *a, **k: (seen.__setitem__("runs", _loop_probe()), [])[1])
        monkeypatch.setattr(cx, "replay", lambda *a, **k: (seen.__setitem__("replay", _loop_probe()), [])[1])
        h = {"x-admin-token": server.core().settings.admin_token or ""}
        assert c.get("/api/runs", headers=h).status_code == 200
        assert c.get("/api/runs/nosuchrun54/bridge", headers=h).status_code == 404
    assert seen == {"runs": "thread", "replay": "thread"}


def test_telemetry_endpoint_reads_logs_in_a_thread(monkeypatch):
    from fastapi.testclient import TestClient
    from avrone import server, telemetry
    seen = []
    monkeypatch.setattr(telemetry, "telemetry", lambda *a, **k: (seen.append(_loop_probe()), {"ok": 1})[1])
    with TestClient(server.app) as c:
        h = {"x-admin-token": server.core().settings.admin_token or ""}
        r = c.get("/api/telemetry", headers=h)
    assert r.status_code == 200 and seen == ["thread"]


# ------------------------------------------------------------------ docker bind paths
def test_bind_args_plain_path_keeps_dash_v(tmp_path):
    from avrone.agentfs import bind_args
    assert bind_args(tmp_path, "/workspace") == ["-v", f"{tmp_path.resolve()}:/workspace"]


@pytest.mark.parametrize("name", ["proj:v2", "a,b", "x:y,z"])
def test_bind_args_colon_or_comma_uses_quoted_mount(tmp_path, name):
    from avrone.agentfs import bind_args
    d = tmp_path / name
    d.mkdir()
    flag, spec = bind_args(d, "/agent")
    assert flag == "--mount"
    fields = dict(f.split("=", 1) for f in next(csv.reader([spec])))
    assert fields == {"type": "bind", "source": str(d.resolve()), "target": "/agent"}


@pytest.mark.parametrize("name", ["new\nline", 'quo"te'])
def test_bind_args_refuses_unmountable_paths(tmp_path, name):
    from avrone.agentfs import AgentFSError, bind_args
    with pytest.raises(AgentFSError):
        bind_args(tmp_path / name, "/workspace")


def test_bind_args_relative_path_becomes_absolute(tmp_path, monkeypatch):
    from avrone.agentfs import bind_args
    monkeypatch.chdir(tmp_path)
    flag, spec = bind_args("ws", "/workspace")
    assert spec.startswith(str(tmp_path.resolve()) + "/ws:")


def test_docker_runner_start_mounts_a_colon_workspace(tmp_path):
    from avrone.sovereign import DockerRunner, CommandResult
    s = Settings(); s.workspace = tmp_path / "data:v2"; s.data_dir = tmp_path / "d"
    r = DockerRunner(s)
    got = {}

    async def fake_exec(argv, cmd, timeout, cwd=None, **kw):
        got["argv"] = argv
        return CommandResult(cmd=cmd, exit_code=0, stdout="cid", stderr="")
    r._exec = fake_exec
    asyncio.run(r.start())
    argv = got["argv"]
    assert "--mount" in argv and "-v" not in argv
    spec = argv[argv.index("--mount") + 1]
    assert f'"source={(tmp_path / "data:v2").resolve()}"' in spec and spec.endswith("target=/workspace")


# ------------------------------------------------------------------ tool wrapper
def test_tool_calling_sys_exit_does_not_escape():
    async def quitter(ctx, args):
        sys.exit(3)
    reg = ToolRegistry()
    reg.register(Tool("quitter", "d", "{}", quitter, source="plugin:x"))
    out = asyncio.run(reg.call(None, "quitter", {}))
    assert out.startswith("ERROR: SystemExit")


def test_plain_def_plugin_tool_runs_in_a_thread():
    def sync_tool(ctx, args):
        return f"hello {args.get('x')} {_loop_probe()}"
    reg = ToolRegistry()
    reg.register(Tool("sync_tool", "d", "{}", sync_tool, source="plugin:x"))
    assert asyncio.run(reg.call(None, "sync_tool", {"x": 1})) == "hello 1 thread"


def test_hostile_str_in_output_and_exception():
    class Evil:
        def __str__(self):
            raise RuntimeError("no str")

    class EvilErr(Exception):
        def __str__(self):
            raise RuntimeError("no str")

    async def evil_out(ctx, args):
        return Evil()

    async def evil_err(ctx, args):
        raise EvilErr()
    reg = ToolRegistry()
    reg.register(Tool("evil_out", "d", "{}", evil_out))
    reg.register(Tool("evil_err", "d", "{}", evil_err))
    assert asyncio.run(reg.call(None, "evil_out", {})) == "<Evil>"
    assert asyncio.run(reg.call(None, "evil_err", {})) == "ERROR: EvilErr: <EvilErr>"


def test_sys_exit_under_capgate_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE", "audit")
    core = _core(tmp_path)

    async def quitter(ctx, args):
        raise SystemExit(1)
    core.tools.register(Tool("quitter2", "d", "{}", quitter, source="plugin:x"))
    out = asyncio.run(core.tools.call(_ctx(core), "quitter2", {}))
    assert out.startswith("ERROR: SystemExit")
