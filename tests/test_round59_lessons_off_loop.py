"""Round 59: the learning bridge parsed the whole memory store on the event loop at every run.

A mock squad with the slow-callback detector on (loop.set_debug, slow_callback_duration 0.1 s)
and a 200k-line memory store logged one ~1.0 s step per squad and per deep wave. Profiling put
all of it in Trinity._lessons_block -> bridge.lessons_block -> memstore.load (the whole store is
read and folded to find the "lessons" namespace). Lessons are on by default, so every squad and deep
run froze every other run, the API, WebSockets and fleet heartbeats for that long. _distill (the
end-of-run lesson save, which rewrites the store) ran on the loop too. Both now go through
asyncio.to_thread. After the fix the same workload logs no slow callbacks at 0.1 s.

Also seen, not a bug: all-tools fuzz seeds 20-23 clean apart from asyncio's own "child process
... exit status already read" warning (a pid reaped twice by the child watcher after a timed-out
shell); governor parsers hold up against junk cgroup/meminfo/vm_stat/pmset text.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest


def _loop_probe():
    try:
        asyncio.get_running_loop()
        return "on-loop"
    except RuntimeError:
        return "thread"


def _core(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LLM_MODE", "mock")
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", "0")
    monkeypatch.setenv("AVRONE_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("AVRONE_DATA_DIR", str(tmp_path / "d"))
    (tmp_path / "ws").mkdir()
    (tmp_path / "d").mkdir()
    from avrone import server
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "d")
    monkeypatch.setattr(server.settings, "llm_mode", "mock")
    return tmp_path / "d"


async def _with_core(fn):
    from avrone import server
    async with server.lifespan(server.app):
        return await fn(server.core())


@pytest.mark.parametrize("mode", ["squad", "deep"])
def test_lessons_and_distill_run_in_a_thread(tmp_path, monkeypatch, mode):
    from avrone import bridge
    _core(tmp_path, monkeypatch)
    seen = {"inject": [], "distill": []}
    monkeypatch.setattr(bridge, "lessons_block", lambda *a, **k: (seen["inject"].append(_loop_probe()), "")[1])
    monkeypatch.setattr(bridge, "distill_lessons", lambda *a, **k: (seen["distill"].append(_loop_probe()), [])[1])

    async def go(c):
        if mode == "squad":
            return await c.squad("summarize caching", 3, "r59s")
        return await c.deep("plan a cache", None, 2, "r59d")
    out = asyncio.run(_with_core(go))
    assert out.get("answer") or out.get("report") or out
    assert seen["inject"] and set(seen["inject"]) == {"thread"}, seen
    assert seen["distill"] and set(seen["distill"]) == {"thread"}, seen


def test_squad_keeps_the_loop_responsive_with_a_big_memory_store(tmp_path, monkeypatch):
    d = _core(tmp_path, monkeypatch)
    with open(d / "memory.jsonl", "w") as f:
        for i in range(120_000):
            f.write(json.dumps({"ns": "lessons" if i % 2 else f"agent{i % 30}", "key": f"k{i % 4000}",
                                "value": "caching " + "v" * 150, "ts": i}) + "\n")

    async def go(c):
        gaps, last, stop = [], time.monotonic(), False

        async def tick():
            nonlocal last
            while not stop:
                await asyncio.sleep(0.01)
                n = time.monotonic(); gaps.append(n - last); last = n
        t = asyncio.create_task(tick())
        await asyncio.sleep(0.03)
        out = await c.squad("caching lessons", 3, "r59big")
        stop = True
        await asyncio.sleep(0.02)
        t.cancel()
        return out, max(gaps)
    out, gap = asyncio.run(_with_core(go))
    assert "answer" in out
    assert gap < 0.3, gap


def test_lessons_still_injected_from_the_thread(tmp_path, monkeypatch):
    """The block computed in the worker thread still reaches the workers' focus."""
    from avrone import bridge
    _core(tmp_path, monkeypatch)
    monkeypatch.setattr(bridge, "lessons_block", lambda *a, **k: "\n\nLessons:\n- MARKER59 cache by key\n")
    monkeypatch.setattr(bridge, "distill_lessons", lambda *a, **k: [])
    events = []

    async def go(c):
        orig = c.codex.emit

        def spy(run_id, kind, agent, **data):
            events.append(kind)
            return orig(run_id, kind, agent, **data)
        c.codex.emit = spy
        return await c.squad("x", 2, "r59inj")
    asyncio.run(_with_core(go))
    assert "lessons.inject" in events
