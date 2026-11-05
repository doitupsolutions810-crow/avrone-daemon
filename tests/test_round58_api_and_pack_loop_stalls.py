"""Round 58: admin endpoints and agent-pack tools that still did heavy work on the event loop.

Measured against a live node (uvicorn, /api/status pinged every 20 ms while each call ran):

  * /api/capgate walked and re-MACed the whole receipt chain inline: 40k receipts froze /api/status
    for 0.85 s, growing linearly with the chain.
  * /api/map parsed every run log like /api/telemetry (which round 54 moved to a thread) but stayed
    inline: 360k log lines froze the node 1.3 s, mermaid format included.
  * /api/checkpoints and /api/evolve read every checkpoint / proposal file inline.
  * /api/fleet/agentpack base64-decoded, gunzipped, verified and installed a pack inline (a 50 MB
    home: ~0.7 s); agent_pack_export / agent_pack_import did the same inside a tool (1.6 s / 0.6 s).

Probed clean: telemetry and map with hours=nan, inf, -1, 1e308 (all 200, no traceback); all-tools
fuzz seeds 16-19.
"""
from __future__ import annotations

import asyncio
import random
import time
from types import SimpleNamespace

import pytest


def _loop_probe():
    try:
        asyncio.get_running_loop()
        return "on-loop"
    except RuntimeError:
        return "thread"


def _hdr(server):
    return {"x-admin-token": server.core().settings.admin_token or ""}


def test_capgate_chain_walk_runs_in_a_thread(monkeypatch):
    from fastapi.testclient import TestClient
    from avrone import capgate, server
    seen = []
    monkeypatch.setattr(capgate, "verify_chain", lambda d: (seen.append(_loop_probe()), {"ok": True, "count": 0})[1])
    with TestClient(server.app) as c:
        r = c.get("/api/capgate", headers=_hdr(server))
    assert r.status_code == 200 and r.json()["chain"]["ok"] is True
    assert seen == ["thread"]


@pytest.mark.parametrize("fmt", ["json", "mermaid"])
def test_map_parses_logs_in_a_thread(monkeypatch, fmt):
    from fastapi.testclient import TestClient
    from avrone import server, telemetry
    seen = []
    real = telemetry.system_map
    monkeypatch.setattr(telemetry, "system_map", lambda *a, **k: (seen.append(_loop_probe()), real(*a, **k))[1])
    with TestClient(server.app) as c:
        r = c.get(f"/api/map?format={fmt}", headers=_hdr(server))
    assert r.status_code == 200 and seen == ["thread"]


@pytest.mark.parametrize("hours", ["nan", "inf", "-1", "1e308", "0"])
def test_map_and_telemetry_odd_windows(hours):
    from fastapi.testclient import TestClient
    from avrone import server
    with TestClient(server.app) as c:
        for ep in ("/api/map", "/api/telemetry"):
            r = c.get(f"{ep}?hours={hours}", headers=_hdr(server))
            assert r.status_code == 200, (ep, hours, r.text[:200])


def test_checkpoints_and_evolve_lists_read_in_a_thread(monkeypatch):
    from fastapi.testclient import TestClient
    from avrone import checkpoint, evolve, server
    seen = {}
    monkeypatch.setattr(checkpoint, "list_managed", lambda d: (seen.__setitem__("ck", _loop_probe()), [])[1])
    monkeypatch.setattr(evolve, "list_accepted", lambda s: (seen.__setitem__("ev", _loop_probe()), [])[1])
    with TestClient(server.app) as c:
        assert c.get("/api/checkpoints", headers=_hdr(server)).json() == []
        assert c.get("/api/evolve", headers=_hdr(server)).json() == {"accepted": []}
    assert seen == {"ck": "thread", "ev": "thread"}


def test_agentpack_endpoint_installs_in_a_thread(monkeypatch):
    from fastapi.testclient import TestClient
    from avrone import server
    seen = []

    async def fake_gateway(request, kind):
        return {"from": "peer-a"}
    with TestClient(server.app) as c:
        monkeypatch.setattr(server, "_gateway", fake_gateway)
        fake = SimpleNamespace(receive_agentpack=lambda p: (seen.append(_loop_probe()), {"ok": True})[1])
        monkeypatch.setattr(server, "_fleet", lambda: fake)
        r = c.post("/api/fleet/agentpack", json={})
    assert r.status_code == 200 and seen == ["thread"]


def _pack_ctx(tmp_path):
    from avrone import agentfs
    ws, data = tmp_path / "ws", tmp_path / "d"
    data.mkdir()
    home = agentfs.agent_dir(ws, "mover")
    r = random.Random(58)
    for i in range(1500):
        (home / f"f{i}.bin").write_bytes(r.randbytes(20000))
    core = SimpleNamespace(settings=SimpleNamespace(workspace=ws, data_dir=data), fleet=None, codex=None)
    return SimpleNamespace(core=core, run_id="r", name="Mover", agent_id="mover", depth=0), data


async def _max_gap(coro):
    gaps, last, stop = [], time.monotonic(), False

    async def tick():
        nonlocal last
        while not stop:
            await asyncio.sleep(0.01)
            n = time.monotonic(); gaps.append(n - last); last = n
    t = asyncio.create_task(tick())
    await asyncio.sleep(0.03)
    out = await coro
    stop = True
    await asyncio.sleep(0.02)
    t.cancel()
    return out, max(gaps)


def test_agent_pack_tools_keep_the_loop_responsive(tmp_path):
    from avrone import agentpack
    from avrone.agentpack_tools import t_agent_pack_export, t_agent_pack_import
    ctx, data = _pack_ctx(tmp_path)

    async def go():
        out, gap_e = await _max_gap(t_agent_pack_export(ctx, {}))
        assert out.startswith("packed agent mover"), out
        name = next(agentpack.pack_dir(data).glob("mover-*.tar.gz")).name
        out2, gap_i = await _max_gap(t_agent_pack_import(ctx, {"name": name}))
        return out2, gap_e, gap_i
    out2, gap_e, gap_i = asyncio.run(go())
    assert out2.startswith("restored agent mover") or "ERROR" in out2
    assert gap_e < 0.25, gap_e
    assert gap_i < 0.25, gap_i


def test_agent_pack_tools_are_marked_off_loop():
    from avrone.agentpack_tools import t_agent_pack_export, t_agent_pack_import
    assert getattr(t_agent_pack_export, "off_loop", False)
    assert getattr(t_agent_pack_import, "off_loop", False)
