"""Round 96: await-free tools that read or write files now run off the event loop.

A call-graph sweep (sync helpers doing file I/O, called directly from async tools) found these
running their whole body on the loop: skill_list/skill_load (every skill file), resources (the
ledger), bridge_post/read/reply (the run journal), evolve_history/evolve_cancel, agent_home,
codex_stats (run logs), self_scan/capabilities_report (disk probes), fleet_status, replay_guard,
proc_status (/proc), router_status.
"""
from __future__ import annotations

import asyncio
import importlib
import time
from types import SimpleNamespace as S

import pytest

TOOLS = {
    "agentkit": ["t_skill_list", "t_skill_load", "t_resources"],
    "bridge": ["t_bridge_post", "t_bridge_read", "t_bridge_reply"],
    "evolve": ["t_evolve_history", "t_evolve_cancel"],
    "tools_agenthome": ["t_agent_home"],
    "tools_codexstats": ["t_codex_stats"],
    "tools_extra": ["t_self_scan", "t_capabilities_report"],
    "tools_fleetstatus": ["t_fleet_status"],
    "tools_gatewayview": ["t_replay_guard"],
    "tools_hostview": ["t_proc_status"],
    "tools_routerstatus": ["t_router_status"],
}


@pytest.mark.parametrize("mod,name", [(m, n) for m, ns in TOOLS.items() for n in ns])
def test_tool_is_off_loop(mod, name):
    fn = getattr(importlib.import_module(f"avrone.{mod}"), name)
    assert getattr(fn, "off_loop", False), f"{mod}.{name} runs on the event loop"


def _ticks(coro_fn) -> int:
    async def main():
        ticks, stop = 0, False

        async def ticker():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.02)
        t = asyncio.create_task(ticker())
        await coro_fn()
        stop = True
        await t
        return ticks
    return asyncio.run(main())


def test_codex_stats_does_not_stall_loop(monkeypatch):
    from avrone import tools_codexstats as cs

    def slow(*a, **k):
        time.sleep(0.4)
        raise LookupError("no such run")
    monkeypatch.setattr(cs, "codex_stats", slow)
    out = {}

    async def go():
        out["r"] = await cs.t_codex_stats(S(), {})
    assert _ticks(go) >= 10 and out["r"].startswith("ERROR")


def test_bridge_read_does_not_stall_loop(monkeypatch):
    from avrone import bridge

    class B:
        def read(self, **k):
            time.sleep(0.4)
            return []
    monkeypatch.setattr(bridge, "for_core", lambda core, run_id: B())
    monkeypatch.setattr(bridge, "compact", lambda posts: "none")

    async def go():
        assert await bridge.t_bridge_read(S(core=None, run_id="r"), {}) == "none"
    assert _ticks(go) >= 10
