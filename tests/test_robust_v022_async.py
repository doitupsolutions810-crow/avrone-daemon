"""v0.22 robust: asyncio-level races (ONE event loop, tasks interleaving at await points).

Every test hammers a check-then-act path with asyncio.gather, repeated inside the test, and asserts
the invariant held on every round. Offline: in-process ASGI transport, fake cores / runners.
"""
import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from avrone import checkpoint as ckm
from avrone import evolve
from avrone import fleet as fleet_mod
from avrone import memstore
from avrone.checkpoint import CheckpointConflict
from avrone.fleet import MEMORY_PATH, FleetManager, sign

ENV = ("AVRONE_FLEET_TOKEN", "AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_ALLOW", "AVRONE_FLEET_PARENT",
       "AVRONE_PUBLIC_URL", "AVRONE_FLEET_HEARTBEAT", "AVRONE_NODE_ID", "AVRONE_FLEET_REQUIRE_ALLOW",
       "AVRONE_FLEET_PRIVILEGED", "AVRONE_PRIVILEGED_NODES", "AVRONE_FLEET_PRIVILEGED_MAX_HOPS",
       "AVRONE_EVOLVE_MAX_BG", "AVRONE_SELF_EVOLVE", "AVRONE_AGENT_MIGRATE")
ROUNDS = 20


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)
    evolve._BG.clear()
    yield
    evolve._BG.clear()


async def yield_n(n=3):
    for _ in range(n):
        await asyncio.sleep(0)


# ================================================================ /api/chat, same checkpoint_id
class CkptCore:
    """Fake core: a squad run that holds its checkpoint across several await points and records how
    many runs are inside the same checkpoint at once."""

    def __init__(self):
        self.inside: dict[str, int] = {}
        self.max_inside = 0
        self.runs = 0

    async def squad(self, message, n=None, run_id=None, checkpoint=None, **kw):
        k = str(checkpoint)
        self.inside[k] = self.inside.get(k, 0) + 1
        self.max_inside = max(self.max_inside, self.inside[k])
        self.runs += 1
        try:
            await yield_n(5)
            return {"run_id": run_id, "answer": "ok", "agents": []}
        finally:
            self.inside[k] -= 1


@pytest.fixture
def api(tmp_path, monkeypatch):
    from avrone import server
    core = CkptCore()
    monkeypatch.setattr(server, "core", lambda: core)
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    monkeypatch.setitem(server.state, "tasks", set())
    return SimpleNamespace(server=server, core=core)


def test_concurrent_api_chat_same_checkpoint_runs_once(api):
    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.server.app), base_url="http://t") as c:
            out = []
            for r in range(ROUNDS):
                body = {"message": "build it", "mode": "squad", "checkpoint_id": f"ck{r % 3}"}
                rs = await asyncio.gather(*(c.post("/api/chat", json=body) for _ in range(8)))
                out.append(sorted(x.status_code for x in rs))
            return out
    rounds = run(main())
    for codes in rounds:
        assert codes.count(200) >= 1 and set(codes) <= {200, 409}
    assert api.core.max_inside == 1, "two runs were inside one checkpoint at the same time"
    assert not ckm._active, "a checkpoint claim leaked"


def test_concurrent_background_chat_same_checkpoint_single_owner(api):
    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.server.app), base_url="http://t") as c:
            for r in range(ROUNDS):
                body = {"message": "bg", "mode": "squad", "checkpoint_id": "bgck", "background": True}
                rs = await asyncio.gather(*(c.post("/api/chat", json=body) for _ in range(6)))
                codes = sorted(x.status_code for x in rs)
                assert codes.count(200) == 1 and codes.count(409) == 5, codes
                await asyncio.gather(*list(api.server.state["tasks"]))
                assert not ckm._active  # the background run released its claim when it ended
    run(main())
    assert api.core.max_inside == 1


def test_concurrent_fleet_runs_same_checkpoint_from_one_sender(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "edge")
    core = MagicMock()
    core.codex.emit = MagicMock()
    core.fleet_hops, core.fleet_privileged = 0, ""
    inside = {"n": 0, "max": 0}

    async def squad(prompt, n=None, checkpoint=None, **kw):
        inside["n"] += 1
        inside["max"] = max(inside["max"], inside["n"])
        await yield_n(4)
        inside["n"] -= 1
        return {"run_id": "r", "answer": "ok"}
    core.squad = squad
    fm = FleetManager(core, tmp_path)

    async def main():
        p = {"mode": "squad", "prompt": "x", "hops": 1, "from": "router", "checkpoint_id": "job1"}
        for _ in range(ROUNDS):
            res = await asyncio.gather(*(fm.handle_run(dict(p)) for _ in range(6)), return_exceptions=True)
            ok = [r for r in res if isinstance(r, dict)]
            bad = [r for r in res if not isinstance(r, dict)]
            assert len(ok) == 1 and all(isinstance(b, CheckpointConflict) for b in bad), res
    run(main())
    assert inside["max"] == 1 and not ckm._active


# ================================================================ evolve_start vs AVRONE_EVOLVE_MAX_BG
def _evolve_ctx(tmp_path):
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path / "d")), runner=None,
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r1", name="Evolver-1", depth=0)


@pytest.mark.parametrize("cap", ["1", "2", "4"])
def test_concurrent_evolve_start_never_exceeds_max_bg(tmp_path, monkeypatch, cap):
    monkeypatch.setenv("AVRONE_EVOLVE_MAX_BG", cap)
    release = {}

    async def fake_loop(ctx, goal, **kw):
        await release["ev"].wait()
        return {"loop_id": kw.get("loop_id"), "stopped": "max_iters", "status": "done"}
    monkeypatch.setattr(evolve, "evolve_loop", fake_loop)
    ctx = _evolve_ctx(tmp_path)

    async def main():
        peak = 0
        for _ in range(ROUNDS):
            release["ev"] = asyncio.Event()

            async def starter():
                await asyncio.sleep(0)
                return await evolve.evolve_start(ctx, "goal", candidates=[[{"path": "a", "content": "b"}]])
            res = await asyncio.gather(*(starter() for _ in range(10)))
            started = [r for r in res if r.get("loop_id")]
            peak = max(peak, len(evolve._bg_running()))
            assert len(started) == int(cap), res
            assert all("already running" in r["error"] for r in res if not r.get("loop_id"))
            release["ev"].set()
            await asyncio.gather(*list(evolve._BG.values()))
            assert evolve._bg_running() == []
        return peak
    assert run(main()) == int(cap)


# ================================================================ fleet register / heartbeat, same node id
def _fleet(tmp_path, monkeypatch, nid="parent"):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", nid)
    core = MagicMock()
    core.active = {}
    core.codex.emit = MagicMock()
    return FleetManager(core, tmp_path / nid)


def _signed(path, payload, token="tok"):
    body = json.dumps(payload).encode()
    return body, {"content-type": "application/json", **sign(token, "POST", path, body)}


def test_concurrent_register_same_node_id_one_url_wins(tmp_path, monkeypatch):
    from avrone import server
    fm = _fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "core", lambda: SimpleNamespace(fleet=fm))

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://t") as c:
            for r in range(ROUNDS):
                nid = f"child{r}"
                reqs = []
                for i in range(10):
                    url = "http://a" if i % 2 == 0 else "http://b"
                    body, h = _signed("/api/fleet/register", {"node_id": nid, "url": url, "load": i})
                    reqs.append(c.post("/api/fleet/register", content=body, headers=h))
                rs = await asyncio.gather(*reqs)
                winners = {("http://a" if i % 2 == 0 else "http://b")
                           for i, x in enumerate(rs) if x.status_code == 200}
                assert len(winners) == 1, [x.status_code for x in rs]
                assert all(x.status_code in (200, 400) for x in rs)
                assert fm.nodes[nid]["url"] in winners
                disk = json.loads(fm.path.read_text())["nodes"]
                assert disk[nid]["url"] == fm.nodes[nid]["url"]  # memory and fleet.json agree
    run(main())


def test_concurrent_heartbeats_and_listing_same_node_id(tmp_path, monkeypatch):
    """Heartbeats for one id (same url) interleaved with list_nodes/health readers: every heartbeat
    is accepted, readers never crash, the record is the latest one and fleet.json is valid JSON."""
    fm = _fleet(tmp_path, monkeypatch)

    async def beat(i):
        await asyncio.sleep(0)
        return fm.register({"node_id": "kid", "url": "http://kid", "load": i})

    async def read():
        await asyncio.sleep(0)
        return len(fm.list_nodes()) + len(fm.health_table())

    async def main():
        for r in range(ROUNDS):
            res = await asyncio.gather(*(beat(r * 100 + i) for i in range(10)), *(read() for _ in range(10)))
            assert all(x == {"ok": True, "parent": "parent"} for x in res[:10])
            assert fm.nodes["kid"]["load"] == r * 100 + 9  # last writer in schedule order
            fm.flush()  # v0.62: heartbeat refreshes are debounced (FLEET_SAVE_DEBOUNCE_S)
            assert json.loads(fm.path.read_text())["nodes"]["kid"]["load"] == r * 100 + 9
    run(main())


# ================================================================ concurrent memory_sync merges
def test_concurrent_memory_sync_merges_lose_nothing(tmp_path, monkeypatch):
    from avrone import server
    fm = _fleet(tmp_path, monkeypatch)
    monkeypatch.setattr(server, "core", lambda: SimpleNamespace(fleet=fm))

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://t") as c:
            for r in range(ROUNDS // 2):
                reqs = []
                for s in range(4):  # 4 child nodes
                    for j in range(3):  # each pushes 3 times concurrently
                        entries = [{"ns": "node", "key": f"k{r}-{j}", "value": f"{s}-{j}", "ts": 1000 + r * 10 + j},
                                   {"ns": "node", "key": f"shared{r}", "value": f"v{j}", "ts": 2000 + r * 10 + j}]
                        body, h = _signed(MEMORY_PATH, {"from": f"kid{s}", "hops": 1, "capability": "memory:sync",
                                                        "entries": entries})
                        reqs.append(c.post(MEMORY_PATH, content=body, headers=h))
                rs = await asyncio.gather(*reqs)
                assert all(x.status_code == 200 for x in rs), [x.text for x in rs]
                store = memstore.MemoryStore(fm.data_dir / "memory.jsonl")
                for s in range(4):
                    space = store.load(memstore.remote_namespace(f"kid{s}", "node"))
                    for j in range(3):
                        assert space[f"k{r}-{j}"]["value"] == f"{s}-{j}"
                    assert space[f"shared{r}"]["value"] == "v2"  # last-writer-wins by ts, not arrival
    run(main())


# ================================================================ thermal migration vs an agent waking up
def _mig_fleet(tmp_path, monkeypatch):
    from avrone import agentfs
    from avrone.config import Settings
    from avrone.governor import Governor
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok-0123456789abcdef")
    monkeypatch.setenv("AVRONE_NODE_ID", "edge")
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE", "on")
    core = MagicMock()
    core.active = {}
    core.codex.emit = MagicMock()
    core.settings = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data")
    core.governor = Governor(4, thermal_max_c=80, readers={
        "temp_c": lambda: 99.0, "load": lambda: None, "mem_pct": lambda: None,
        "disk_free_pct": lambda: None}, ttl_s=0)
    fm = FleetManager(core, tmp_path / "data")
    for aid in ("alpha", "bravo", "charlie"):
        d = agentfs.agent_dir(core.settings.workspace, aid)
        (d / "notes.txt").write_text(aid)
        os.utime(d / "notes.txt", (1_700_000_000, 1_700_000_000))
    fm.nodes["cool"] = {"node_id": "cool", "url": "http://cool", "last_seen": time.time(), "load": 0}
    return fm


def test_thermal_migrate_never_ships_an_agent_that_woke_up_mid_batch(tmp_path, monkeypatch):
    """thermal_migrate snapshots the idle agents, then awaits one migration after another. An agent
    that starts working while an earlier agent's pack is in flight must not be exported."""
    def one_round(i):
        fm = _mig_fleet(tmp_path / f"r{i}", monkeypatch)
        posted = []

        async def post(base, path, payload, timeout=30):
            posted.append(payload["agent_id"])
            if len(posted) == 1:  # while alpha's pack is on the wire, bravo gets a task
                fm.core.active["Bravo-1"] = {"status": "working", "agent_id": "bravo", "role": "Bravo"}
            await yield_n(3)
            return {"ok": True}
        monkeypatch.setattr(fm, "_post", post)
        out = run(fm.thermal_migrate())
        return posted, out
    for i in range(ROUNDS // 2):
        posted, out = one_round(i)
        assert "bravo" not in posted, f"round {i}: exported busy agent bravo ({posted})"
        assert posted == ["alpha", "charlie"]
        assert [o["agent_id"] for o in out if o["ok"]] == ["alpha", "charlie"]


# ================================================================ overlapping netsquad runs on one core
def test_overlapping_netsquad_runs_keep_their_route_and_restore_after(tmp_path, monkeypatch):
    """Two netsquad placements from different parents overlap on one node and end in crossed order.
    Each run's cycle check must see (at least) its own route and the smaller budget, and the core
    must be back to its idle values afterwards. The old per-run save/restore handed run A run B's
    route (cycle back to A's parent undetected) and left a stale route after both ended."""
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "mid")
    core = MagicMock()
    core.codex.emit = MagicMock()
    core.fleet_hops, core.fleet_privileged = 0, ""
    core.netsquad_path, core.netsquad_budget = None, None
    fm = FleetManager(core, tmp_path)

    def payload(parent, budget):
        return {"mode": "squad", "prompt": parent, "hops": 1, "from": parent, "n": 2, "route": [],
                "capability": "squad:net", "netsquad": {"path": [parent], "role": "W", "budget": budget}}

    async def main():
        seen = []
        for _ in range(ROUNDS // 2):
            started = {k: asyncio.Event() for k in ("pa", "pb")}
            finish = {k: asyncio.Event() for k in ("pa", "pb")}
            views = {}

            async def squad(prompt, n=None, run_id=None, **kw):
                started[prompt].set()
                await finish[prompt].wait()
                views[prompt] = (list(core.netsquad_path), core.netsquad_budget)
                return {"run_id": "r", "answer": "ok"}
            core.squad = squad
            ta = asyncio.create_task(fm.handle_run(payload("pa", 6)))
            await started["pa"].wait()
            tb = asyncio.create_task(fm.handle_run(payload("pb", 3)))
            await started["pb"].wait()
            finish["pa"].set()
            await ta
            finish["pb"].set()
            await tb
            seen.append((views, core.netsquad_path, core.netsquad_budget))
        return seen
    for views, after_path, after_budget in run(main()):
        assert "pa" in views["pa"][0], "run A lost its own route to an overlapping run"
        assert "pb" in views["pb"][0]
        assert views["pa"][1] <= 6 and views["pb"][1] <= 3
        assert after_path is None and after_budget is None, "stale netsquad route after every run ended"
