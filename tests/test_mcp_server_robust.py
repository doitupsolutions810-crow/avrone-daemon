"""Round 38: AVRONE's own MCP server (stdio JSON-RPC) against hostile and odd clients."""
import asyncio
import io
import json
import os
import subprocess
import sys
import time

import pytest

from avrone import mcp_server as ms


class Tools:
    def list(self):
        return [{"name": "shell"}]


class Core:
    def __init__(self, delay=0.0, result=None):
        self.tools = Tools()
        self.delay = delay
        self.result = result
        self.calls = []
        self.cancelled = 0
        self.settings = None

    async def squad(self, task, size=None, **kw):
        self.calls.append(("squad", task, size))
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return self.result if self.result is not None else {"answer": f"squad:{task}", "run_id": "r"}

    async def chat(self, task):
        self.calls.append(("chat", task))
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        return self.result if self.result is not None else f"chat:{task}"

    async def deep(self, task, first=None, **kw):
        self.calls.append(("deep", task, first))
        return {"answer": "deep", "run_id": "d"}


class Out(io.BytesIO):
    def flush(self):
        pass


def server(core):
    async def fac():
        return core, {}
    return ms.AvroneMCPServer(fac)


def lines_of(out):
    return [json.loads(x) for x in out.getvalue().decode().splitlines() if x]


def serve(lines, core=None, raw=False):
    core = core or Core()

    async def go():
        s = server(core)
        r = asyncio.StreamReader(limit=16 * 1024 * 1024)
        for ln in lines:
            if isinstance(ln, bytes):
                r.feed_data(ln + b"\n")
            else:
                r.feed_data(((ln if isinstance(ln, str) else json.dumps(ln)) + "\n").encode())
        r.feed_eof()
        o = Out()
        await asyncio.wait_for(s.serve(r, o), 30)
        return o, s
    o, s = asyncio.run(go())
    return (o.getvalue() if raw else lines_of(o)), core, s


PING = {"jsonrpc": "2.0", "id": 99, "method": "ping"}


def call(i, name="avrone_chat", **args):
    return {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": args}}


# ================================================================ parser crashes
def test_huge_integer_is_parse_error_not_crash():
    out, _, _ = serve(['{"jsonrpc":"2.0","id":' + "9" * 5000 + ',"method":"ping"}', PING])
    assert out[0]["error"]["code"] == -32700 and out[-1] == {"jsonrpc": "2.0", "id": 99, "result": {}}


def test_huge_integer_inside_params_is_parse_error():
    out, _, _ = serve(['{"jsonrpc":"2.0","id":1,"method":"ping","params":{"n":' + "1" * 10000 + "}}", PING])
    assert out[0]["error"]["code"] == -32700 and out[-1]["id"] == 99


def test_deep_nesting_is_parse_error_not_crash():
    out, _, _ = serve(["[" * 200000 + "]" * 200000, PING])
    assert out[0]["error"]["code"] in (-32700, -32600) and out[-1]["id"] == 99


def test_deep_nesting_in_params_is_parse_error():
    deep = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":' + "[" * 100000 + "]" * 100000 + "}}"
    out, _, _ = serve([deep, PING])
    assert out[0]["error"]["code"] == -32700 and out[-1]["id"] == 99


def test_utf8_bom_line_accepted():
    out, _, _ = serve(["\ufeff" + json.dumps(PING)])
    assert out == [{"jsonrpc": "2.0", "id": 99, "result": {}}]


def test_invalid_utf8_bytes_still_parse_as_replacement():
    out, _, _ = serve([b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"\xff\xfe"}}'])
    assert out == [{"jsonrpc": "2.0", "id": 1, "result": {}}]


@pytest.mark.parametrize("junk", ["NaN", "Infinity", "-Infinity", "null", "true", '"str"', "3.5"])
def test_scalar_junk_ignored_and_server_lives(junk):
    out, _, _ = serve([junk, PING])
    assert out[-1]["id"] == 99


@pytest.mark.parametrize("raw", [
    '{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"}',
    '{"jsonrpc":"2.0","id":1,"method":"ping","params":{},"params":{"x":1}}',
    '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":1,"x":2}}',
    '[{"jsonrpc":"2.0","id":1,"id":2,"method":"ping"},' + json.dumps(PING) + ']',
])
def test_duplicate_json_object_member_is_parse_error(raw):
    out, _, _ = serve([raw, PING])
    assert out[0] == {"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32700, "message": "Parse error"}}
    assert out[-1] == {"jsonrpc": "2.0", "id": 99, "result": {}}


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
@pytest.mark.parametrize("nested", ['"n":{value}', '"n":[0,{value}]'])
def test_non_finite_number_inside_request_is_parse_error(literal, nested):
    params = nested.format(value=literal)
    raw = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{' + params + '}}'
    out, _, _ = serve([raw, PING])
    assert out[0] == {"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32700, "message": "Parse error"}}
    assert out[-1] == {"jsonrpc": "2.0", "id": 99, "result": {}}


def test_non_json_number_in_batch_rejects_whole_line_and_keeps_server_alive():
    raw = ('[{"jsonrpc":"2.0","id":1,"method":"ping","params":{"n":NaN}},'
           + json.dumps(PING) + ']')
    out, _, _ = serve([raw, PING])
    assert out[0] == {"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32700, "message": "Parse error"}}
    assert out[-1] == {"jsonrpc": "2.0", "id": 99, "result": {}}


def test_non_object_batch_member_gets_invalid_request_response():
    out, _, _ = serve([[17, PING]])
    assert out[0] == {"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32600, "message": "Invalid Request"}}
    assert out[1] == {"jsonrpc": "2.0", "id": 99, "result": {}}


@pytest.mark.parametrize("bad", [
    {"jsonrpc": "2.0", "id": 5},
    {"jsonrpc": "2.0", "id": 6, "params": {}},
    {"jsonrpc": "2.0", "params": {}},
])
def test_request_shaped_object_without_method_gets_invalid_request(bad):
    out, _, _ = serve([bad, PING])
    assert out[0] == {"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32600, "message": "Invalid Request"}}
    assert out[-1] == {"jsonrpc": "2.0", "id": 99, "result": {}}


@pytest.mark.parametrize("response", [
    {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}},
    {"jsonrpc": "2.0", "id": 8, "error": {"code": -32600, "message": "bad"}},
])
def test_unsolicited_response_without_method_is_ignored(response):
    out, _, _ = serve([response, PING])
    assert out == [{"jsonrpc": "2.0", "id": 99, "result": {}}]


def test_batch_member_without_method_gets_invalid_request_response():
    out, _, _ = serve([[{"jsonrpc": "2.0", "id": 7}, PING]])
    assert any(m == {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Invalid Request"}} for m in out)
    assert any(m == {"jsonrpc": "2.0", "id": 99, "result": {}} for m in out)


def test_crlf_lines():
    out, _, _ = serve([json.dumps(PING) + "\r"])
    assert out == [{"jsonrpc": "2.0", "id": 99, "result": {}}]


# ================================================================ ids and envelope
@pytest.mark.parametrize("bad", [True, False, 1.5, [1], {"a": 1}])
def test_bad_id_types_rejected_with_null_id(bad):
    out, _, _ = serve([{"jsonrpc": "2.0", "id": bad, "method": "ping"}, PING])
    assert out[0] == {"jsonrpc": "2.0", "id": None,
                      "error": {"code": -32600, "message": "Invalid Request: id must be a string, an integer or null"}}


@pytest.mark.parametrize("good", [0, -7, 2 ** 62, "", "abc", "ü-1"])
def test_good_ids_echoed_exactly(good):
    out, _, _ = serve([{"jsonrpc": "2.0", "id": good, "method": "ping"}])
    assert out == [{"jsonrpc": "2.0", "id": good, "result": {}}]


def test_int_and_string_ids_are_distinct():
    out, _, _ = serve([[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                        {"jsonrpc": "2.0", "id": "1", "method": "ping"}]])
    assert sorted(json.dumps(m["id"]) for m in out) == ['"1"', "1"] and all("result" in m for m in out)


@pytest.mark.parametrize("ver", ["1.0", 2.0, None, "2.0 "])
def test_wrong_jsonrpc_version_rejected(ver):
    out, _, _ = serve([{"jsonrpc": ver, "id": 5, "method": "ping"}])
    assert out[0]["id"] == 5 and out[0]["error"]["code"] == -32600 and "2.0" in out[0]["error"]["message"]


def test_missing_jsonrpc_tolerated():
    out, _, _ = serve([{"id": 5, "method": "ping"}])
    assert out == [{"jsonrpc": "2.0", "id": 5, "result": {}}]


def test_wrong_version_notification_gets_no_reply():
    out, _, _ = serve([{"jsonrpc": "1.0", "method": "notifications/initialized"}, PING])
    assert out == [{"jsonrpc": "2.0", "id": 99, "result": {}}]


def test_null_id_request_answered_and_not_tracked():
    out, _, s = serve([[{"jsonrpc": "2.0", "id": None, "method": "ping"},
                        {"jsonrpc": "2.0", "id": None, "method": "ping"}]])
    assert out == [{"jsonrpc": "2.0", "id": None, "result": {}}] * 2 and s._by_id == {}


# ================================================================ batches
def test_batch_over_cap_refused(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_MAX_BATCH", "3")
    out, core, _ = serve([json.dumps([call(i, task="t") for i in range(4)]), PING])
    assert out[0]["id"] is None and "exceeds 3" in out[0]["error"]["message"] and core.calls == []
    assert out[-1]["id"] == 99


def test_batch_at_cap_runs(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_MAX_BATCH", "3")
    out, core, _ = serve([json.dumps([{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(3)])])
    assert sorted(m["id"] for m in out) == [0, 1, 2]


@pytest.mark.parametrize("v,want", [("", 64), ("x", 64), ("0", 1), ("-3", 1), ("99999", 1024), ("10", 10)])
def test_max_batch_env(monkeypatch, v, want):
    monkeypatch.setenv("AVRONE_MCP_MAX_BATCH", v)
    assert ms.max_batch() == want


def test_default_batch_is_64(monkeypatch):
    monkeypatch.delenv("AVRONE_MCP_MAX_BATCH", raising=False)
    out, _, _ = serve([json.dumps([{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(65)])])
    assert len(out) == 1 and out[0]["error"]["code"] == -32600


# ================================================================ duplicate ids in flight
def test_duplicate_inflight_id_refused():
    core = Core(delay=0.3)
    out, _, _ = serve([[call(7, task="a"), call(7, task="b")]], core)
    errs = [m for m in out if "error" in m]
    oks = [m for m in out if "result" in m]
    assert len(errs) == 1 and "still in progress" in errs[0]["error"]["message"] and errs[0]["id"] == 7
    assert len(oks) == 1 and core.calls == [("chat", "a")]


def test_same_id_reusable_after_finish():
    out, _ = asyncio.run(interactive(Core(), [(0, call(7, task="a")), (0.2, call(7, task="b"))]))
    assert [m["result"]["content"][0]["text"] for m in out] == ["chat:a", "chat:b"]


# ================================================================ cancellation
async def interactive(core, steps, grace=None):
    """steps: list of (delay_before, message). Returns parsed output."""
    s = server(core)
    r = asyncio.StreamReader()
    o = Out()
    task = asyncio.create_task(s.serve(r, o))
    for delay, msg in steps:
        await asyncio.sleep(delay)
        r.feed_data((json.dumps(msg) + "\n").encode())
    r.feed_eof()
    await asyncio.wait_for(task, 30)
    return lines_of(o), s


def cancel_msg(rid):
    return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": rid, "reason": "user"}}


def test_cancel_stops_run_and_sends_no_reply():
    core = Core(delay=5)
    out, s = asyncio.run(interactive(core, [(0, call(1, task="long")), (0.2, cancel_msg(1)), (0.1, PING)]))
    assert out == [{"jsonrpc": "2.0", "id": 99, "result": {}}]
    assert core.cancelled == 1 and s._inflight == 0


def test_cancel_frees_inflight_slot(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_MAX_INFLIGHT", "1")
    core = Core(delay=0.5)
    steps = [(0, call(1, task="a")), (0.1, cancel_msg(1)), (0.1, call(2, task="b"))]
    out, _ = asyncio.run(interactive(core, steps))
    assert out == [{"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "chat:b"}],
                                                           "isError": False}}]


def test_cancel_string_id_does_not_cancel_int_id():
    core = Core(delay=0.3)
    out, _ = asyncio.run(interactive(core, [(0, call(1, task="a")), (0.05, cancel_msg("1"))]))
    assert core.cancelled == 0 and out[0]["id"] == 1 and "result" in out[0]


@pytest.mark.parametrize("params", [None, [], {"requestId": None}, {"requestId": True}, {"requestId": 404},
                                    {"requestId": {"x": 1}}, "nope"])
def test_cancel_junk_or_unknown_is_harmless(params):
    core = Core(delay=0.2)
    msg = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params}
    out, _ = asyncio.run(interactive(core, [(0, call(1, task="a")), (0.05, msg)]))
    assert core.cancelled == 0 and out[0]["id"] == 1 and "result" in out[0]


def test_cancel_after_finish_is_noop():
    core = Core()
    out, _ = asyncio.run(interactive(core, [(0, call(1, task="a")), (0.2, cancel_msg(1)), (0.05, PING)]))
    assert [m["id"] for m in out] == [1, 99]


def test_cancel_releases_checkpoint_claim(tmp_path):
    from types import SimpleNamespace
    from avrone import checkpoint as cp
    core = Core(delay=5)
    core.settings = SimpleNamespace(data_dir=tmp_path)
    steps = [(0, call(1, name="avrone_squad", task="t", checkpoint_id="job1")), (0.2, cancel_msg(1))]
    asyncio.run(interactive(core, steps))
    assert not cp.is_busy(cp.managed_path(tmp_path, "job1"))


# ================================================================ shutdown on stdin close
def test_eof_waits_for_short_work(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_SHUTDOWN_GRACE", "5")
    out, core, _ = serve([call(1, task="a")], Core(delay=0.2))
    assert out[0]["result"]["content"][0]["text"] == "chat:a"


def test_eof_cancels_long_work_after_grace(monkeypatch):
    monkeypatch.setenv("AVRONE_MCP_SHUTDOWN_GRACE", "0.2")
    import time
    t0 = time.monotonic()
    out, core, s = serve([call(1, task="a"), call("x", name="avrone_squad", task="b")], Core(delay=60))
    assert time.monotonic() - t0 < 10
    assert sorted(json.dumps(m["id"]) for m in out) == ['"x"', "1"]
    assert all("shutting down" in m["error"]["message"] for m in out)
    assert core.cancelled == 2 and s._inflight == 0


@pytest.mark.parametrize("v,want", [("", 30.0), ("abc", 30.0), ("-1", 0.0), ("nan", 30.0), ("1e9", 3600.0),
                                    ("2.5", 2.5)])
def test_shutdown_grace_env(monkeypatch, v, want):
    monkeypatch.setenv("AVRONE_MCP_SHUTDOWN_GRACE", v)
    assert ms.shutdown_grace() == want


# ================================================================ output safety
def test_nan_result_becomes_internal_error_not_invalid_json():
    core = Core(result={"answer": "ok", "run_id": float("nan")})
    out, _, _ = serve([call(1, name="avrone_squad", task="t")], core, raw=True)
    msg = json.loads(out.decode().splitlines()[0])  # strict parse below
    json.loads(out.decode().splitlines()[0], parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
    assert msg["id"] == 1 and msg["error"]["code"] == -32603


def test_unserialisable_tools_list_becomes_error():
    class T:
        def list(self):
            return [{"name": object()}]
    core = Core()
    core.tools = T()
    out, _, _ = serve([call(3, name="avrone_tools")], core)
    assert out[0]["id"] == 3 and ("error" in out[0] or out[0]["result"]["isError"])


def test_lone_surrogate_in_result_still_written():
    out, _, _ = serve([call(1, task="t")], Core(result="bad \udcff char"), raw=True)
    line = out.decode().splitlines()[0]
    msg = json.loads(line)
    assert msg["id"] == 1 and "bad" in msg["result"]["content"][0]["text"]


def test_broken_stdout_stops_server():
    class Broken(Out):
        def write(self, b):
            raise BrokenPipeError(32, "EPIPE")

    async def go():
        s = server(Core())
        r = asyncio.StreamReader()
        for _ in range(3):
            r.feed_data((json.dumps(PING) + "\n").encode())
        # no EOF: the server must stop by itself once stdout is gone
        t = asyncio.create_task(s.serve(r, Broken()))
        await asyncio.sleep(0.1)
        r.feed_data((json.dumps(PING) + "\n").encode())
        await asyncio.wait_for(t, 5)
        return s
    s = asyncio.run(go())
    assert s._closed


def test_closed_stdout_valueerror_handled():
    class Closed(Out):
        def write(self, b):
            raise ValueError("I/O operation on closed file")
    async def go():
        s = server(Core())
        r = asyncio.StreamReader()
        r.feed_data((json.dumps(PING) + "\n").encode())
        r.feed_eof()
        await asyncio.wait_for(s.serve(r, Closed()), 5)
        return s
    assert asyncio.run(go())._closed


# ================================================================ argument shapes
@pytest.mark.parametrize("size", [1e999, -1e999, "1e999", float("nan"), "nan", [4], {"n": 4}])
def test_clamp_size_junk_is_default(size):
    assert ms._clamp_size(size) is None


@pytest.mark.parametrize("size,want", [(0, 1), (99, 8), (3.9, 3), ("5", 5), (" 2 ", 2)])
def test_clamp_size_values(size, want):
    assert ms._clamp_size(size) == want


def test_huge_float_squad_size_over_wire_uses_default():
    out, core, _ = serve(['{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"avrone_squad",'
                          '"arguments":{"task":"t","squad_size":1e999}}}'])
    assert out[0]["result"]["isError"] is False and core.calls == [("squad", "t", None)]


@pytest.mark.parametrize("first", [5, ["a"], {"a": 1}, True])
def test_first_answer_must_be_string(first):
    out, core, _ = serve([call(1, name="avrone_deep", task="t", first_answer=first)])
    assert out[0]["result"]["isError"] and "first_answer" in out[0]["result"]["content"][0]["text"]
    assert core.calls == []


def test_first_answer_null_is_fine():
    out, core, _ = serve([call(1, name="avrone_deep", task="t", first_answer=None)])
    assert out[0]["result"]["isError"] is False


# ================================================================ real subprocess over stdio
def test_real_stdio_process_survives_hostile_lines(tmp_path):
    env = {**os.environ, "AVRONE_DATA_DIR": str(tmp_path / "d"), "AVRONE_WORKSPACE": str(tmp_path / "w"),
           "AVRONE_MCP_SERVER_CHILD": "1", "AVRONE_LLM_BASE_URL": "http://127.0.0.1:9/v1",
           "AVRONE_MCP_SHUTDOWN_GRACE": "1"}
    hostile = [
        '{"jsonrpc":"2.0","id":' + "9" * 6000 + ',"method":"ping"}',
        "[" * 50000 + "]" * 50000,
        '{"jsonrpc":"2.0","id":true,"method":"ping"}',
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
    ]
    p = subprocess.run([sys.executable, "-m", "avrone.mcp_server"], input="\n".join(hostile) + "\n",
                       capture_output=True, text=True, timeout=60, env=env, cwd=os.getcwd())
    out = [json.loads(x) for x in p.stdout.splitlines() if x.strip()]
    assert out[-1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert [m["error"]["code"] for m in out[:3]] in ([-32700, -32700, -32600], [-32700, -32600, -32600])


# ================================================================ round 40: shutdown while the core is still building
def _eof_reader():
    r = asyncio.StreamReader()
    r.feed_eof()
    return r


class _Sink:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def flush(self):
        pass


def test_shutdown_cancels_core_that_never_finishes(monkeypatch):
    from avrone import mcp_server as ms
    monkeypatch.setenv("AVRONE_MCP_SHUTDOWN_GRACE", "0")
    started = asyncio.Event() if False else None

    async def forever():
        await asyncio.sleep(3600)

    async def go():
        srv = ms.AvroneMCPServer(core_factory=forever)
        t0 = time.monotonic()
        await asyncio.wait_for(srv.serve(_eof_reader(), _Sink()), timeout=10)
        assert srv._core_task.cancelled()
        return time.monotonic() - t0
    assert asyncio.run(go()) < 5


def test_shutdown_stops_runner_of_core_finishing_after_eof(monkeypatch):
    from avrone import mcp_server as ms
    monkeypatch.setenv("AVRONE_MCP_SHUTDOWN_GRACE", "5")
    stopped = []

    class Runner:
        async def stop(self):
            stopped.append("runner")

    class Mgr:
        async def stop(self):
            stopped.append("mcp")

    async def slow():
        await asyncio.sleep(0.3)
        return object(), {"runner": Runner(), "mcp": Mgr()}

    async def go():
        srv = ms.AvroneMCPServer(core_factory=slow)
        await srv.serve(_eof_reader(), _Sink())
    asyncio.run(go())
    assert sorted(stopped) == ["mcp", "runner"]  # before: never stopped, children held the pipe


def test_shutdown_tolerates_core_build_failure(monkeypatch):
    from avrone import mcp_server as ms
    monkeypatch.setenv("AVRONE_MCP_SHUTDOWN_GRACE", "2")

    async def boom():
        await asyncio.sleep(0.05)
        raise RuntimeError("docker probe failed")

    async def go():
        srv = ms.AvroneMCPServer(core_factory=boom)
        await srv.serve(_eof_reader(), _Sink())
    asyncio.run(go())


def test_protocol_stdout_children_cannot_write_to_protocol(tmp_path):
    code = (
        "import sys, subprocess\n"
        "from avrone.mcp_server import protocol_stdout\n"
        "out = protocol_stdout()\n"
        "subprocess.run([sys.executable, '-c', 'print(\"STRAY-CHILD\")'])\n"
        "print('STRAY-PARENT')\n"
        "out.write(b'{\"ok\":1}\\n'); out.flush()\n"
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30, cwd=os.getcwd())
    assert p.stdout == '{"ok":1}\n'
    assert "STRAY-CHILD" in p.stderr


def test_protocol_stdout_lingering_child_does_not_hold_pipe(tmp_path):
    code = (
        "import sys, subprocess\n"
        "from avrone.mcp_server import protocol_stdout\n"
        "out = protocol_stdout()\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], stderr=subprocess.DEVNULL)\n"
        "out.write(b'bye\\n'); out.flush()\n"
    )
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=25, cwd=os.getcwd())
    assert p.stdout == b"bye\n" and time.monotonic() - t0 < 15


def test_protocol_stdout_fallback(monkeypatch):
    from avrone import mcp_server as ms
    import io

    class NoFd(io.TextIOWrapper):
        pass
    buf = io.BytesIO()
    fake = io.TextIOWrapper(buf)
    monkeypatch.setattr(ms.sys, "stdout", fake)
    assert ms.protocol_stdout() is buf
