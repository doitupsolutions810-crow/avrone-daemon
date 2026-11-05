"""Offline tests for avrone.mcp_server (AVRONE exposed as an MCP server over stdio)."""
import asyncio
import io
import json

import pytest

from avrone import __version__, mcp_server as ms


class FakeTools:
    def list(self):
        return [{"name": "shell", "source": "builtin", "args": "{}", "description": "run"}]


class FakeCore:
    def __init__(self, deep=True, fail=False):
        self.tools = FakeTools()
        self.calls = []
        self.fail = fail
        if not deep:
            self.deep = None
            del self.deep

    async def squad(self, task, size=None):
        if self.fail:
            raise RuntimeError("model down")
        self.calls.append(("squad", task, size))
        return {"answer": f"squad:{task}", "run_id": "r1", "agents": [1, 2, 3]}

    async def chat(self, task):
        self.calls.append(("chat", task))
        return "plain string result"

    async def deep(self, task, first=None):
        self.calls.append(("deep", task, first))
        return {"answer": "deep report", "run_id": "r2"}


class NoDeepCore(FakeCore):
    deep = property(lambda self: (_ for _ in ()).throw(AttributeError))  # hasattr -> False


def server(core):
    async def factory():
        return core, {}
    return ms.AvroneMCPServer(core_factory=factory)


def run(c):
    return asyncio.run(c)


def test_server_info_tracks_package_version():
    assert ms.SERVER_INFO["version"] == __version__


@pytest.mark.parametrize("req,expected", [("2024-11-05", "2024-11-05"), ("1999-01-01", ms.PROTOCOL_VERSION),
                                          (None, ms.PROTOCOL_VERSION)])
def test_initialize_version_negotiation(req, expected):
    res = run(server(FakeCore()).handle("initialize", {"protocolVersion": req}))
    assert res["protocolVersion"] == expected and res["serverInfo"]["name"] == "avrone"


def test_ping_list_notifications_unknown():
    s = server(FakeCore())
    assert run(s.handle("ping", {})) == {}
    assert run(s.handle("notifications/initialized", {})) is None
    names = {t["name"] for t in run(s.handle("tools/list", {}))["tools"]}
    assert names == {"avrone_squad", "avrone_chat", "avrone_deep", "avrone_tools"}
    with pytest.raises(ms._RPCError) as e:
        run(s.handle("nope/method", {}))
    assert e.value.code == -32601


def test_call_unknown_tool():
    with pytest.raises(ms._RPCError) as e:
        run(server(FakeCore()).call_tool("rm_rf", {}))
    assert e.value.code == -32602


def test_squad_structured_and_meta():
    core = FakeCore()
    res = run(server(core).call_tool("avrone_squad", {"task": "  build it  ", "squad_size": 3}))
    assert res["isError"] is False and res["content"][0]["text"] == "squad:build it"
    assert res["structuredContent"] == {"run_id": "r1", "agents": 3}
    assert core.calls == [("squad", "build it", 3)]


@pytest.mark.parametrize("size,expected", [(None, None), (0, 1), (-5, 1), (99, 8), ("4", 4),
                                           ("junk", None), (True, None), (2.9, 2)])
def test_squad_size_clamped_to_schema(size, expected):
    assert ms._clamp_size(size) == expected


def test_task_required_and_tools_listing():
    s = server(FakeCore())
    res = run(s.call_tool("avrone_chat", {"task": "   "}))
    assert res["isError"] and "required" in res["content"][0]["text"]
    res = run(s.call_tool("avrone_tools", {}))
    assert res["structuredContent"]["tools"][0]["name"] == "shell"


def test_chat_string_result():
    res = run(server(FakeCore()).call_tool("avrone_chat", {"task": "hi"}))
    assert res == {"content": [{"type": "text", "text": "plain string result"}], "isError": False}


def test_deep_uses_core_deep():
    core = FakeCore()
    res = run(server(core).call_tool("avrone_deep", {"task": "q", "first_answer": "draft"}))
    assert res["content"][0]["text"] == "deep report"
    assert core.calls == [("deep", "q", "draft")]


def test_deep_falls_back_to_squad_prompt():
    core = NoDeepCore()
    run(server(core).call_tool("avrone_deep", {"task": "q", "first_answer": "draft"}))
    kind, prompt, _ = core.calls[0]
    assert kind == "squad" and "draft" in prompt and "TASK:\nq" in prompt


def test_core_failure_becomes_tool_error():
    res = run(server(FakeCore(fail=True)).call_tool("avrone_squad", {"task": "x"}))
    assert res["isError"] and "RuntimeError: model down" in res["content"][0]["text"]


def test_factory_failure_becomes_tool_error():
    async def factory():
        raise OSError("docker missing")
    s = ms.AvroneMCPServer(core_factory=factory)
    res = run(s.call_tool("avrone_chat", {"task": "x"}))
    assert res["isError"] and "docker missing" in res["content"][0]["text"]


def test_core_built_once():
    n = {"c": 0}

    async def factory():
        n["c"] += 1
        return FakeCore(), {}

    async def go():
        s = ms.AvroneMCPServer(core_factory=factory)
        s.warm()
        await asyncio.gather(s.core(), s.core(), s.core())
    run(go())
    assert n["c"] == 1


# ------------------------------------------------------------ full stdio loop
def _serve(lines, core=None):
    async def go():
        s = server(core or FakeCore())
        reader = asyncio.StreamReader()
        for ln in lines:
            reader.feed_data((ln if isinstance(ln, str) else json.dumps(ln)).encode() + b"\n")
        reader.feed_eof()
        out = io.BytesIO()
        await s.serve(reader, out)
        return [json.loads(x) for x in out.getvalue().decode().splitlines() if x]
    return run(go())


def test_serve_roundtrip_batch_parse_error_and_notifications():
    msgs = _serve([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},   # no reply
        "",                                                          # blank line skipped
        "{not json",                                                 # parse error
        [{"jsonrpc": "2.0", "id": 2, "method": "ping"},
         {"jsonrpc": "2.0", "id": 3, "method": "bogus"}],            # batch
        {"jsonrpc": "2.0", "id": 4, "result": {}},                   # stray response: ignored
        "42",                                                        # junk scalar: ignored
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "avrone_squad", "arguments": {"task": "t"}}},
    ])
    by_id = {m["id"]: m for m in msgs}
    assert by_id[1]["result"]["serverInfo"]["name"] == "avrone"
    # Parse failures and non-object JSON-RPC values both use a null id, so
    # assert their distinct protocol error codes without relying on arrival order.
    null_id_errors = [m.get("error", {}).get("code") for m in msgs if m.get("id") is None]
    assert -32700 in null_id_errors
    assert -32600 in null_id_errors
    assert by_id[2]["result"] == {}
    assert by_id[3]["error"]["code"] == -32601
    assert by_id[5]["result"]["content"][0]["text"] == "squad:t"
    assert 4 not in by_id and len(msgs) == 5


def test_serve_internal_error_maps_to_32603(monkeypatch):
    async def explode(self, method, params):
        raise ValueError("kaboom")
    monkeypatch.setattr(ms.AvroneMCPServer, "handle", explode)
    msgs = _serve([{"jsonrpc": "2.0", "id": 9, "method": "ping"},
                   {"jsonrpc": "2.0", "method": "notifications/x"}])
    assert msgs == [{"jsonrpc": "2.0", "id": 9, "error": {"code": -32603, "message": "ValueError: kaboom"}}]


def test_shutdown_stops_extras():
    stopped = []

    class Stoppable:
        def __init__(self, n):
            self.n = n

        async def stop(self):
            stopped.append(self.n)
            if self.n == "mcp":
                raise RuntimeError("ignored")

    async def factory():
        return FakeCore(), {"mcp": Stoppable("mcp"), "runner": Stoppable("runner")}

    async def go():
        s = ms.AvroneMCPServer(core_factory=factory)
        await s.core()
        await s.shutdown()
    run(go())
    assert stopped == ["mcp", "runner"]


def test_shutdown_noop_when_core_failed():
    async def factory():
        raise RuntimeError("no core")

    async def go():
        s = ms.AvroneMCPServer(core_factory=factory)
        s.warm()
        with pytest.raises(RuntimeError):
            await s.core()
        await s.shutdown()  # must not raise
    run(go())
