"""Round 74: the fleet directory at scale, and the last symlink gaps in the log readers.

1. find_agent rescanned every row for every hit: 2,000 rows x 50 agents all named "Worker-1" took
   9.6 s on the event loop per fleet_tell / agent_message, and the ambiguity error listed all
   100,000 matches. Now one pass, at most 20 shown.
2. render() had no size bound: that tree rendered 35 MB (2 s) into a model's context through
   fleet_agents. Now capped near RENDER_MAX_CHARS with a footer; fleet_agents filters and renders
   in a worker thread and refuses a filter over 500 chars.
3. GET /api/fleet encoded 256 x 60 KB records on the loop (jsonable + FastAPI's encoder); now
   built once in a worker thread.
4. Telemetry opened logs with a plain open() (a planted symlink fed an outside file to the HUD; a
   FIFO named x.jsonl hung it) and the OSINT rollup followed symlinks.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from avrone import presence, telemetry
from avrone.presence import find_agent, render


def _rows(n_rows, n_agents, name="Worker-1", status="idle"):
    return [{"route": f"n{i}", "node_id": f"n{i}", "status": "online", "env": {},
             "agents": [{"name": name, "status": status, "task": "t" * 1000} for _ in range(n_agents)]}
            for i in range(n_rows)]


def test_find_agent_linear_on_huge_ambiguous_tree():
    rows = _rows(2000, 50)
    t = time.perf_counter()
    with pytest.raises(LookupError) as e:
        find_agent(rows, "Worker-1")
    took = time.perf_counter() - t
    assert took < 1.0, f"{took:.2f} s"  # was 9.6 s
    msg = str(e.value)
    assert "ambiguous" in msg and "more; give route/name" in msg and len(msg) < 2000


def test_find_agent_semantics_kept():
    rows = _rows(3, 1, name="A")
    rows[1]["agents"][0]["status"] = "working"
    assert find_agent(rows, "A") == ("n1", "A")  # the live one wins
    rows[2]["agents"][0]["status"] = "working"
    with pytest.raises(LookupError, match="ambiguous: n1/A, n2/A$"):
        find_agent(rows, "A")
    with pytest.raises(LookupError, match="no agent named"):
        find_agent(rows, "B")
    assert find_agent(rows, "n0/A") == ("n0", "A")
    with pytest.raises(LookupError, match="no node at route"):
        find_agent(rows, "zz/A")
    with pytest.raises(LookupError, match="no agent name"):
        find_agent(rows, "n0/")
    single = _rows(1, 1, name="Solo")
    assert find_agent(single, "Solo") == ("n0", "Solo")


def test_render_is_bounded():
    rows = _rows(2000, 50, status="working")
    t = time.perf_counter()
    out = render(rows)
    took = time.perf_counter() - t
    assert len(out) < presence.RENDER_MAX_CHARS + 50_000  # was 35 MB
    assert "output capped" in out and "of 2000 nodes shown" in out
    assert took < 0.5, f"{took:.2f} s"


def test_render_small_tree_unchanged_and_uncapped():
    rows = _rows(3, 2, name="X")
    out = render(rows)
    assert "capped" not in out and out.count("   - X") == 6


def test_fleet_agents_runs_off_loop_and_caps(monkeypatch):
    from avrone import fleet as fl
    rows = _rows(2000, 50, status="working")

    async def fake_dir(core):
        return rows
    monkeypatch.setattr(presence, "directory", fake_dir)
    monkeypatch.setattr(fl, "_fleet", lambda ctx: SimpleNamespace(node_id="root"))
    seen = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        seen.append(fn)
        return await real(fn, *a, **k)
    monkeypatch.setattr(fl.asyncio, "to_thread", spy)
    out = asyncio.run(fl.t_fleet_agents(SimpleNamespace(core=None), {}))
    assert seen and len(out) < presence.RENDER_MAX_CHARS + 50_000
    out = asyncio.run(fl.t_fleet_agents(SimpleNamespace(core=None), {"filter": "q" * 501}))
    assert out.startswith("ERROR: filter is longer")
    out = asyncio.run(fl.t_fleet_agents(SimpleNamespace(core=None), {"filter": "n7"}))
    assert "[n7]" in out


def test_fleet_info_built_off_loop(monkeypatch):
    from avrone import server
    big = {f"k{i}": "v" * 50 for i in range(900)}
    nodes = [{"node_id": f"n{i}", "capabilities": big, "last_seen": float("inf"), "status": "online"}
             for i in range(64)]
    f = SimpleNamespace(info=lambda: {"node_id": "root", "children": 64}, list_nodes=lambda: nodes)
    monkeypatch.setattr(server, "_fleet", lambda: f)
    monkeypatch.setattr(server, "require_admin", lambda t: None)
    seen = []
    real = asyncio.to_thread

    async def spy(fn, *a, **k):
        seen.append(fn)
        return await real(fn, *a, **k)
    monkeypatch.setattr(server.asyncio, "to_thread", spy)
    resp = asyncio.run(server.fleet_info(None))
    body = json.loads(resp.body)
    assert seen and body["children"] == 64 and len(body["nodes"]) == 64
    assert body["nodes"][0]["last_seen"] is None or isinstance(body["nodes"][0]["last_seen"], (int, float, str))
    assert resp.media_type == "application/json"


def test_telemetry_ignores_planted_symlink_and_fifo(tmp_path):
    logs = tmp_path / "runs"
    logs.mkdir()
    now = time.time()
    (logs / "good.jsonl").write_text(json.dumps({"run_id": "good", "kind": "k", "ts": now}) + "\n")
    outside = tmp_path / "secret.txt"
    outside.write_text(json.dumps({"run_id": "LEAK", "kind": "k", "ts": now}) + "\n")
    os.symlink(outside, logs / "planted.jsonl")
    os.mkfifo(logs / "pipe.jsonl")
    evs = list(telemetry._iter_events(logs, now - 60))  # a FIFO used to block here forever
    assert [e["run_id"] for e in evs] == ["good"]


def test_osint_metrics_ignores_planted_symlink(tmp_path):
    from avrone import osint
    logs = tmp_path / "runs"
    logs.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("\n".join(json.dumps({"kind": "recon.dns", "data": {"host": "leak.example"}})
                                 for _ in range(3)) + "\n")
    os.symlink(outside, logs / "planted.jsonl")
    ctx = SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path))))
    out = asyncio.run(osint.t_osint_metrics(ctx, {}))
    assert "leak.example" not in out
