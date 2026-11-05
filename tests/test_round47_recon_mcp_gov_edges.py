"""Round 47: recon target smuggling, hostile MCP servers, governor/telemetry and fleet strict JSON.

  * recon: private addresses hidden in names the string guard missed: compressed IPv6 on
    sslip.io/nip.io (0--1 = ::1, fe80--1), dotted quads glued to text (customer-10.0.0.1.nip.io,
    make-127.0.0.1-rr.1u.ms), DNS-rebinding services (rbndr.us hex pairs); plus a seeded corpus of
    encodings (full-width, ideographic dots, zero-width, case, trailing dots, ports, schemes) where
    nothing private ever passes and ordinary public names still do;
  * MCP: a server listing 100 pages of tools is capped at MAX_TOOLS_PER_SERVER; schemas with
    unhashable "type", 100k properties, junk "required" never drop a tool or blow up the doc;
  * governor: +-inf sensor readings no longer 500 /api/telemetry; junk thresholds never raise;
  * fleet: a peer registering Infinity/NaN/bytes in capabilities/presence no longer 500s /api/fleet.
"""
from __future__ import annotations

import json
import math
import random

import pytest

from avrone import recon
from avrone.mcp_client import MAX_TOOLS_PER_SERVER, MCPClient, args_doc_from_schema


# ------------------------------------------------------------------ recon
@pytest.fixture
def any_public(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")
    monkeypatch.setenv("AVRONE_RECON", "allow")


SMUGGLED = [
    "0--1.sslip.io", "--1.sslip.io", "fe80--1.sslip.io", "fc00--1.nip.io", "fd12-3456--1.sslip.io",
    "0-0-0-0-0-0-0-1.sslip.io", "0-0-0-0-0-ffff-7f00-1.sslip.io", "--ffff-a9fe-a9fe.sslip.io",
    "customer-10.0.0.1.nip.io", "app-192.168.1.5.sslip.io", "make-127.0.0.1-rr.1u.ms",
    "make-169.254.169.254-rebind-8.8.8.8-rr.1u.ms", "7f000001.c0a80001.rbndr.us", "08080808.a9fea9fe.rbndr.us",
    "a.10.0.0.1-x.example.com", "x.172.16.0.1.example.org",
]


@pytest.mark.parametrize("host", SMUGGLED)
def test_private_address_smuggled_in_a_name_is_refused(any_public, host):
    for form in (host, host.upper(), host + ".", f"https://{host}/x", f"http://{host}:8080"):
        _, err = recon._host_arg({"host": form})
        assert err and "private" in err, (form, err)


PUBLIC = ["example.com", "2a01-4f8-c17-b8f--2.sslip.io", "8.8.8.8.nip.io", "my-app.example.com",
          "cafe-babe.example.com", "dead--beef.example.com", "1-2.example.com", "a-1.2.3.4-b.example.com",
          "xn--bcher-kva.de", "bücher.de", "sub.domain.co.uk"]


@pytest.mark.parametrize("host", PUBLIC)
def test_ordinary_public_names_still_pass(any_public, host):
    assert recon._host_arg({"host": host})[1] is None, host


def _encodings(r: random.Random, base: str) -> str:
    fw = str.maketrans("0123456789.abcdefghijklmnopqrstuvwxyz",
                       "０１２３４５６７８９．ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ")
    ops = [
        lambda s: s.upper(), lambda s: s + ".", lambda s: s.translate(fw), lambda s: s.replace(".", "\u3002"),
        lambda s: s.replace(".", "\uff61"), lambda s: s[:1] + "\u200b" + s[1:],  # zero-width INSIDE a label (IDNA drops it)
        lambda s: "https://" + s + "/p?q#f", lambda s: s + ":443", lambda s: "HTTP://" + s,
        lambda s: s[:2] + "\u00ad" + s[2:], lambda s: s,
    ]
    for _ in range(r.randint(1, 3)):
        base = r.choice(ops)(base)
    return base


PRIVATE_BASES = ["localhost", "127.0.0.1", "10.0.0.1", "169.254.169.254", "metadata.google.internal",
                 "foo.internal", "router.lan", "printer.local", "kubernetes.default.svc", "0x7f.1",
                 "2130706433", "017700000001", "127.1", "::1", "[::ffff:127.0.0.1]", "10.0.0.1.nip.io",
                 "10-0-0-1.sslip.io", "7f000001.nip.io", "0--1.sslip.io", "x.localtest.me",
                 "customer-10.0.0.1.nip.io", "make-127.0.0.1-rr.1u.ms"]


@pytest.mark.parametrize("seed", range(6))
def test_seeded_encodings_never_let_a_private_target_through(any_public, seed):
    r = random.Random(4700 + seed)
    for _ in range(400):
        form = _encodings(r, r.choice(PRIVATE_BASES))
        _, err = recon._host_arg({"host": form})
        assert err is not None, repr(form)


def test_host_arg_never_raises_on_junk(any_public):
    r = random.Random(47)
    pool = "abc.-:/@[]%\x00\ud800\u3002\uff0e\u200b0123456789xn--" + "漢字"
    for _ in range(3000):
        raw = "".join(r.choice(pool) for _ in range(r.randint(0, 80)))
        host, err = recon._host_arg({"host": raw})
        assert isinstance(host, str) and (err is None or err.startswith("ERROR"))
    for v in [None, 0, 1.5, float("nan"), [], {}, b"example.com", True, "x" * 5000]:
        recon._host_arg({"host": v})


# ------------------------------------------------------------------ mcp
class _PagedServer:
    """Stands in for a transport: endless pages of tools, each page 300 tools."""
    kind = "fake"

    def __init__(self):
        self.calls = 0

    async def request(self, method, params, timeout):
        self.calls += 1
        page = self.calls
        return {"tools": [{"name": f"t{page}_{i}", "inputSchema": {}} for i in range(300)],
                "nextCursor": f"c{page}"}


@pytest.mark.asyncio
async def test_endless_tool_pages_are_capped():
    c = MCPClient("flood", {"command": "true"})
    fake = _PagedServer()

    async def req(method, params, timeout):
        return await fake.request(method, params, timeout)
    c._request = req  # type: ignore[assignment]
    tools = await c.list_tools()
    assert len(tools) == MAX_TOOLS_PER_SERVER
    assert fake.calls <= MAX_TOOLS_PER_SERVER // 300 + 1


HOSTILE_SCHEMAS = [
    {"properties": {"a": {"type": {"x": 1}}}},
    {"properties": {"a": {"type": [{"x": 1}, "string", None, 5]}}},
    {"properties": {"a": {"type": ["null"]}}},
    {"properties": {f"k{i}": {"type": "string"} for i in range(100_000)}, "required": [f"k{i}" for i in range(50)]},
    {"properties": {"x" * 100_000: {"type": "string"}}},
    {"properties": {"a": {"enum": [float("nan"), b"x", {"d": 1}]}}},
    {"properties": "nope", "required": "a"},
    {"properties": {"a": None, "b": 5, "c": []}, "required": [None, {"x": 1}, ["y"]]},
    {"properties": {1: {"type": "integer"}, None: {}}},
    None, [], "schema", 42,
]


@pytest.mark.parametrize("i", range(len(HOSTILE_SCHEMAS)))
def test_hostile_schema_docs_are_bounded_and_never_raise(i):
    doc = args_doc_from_schema(HOSTILE_SCHEMAS[i])
    assert isinstance(doc, str) and len(doc) <= 300 and doc.startswith("{") and doc.endswith("}")


@pytest.mark.asyncio
async def test_registration_keeps_tools_with_hostile_schemas(tmp_path):
    from avrone.mcp_client import MCPManager
    from avrone.tools import ToolRegistry

    class FakeClient:
        kind = "fake"
        timeout = 5.0
        name = "evil"
        server_info: dict = {}
        protocol_version = "2025-06-18"
        tools = [{"name": f"tool{i}", "inputSchema": s, "description": {"not": "a string"}}
                 for i, s in enumerate(HOSTILE_SCHEMAS)]

        def __init__(self, *a, **k):
            pass

        async def connect(self):
            pass

        async def initialize(self):
            return {}

        async def list_tools(self):
            return self.tools

    import avrone.mcp_client as mc
    orig = mc.MCPClient
    mc.MCPClient = FakeClient  # type: ignore[assignment]
    try:
        cfg = tmp_path / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": {"evil": {"command": "true"}}}))
        mgr = MCPManager(config_path=cfg)
        reg = ToolRegistry()
        await mgr.start(reg)
    finally:
        mc.MCPClient = orig
    names = [n for n in reg.names() if n.startswith("mcp__evil__")]
    assert len(names) == len(HOSTILE_SCHEMAS), names


# ------------------------------------------------------------------ governor / telemetry
def test_infinite_sensor_readings_do_not_500_telemetry(monkeypatch):
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "all")
    from fastapi.testclient import TestClient
    from avrone.server import app, core
    with TestClient(app, raise_server_exceptions=False) as c:
        gov = core().governor
        saved = dict(gov.readers)
        try:
            gov.readers.update({"load": lambda: float("inf"), "temp_c": lambda: -float("inf"),
                                "mem_pct": lambda: 1e308, "disk_free_pct": lambda: float("nan")})
            gov._cache = None
            for q in ["", "?hours=nan", "?hours=inf", "?hours=-1e308", "?hours=0"]:
                r = c.get("/api/telemetry" + q)
                assert r.status_code == 200, (q, r.text[:200])
                json.loads(r.text, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
            if gov.enabled:
                assert gov.target() == gov.floor  # inf load still throttles fully
        finally:
            gov.readers.clear()
            gov.readers.update(saved)
            gov._cache = None


@pytest.mark.parametrize("seed", range(3))
def test_governor_junk_everything_never_raises(seed):
    from avrone.governor import Governor
    r = random.Random(seed)
    junk = [float("nan"), float("inf"), -float("inf"), "x", None, -5, 0, 10**400, "1e999", [], b"\xff", True]
    for _ in range(300):
        vals = {k: r.choice(junk) for k in ("load", "mem_pct", "disk_free_pct", "temp_c", "thermal_limit_pct")}

        def mk(v):
            def f():
                if isinstance(v, list):
                    raise OSError("sensor gone")
                return v
            return f
        g = Governor(max_parallel=r.choice(junk + [8]), min_parallel=r.choice(junk + [2]),
                     thermal_max_c=r.choice(junk), ttl_s=r.choice(junk + [0]),
                     readers={k: mk(v) for k, v in vals.items()})
        t = g.target(want=r.choice(junk + [3]))
        assert 1 <= t <= g.max_parallel
        g.is_hot()
        json.dumps(g.state(), allow_nan=False)


# ------------------------------------------------------------------ fleet
def test_peer_registering_infinity_is_stored_as_strict_json(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    from avrone.fleet import FleetManager
    monkeypatch.delenv("AVRONE_FLEET_ALLOW", raising=False)
    c = MagicMock()
    c.active = {}
    c.fleet_hops = 0
    f = FleetManager(c, tmp_path)
    f.allow, f.require_allow = ["edge-inf"], True
    payload = {"node_id": "edge-inf", "url": "http://127.0.0.1:8799",
               "capabilities": {"gpu": float("inf"), "x": float("nan"), "s": {1, 2}, "b": b"\xff"},
               "presence": {"score": -float("inf")}, "load": float("inf")}
    f.register(payload)
    nodes = f.list_nodes()
    json.dumps(nodes, allow_nan=False)            # used to raise: /api/fleet answered 500
    node = next(n for n in nodes if n["node_id"] == "edge-inf")
    assert node["capabilities"]["gpu"] is None and node["presence"]["score"] is None
    assert node["load"] == 0  # existing node_load contract: non-finite is junk
    # and the saved registry reloads as strict JSON too
    for p in tmp_path.rglob("*.json"):
        json.loads(p.read_text(), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
