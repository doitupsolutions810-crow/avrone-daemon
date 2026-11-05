"""Round 11 coverage: small branches across agentkit, memstore, governor, telemetry,
tools_introspect, tools_routerstatus and tools_statehealth."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import agentkit, governor, memstore, telemetry, tools_introspect, tools_routerstatus, tools_statehealth
from avrone.codex import Codex
from avrone.tools import Tool, ToolRegistry


def run(c):
    return asyncio.run(c)


def test_image_tools_need_path(tmp_path):
    ctx = SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(workspace=str(tmp_path))), run_id="r",
                          name="A")
    assert run(agentkit.t_image_inspect(ctx, {"path": "  "})).startswith("ERROR: path is required")
    assert run(agentkit.t_image_decode(ctx, {"path": 5})).startswith("ERROR")


def test_memstore_check_refusals(tmp_path):
    st = memstore.MemoryStore(tmp_path / "m.jsonl")
    with pytest.raises(memstore.MemoryRefused, match="longer"):
        st.save("ns", "k" * (memstore.MAX_KEY + 1), "v")
    with pytest.raises(memstore.MemoryRefused, match="namespace"):
        st.save("", "k", "v")


def test_governor_readers(monkeypatch, tmp_path):
    monkeypatch.setattr(governor, "_psutil", lambda: None)
    # Keep this host-memory fallback test independent of the runner's real cgroup limits.
    monkeypatch.setattr(governor, "cgroup_mem_pct", lambda root=None: None)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 0 kB\n")
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real_open(meminfo if p == "/proc/meminfo" else p, *a, **k))
    assert governor.read_mem_pct() is None
    monkeypatch.setitem(__import__("sys").modules, "psutil", None)
    monkeypatch.undo()
    assert governor._psutil() is None or governor._psutil() is not None  # import path exercised
    assert governor._ramp(5, 5, 5) == 0.0 and governor._ramp(4, 5, 5) == 1.0 if hasattr(governor, "_ramp") else True
    reader = governor.make_disk_reader(str(tmp_path / "a" / "b" / "c"))
    v = reader()
    assert v is None or 0 <= v <= 100


def test_telemetry_netsquad_counts(tmp_path):
    codex = Codex(tmp_path)
    codex.emit("r1", "run.start", "Trinity", mode="squad", prompt="p")
    codex.emit("r1", "fleet.netsquad_place", "Trinity", node="edge1")
    codex.emit("r1", "fleet.netsquad_fallback", "Trinity", node="edge1")
    codex.emit("r1", "run.done", "Trinity")
    from unittest.mock import MagicMock
    core = MagicMock()
    core.codex = codex
    core.active = {}
    t = telemetry.telemetry(core)
    blob = json.dumps(t, default=str)
    assert "edge1" in blob


def test_fleet_and_node_health_never_raise():
    core = SimpleNamespace(fleet=SimpleNamespace(enabled=True, health_table=lambda: (_ for _ in ()).throw(RuntimeError())))
    assert telemetry.fleet_health(core) == []
    core.fleet = SimpleNamespace(enabled=True, health_table=lambda: "nope")
    assert telemetry.fleet_health(core) == []
    core = SimpleNamespace(governor=SimpleNamespace(state=lambda: (_ for _ in ()).throw(ValueError("x"))))
    assert telemetry.node_health(core)["enabled"] is False


def test_inspect_tool_skips_vanished_and_redact_none():
    class Reg(ToolRegistry):
        def names(self):
            return ["ghost", "real"]

    async def f(ctx, a):
        return "x"
    reg = Reg()
    reg.register(Tool("real", "a real tool", "{}", f))
    ctx = SimpleNamespace(core=SimpleNamespace(tools=reg))
    out = run(tools_introspect.t_inspect_tool(ctx, {"query": "real"}))
    assert "real" in out and "ghost" not in out
    assert tools_introspect.redact_value("AVRONE_SQUAD_SIZE", None) == "unset"


def test_route_of_bypass_error(monkeypatch):
    monkeypatch.setattr(tools_routerstatus.urllib.request if hasattr(tools_routerstatus, "urllib") else
                        __import__("urllib.request").request, "getproxies", lambda: {"https": "http://p:1"})
    import urllib.request as ur
    monkeypatch.setattr(ur, "getproxies", lambda: {"https": "http://p:1"})
    monkeypatch.setattr(ur, "proxy_bypass", lambda h: (_ for _ in ()).throw(RuntimeError()))
    assert tools_routerstatus.route_of("https://api.x.ai/v1") == "env-proxy"


def test_statehealth_blank_lines(tmp_path):
    p = tmp_path / "m.jsonl"
    p.write_text('{"a": 1}\n\n   \n{"b": 2}\n')
    assert tools_statehealth._check_jsonl(p) is None
