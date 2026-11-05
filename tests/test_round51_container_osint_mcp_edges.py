"""Round 51: agent home containers, osint_domain sections, MCP client streams and config.

  * agentcontainer: a same-named container from ANOTHER workspace on the host (two AVRONE
    instances share node id + role) was adopted when running (this agent got the other /agent
    mount) or rm -f'd when stopped; launcher(timeout=inf/10**400) raised OverflowError;
    explicit mem/cpu NaN/0/-1/inf reached docker verbatim; idle=NaN never reaped, negative
    reaped every call;
  * osint_domain: a raising (or None-returning) sub-probe sank the profile; sections unbounded;
    osint_metrics listed every one of 100k junk kinds;
  * mcp_client: SSE lines were buffered without a cap (1 GB with no newline, or a huge non-data
    line); mcp.json as a FIFO hung startup, a 50 MB or 100k-deep config was read/parsed whole,
    10k servers were all spawned.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from avrone import agentcontainer as ac
from avrone import mcp_client as mc
from avrone import osint, recon
from avrone.sovereign import CommandResult


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------------ agentcontainer
class Docker:
    def __init__(self, inspect_out: str | None):
        self.inspect_out, self.calls = inspect_out, []

    async def __call__(self, argv, timeout):
        self.calls.append(argv)
        verb = argv[1]
        if verb == "inspect":
            if self.inspect_out is None:
                return CommandResult("inspect", 1, "", "Error: No such object")
            return CommandResult("inspect", 0, self.inspect_out, "")
        return CommandResult(verb, 0, "id\n", "")

    def verbs(self):
        return [a[1] for a in self.calls]


def _pool(fake, tmp_path):
    return ac.AgentContainerPool(fake, "img", False, node="n1", clock=lambda: 100.0, idle=600)


@pytest.mark.parametrize("state", ["true", "false"])
def test_foreign_container_is_neither_adopted_nor_removed(tmp_path, state):
    mine = tmp_path / "ws1" / "agents" / "coder"
    other = tmp_path / "ws2" / "agents" / "coder"
    mine.mkdir(parents=True)
    other.mkdir(parents=True)
    fake = Docker(f"{state}|{other.resolve()}")
    pool = _pool(fake, tmp_path)
    with pytest.raises(ac.ForeignContainer):
        run(pool.ensure(mine))
    assert fake.verbs() == ["inspect"] and pool.homes == {}


def test_own_container_adopted_and_old_output_still_accepted(tmp_path):
    mine = tmp_path / "agents" / "coder"
    mine.mkdir(parents=True)
    fake = Docker(f"true|{mine.resolve()}")
    assert run(_pool(fake, tmp_path).ensure(mine)).endswith("coder") and fake.verbs() == ["inspect"]
    fake2 = Docker("true")  # pre-v0.44 format / older docker: no mount reported
    run(_pool(fake2, tmp_path).ensure(mine))
    assert fake2.verbs() == ["inspect"]
    fake3 = Docker(f"false|{mine.resolve()}")  # our own stopped leftover: replaced as before
    run(_pool(fake3, tmp_path).ensure(mine))
    assert fake3.verbs() == ["inspect", "rm", "run"]


def test_parse_inspect():
    assert ac.parse_inspect("true|/x\n") == (True, "/x")
    assert ac.parse_inspect("TRUE") == (True, "")
    assert ac.parse_inspect(None) == (False, "")
    assert ac.parse_inspect("false|") == (False, "")
    assert "/agent" in ac.inspect_argv("c")[3]


@pytest.mark.parametrize("t", [float("inf"), 10**400, float("-inf"), float("nan"), "x", None, -3])
def test_launcher_junk_timeouts(t):
    argv = ac.launcher("ls", t)
    assert argv[-1] == "ls"
    if t is not None:
        assert argv[3] in ("0", str(ac.MAX_GUARD_SECS + ac.DEADLINE_SLACK))


@pytest.mark.parametrize("mem,cpu", [(float("nan"), float("nan")), (0, 0), (-1, -1), (float("inf"), float("inf")),
                                     ("x", "y"), (10**400, 10**400)])
def test_hardening_junk_limits(mem, cpu):
    a = ac.hardening_args(False, mem, cpu)
    m = a[a.index("--memory") + 1]
    c = a[a.index("--cpus") + 1]
    assert m.endswith("m") and int(m[:-1]) >= ac.MIN_MEM_MB
    assert 0 < float(c) <= ac.MAX_CPUS and c not in ("nan", "inf")


@pytest.mark.parametrize("idle", [float("nan"), -5, "junk"])
def test_idle_junk(tmp_path, idle):
    now = [0.0]
    fake = Docker(None)
    pool = ac.AgentContainerPool(fake, "img", False, node="n", clock=lambda: now[0], idle=idle)
    d = tmp_path / "a"
    d.mkdir()
    run(pool.ensure(d))
    now[0] = 1.0  # one second later: a negative idle used to reap at once
    assert run(pool.reap_idle()) == []
    now[0] = 10 * 86400.0
    assert run(pool.reap_idle()) != []  # NaN used to never reap


# ------------------------------------------------------------------ osint
class _Ctx:
    def __init__(self, tmp):
        self.run_id, self.name = "r", "a"
        self.core = SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None),
                                    settings=SimpleNamespace(data_dir=str(tmp)))


def test_osint_domain_survives_bad_sections(monkeypatch, tmp_path):
    monkeypatch.setattr(recon, "_gate", lambda ctx, tool, args: ("example.com", None))

    async def boom(ctx, args):
        raise RuntimeError("resolver exploded")

    async def none(ctx, args):
        return None

    async def huge(ctx, args):
        return "x" * 5_000_000
    monkeypatch.setattr(recon, "t_dns_enum", boom)
    monkeypatch.setattr(recon, "t_tls_inspect", huge)
    monkeypatch.setattr(recon, "t_http_fingerprint", none)
    monkeypatch.setattr(recon, "t_cert_transparency", none)
    out = run(osint.t_osint_domain(_Ctx(tmp_path), {"host": "example.com"}))
    assert "ERROR: RuntimeError" in out and "[TLS]" in out and len(out) < 50_000


def test_osint_metrics_kind_list_bounded(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    with (runs / "r.jsonl").open("w") as f:
        for i in range(5000):
            f.write(json.dumps({"kind": f"recon.junk{i}", "data": {"host": "h"}}) + "\n")
            f.write(json.dumps({"kind": recon.REFUSED_KIND, "data": {"tool": f"t{i}"}}) + "\n")
    out = run(osint.t_osint_metrics(_Ctx(tmp_path), {}))
    assert len(out.splitlines()) < 60 and "more kinds" in out and "total probes: 5000" in out


# ------------------------------------------------------------------ mcp
def _http_transport(handler):
    t = mc.HTTPTransport("http://mcp.test/mcp")
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return t


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for c in self.chunks:
            yield c


@pytest.mark.parametrize("chunks", [
    [b"data: " + b"a" * 65536 for _ in range(40)],           # one endless data line, no newline
    [b"event: " + b"e" * 65536 for _ in range(40)],          # a huge non-data line
    [b": " + b"c" * 65536 for _ in range(40)],               # a huge comment
])
def test_sse_long_lines_capped(monkeypatch, chunks):
    monkeypatch.setenv("AVRONE_MCP_MAX_RESPONSE_BYTES", str(1024 * 1024))
    t = _http_transport(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                    stream=_Stream(chunks)))
    with pytest.raises(mc.MCPError, match="larger than"):
        run(t.request("tools/list", None, 10))


@pytest.mark.parametrize("sep", [b"\n", b"\r\n", b"\r"])
def test_sse_line_endings_still_parse(sep):
    body = b": hi" + sep + b"event: message" + sep + b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":1}}' + sep + sep
    # split mid-CRLF too
    chunks = [body[:len(body) // 2], body[len(body) // 2:]]
    t = _http_transport(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                    stream=_Stream(chunks)))
    assert run(t.request("x", None, 10)) == {"ok": 1}


def test_mcp_config_guards(tmp_path):
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"mcpServers": {}, "pad": "x" * (mc.MAX_CONFIG_BYTES + 5)}))
    with pytest.raises(ValueError, match="larger"):
        mc.load_config(big)
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 100_000)
    with pytest.raises(ValueError):
        mc.load_config(deep)
    many = tmp_path / "many.json"
    many.write_text(json.dumps({"mcpServers": {f"s{i}": {"command": "true"} for i in range(mc.MAX_SERVERS + 1)}}))
    with pytest.raises(ValueError, match="at most"):
        mc.load_config(many)
    with pytest.raises(ValueError, match="regular"):
        mc.load_config(tmp_path)
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps({"mcpServers": {"a": {"command": "x"}, "b": {"command": "y", "disabled": True}}}))
    assert list(mc.load_config(ok)) == ["a"]


def test_mcp_config_fifo_does_not_hang(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs")
    p = tmp_path / "mcp.json"
    os.mkfifo(p)
    box = {}

    def go():
        try:
            mc.load_config(p)
        except ValueError as e:
            box["e"] = e
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(5)
    assert not th.is_alive() and "e" in box


def test_manager_reports_bad_config_instead_of_spawning(tmp_path):
    many = tmp_path / "many.json"
    many.write_text(json.dumps({"mcpServers": {f"s{i}": {"command": "true"} for i in range(500)}}))
    m = mc.MCPManager(config_path=many)
    st = run(m.start(SimpleNamespace(register=lambda *a, **k: None)))
    assert st["_config"]["connected"] is False and m.clients == {}
