import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from avrone import tools_statehealth as sh
from avrone.fleet import FleetManager
from avrone.tools import Tool, ToolRegistry, builtin_registry
from avrone.tools_extra import register_extra_tools


def mk(tmp_path, reg=None):
    if reg is None:
        reg = builtin_registry()
        register_extra_tools(reg)
    core = SimpleNamespace(tools=reg, settings=SimpleNamespace(data_dir=str(tmp_path / "d")))
    return reg, SimpleNamespace(core=core, run_id="r", name="A", depth=0)


def call(reg, ctx, name, **a):
    return asyncio.run(reg.call(ctx, name, a))


def test_registered_and_schemas():
    reg, _ = mk(__import__("pathlib").Path("/tmp/none-x"))
    assert reg.check("state_health", {"verbose": "yes"})[0] == "bad_args"
    assert reg.check("registry_audit", {}) is None


def test_no_data_dir(tmp_path):
    reg, ctx = mk(tmp_path)
    assert "does not exist yet" in call(reg, ctx, "state_health")
    ctx.core.settings = SimpleNamespace(data_dir=None)
    assert "no data_dir" in call(reg, ctx, "state_health")


def test_healthy_then_problems(tmp_path, monkeypatch):
    reg, ctx = mk(tmp_path)
    d = tmp_path / "d"
    (d / "evolve" / "loops").mkdir(parents=True)
    (d / "bridge").mkdir()
    (d / "checkpoints").mkdir()
    (d / "evolve" / "p1.json").write_text("{}")
    (d / "bridge" / "r1.jsonl").write_text('{"a": 1}\n')
    (d / "memory.jsonl").write_text('{"ns": "x"}\n')
    out = call(reg, ctx, "state_health")
    assert out.startswith("state_health: HEALTHY") and "evolve proposals: 1 file(s), 0 bad" in out
    assert "checkpoints" not in out and "checkpoints: 0" in call(reg, ctx, "state_health", verbose=True)
    secret = "TOPSECRETVALUE"
    (d / "evolve" / "loops" / "L.json").write_text("{" + secret)
    (d / "bridge" / "r2.jsonl").write_text('{"a": 1}\n{"b"')
    (d / "bridge" / "r3.jsonl").write_text('{"a": 1}\nnot json\n')
    (d / "checkpoints" / "c.json").write_bytes(b"\xff\xfe")
    (d / "fleet.json").write_text("{")
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "t")
    FleetManager(MagicMock(), d)  # moves the corrupt fleet.json aside
    out = call(reg, ctx, "state_health")
    assert out.startswith("state_health: ATTENTION")
    assert "evolve/loops/L.json: invalid json" in out and "bridge/r2.jsonl: torn last line" in out
    assert "bridge/r3.jsonl: 1 undecodable line(s)" in out and "checkpoints/c.json: not utf-8" in out
    assert "set aside: fleet.json.corrupt-" in out and secret not in out


def test_caps(tmp_path, monkeypatch):
    reg, ctx = mk(tmp_path)
    d = tmp_path / "d" / "evolve"
    d.mkdir(parents=True)
    for i in range(20):
        (d / f"p{i}.json").write_text("{")
    (d / ".hidden.json").write_text("{")
    monkeypatch.setattr(sh, "MAX_FILES_PER_AREA", 18)
    out = call(reg, ctx, "state_health")
    assert "18 file(s), 18 bad" in out and "... 3 more" in out and ".hidden" not in out
    monkeypatch.setattr(sh, "MAX_JSONL_BYTES", 1)
    (tmp_path / "d" / "memory.jsonl").write_text("garbage")
    assert sh._check_jsonl(tmp_path / "d" / "memory.jsonl") is None


def test_unreadable(tmp_path, monkeypatch):
    p = tmp_path / "x.json"
    p.write_text("{}")
    monkeypatch.setattr(type(p), "read_text", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(type(p), "read_bytes", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    import avrone.safeio as _sio  # round 85: _check_json reads through safeio.read_capped
    monkeypatch.setattr(_sio, "read_capped", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    assert sh._check_json(p) == "unreadable (PermissionError)"
    assert sh._check_jsonl(p) == "unreadable (PermissionError)"


def test_registry_audit(tmp_path):
    reg, ctx = mk(tmp_path)
    out = call(reg, ctx, "registry_audit")
    n = len(reg.names())
    assert out.startswith(f"registry_audit: {n} tool(s)") and "builtin=" in out and "no arg schema (" in out
    assert "state_health" not in out.split("no arg schema")[1]
    assert "no tools from source 'nope'" in call(reg, ctx, "registry_audit", source="nope")
    a = sh.registry_audit(reg)
    assert a["acting"] + a["read_only"] == n and a["acting"] > 5


def test_registry_audit_custom_and_missing(tmp_path):
    r = ToolRegistry()

    async def f(ctx, args):
        return "x"
    r.register(Tool("shell", "d", "{}", f, source="mcp:demo"))
    r.register(Tool("think", "d", "{}", f, schema={}))
    _, ctx = mk(tmp_path, r)
    out = asyncio.run(sh.t_registry_audit(ctx, {"source": "mcp:demo"}))
    assert "1 tool(s) from mcp:demo; 1 acting, 0 read-only" in out and "shell" in out
    ctx.core.tools = None
    assert "no tool registry" in asyncio.run(sh.t_registry_audit(ctx, {}))
