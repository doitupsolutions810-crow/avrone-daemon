"""Round 55: quadratic diff, osint rollup on the loop, per-subscriber event serialization.

  * diff_files: difflib is quadratic on many similar short lines (20k lines: 5.4 s, 40k: 21 s; the
    1 MB input cap allows ~200k lines, i.e. minutes) and it ran on the event loop. Inputs over
    DIFF_INLINE_CHARS now diff in a child process with a deadline (DIFF_TIMEOUT_S);
  * osint_metrics scanned every run log on the loop (300k events: 0.72 s stall) -> off_loop;
  * each WebSocket pump re-ran asdict()+dumps for its own copy of every event: a 4 MB event cost
    ~1.3 s of loop time across 32 HUD subscribers. emit() now serializes once (codex.wire_text).

Also probed live (uvicorn): 10 MB binary / 15 MB text / 100k-'{' frames, a 20k-frame flood, 80
connections against the 64-subscriber cap, never-reading clients during a run: no tracebacks,
/api/status stayed at ~1 ms, slots recovered.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import AgentContext, TrinityCore


class _LLM:
    async def chat(self, messages, **kw):
        return '{"final": "ok"}'


def _core(tmp_path):
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"
    s.workspace.mkdir(parents=True, exist_ok=True)
    core = TrinityCore(s, _LLM(), LocalRunner(s), Codex(s.data_dir))
    from avrone.tools_extra import register_extra_tools
    register_extra_tools(core.tools)
    return core


def _ctx(core):
    return AgentContext(core=core, run_id="r55", name="Fuzzer", depth=0)


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


def _similar(n, k):
    return "".join(("x\n" if i % k else f"{i}\n") for i in range(n))


# ------------------------------------------------------------------ diff_files
def test_quadratic_diff_does_not_block_the_loop(tmp_path):
    core = _core(tmp_path)
    ws = Path(core.settings.workspace)
    (ws / "c.txt").write_text(_similar(14000, 2))
    (ws / "e.txt").write_text(_similar(14000, 3))  # ~2.5 s of difflib in-process
    out, gap = asyncio.run(_max_gap(core.tools.call(_ctx(core), "diff_files", {"path_a": "c.txt", "path_b": "e.txt"})))
    assert out.startswith("--- c.txt") and "added" in out
    assert gap < 0.4, f"event loop blocked for {gap:.2f}s"


def test_quadratic_diff_has_a_deadline(tmp_path, monkeypatch):
    from avrone import tools_extra
    monkeypatch.setattr(tools_extra, "DIFF_TIMEOUT_S", 1.0)
    core = _core(tmp_path)
    ws = Path(core.settings.workspace)
    (ws / "c.txt").write_text(_similar(40000, 2))
    (ws / "e.txt").write_text(_similar(40000, 3))  # ~21 s uninterrupted before
    t0 = time.monotonic()
    out = asyncio.run(core.tools.call(_ctx(core), "diff_files", {"path_a": "c.txt", "path_b": "e.txt"}))
    assert time.monotonic() - t0 < 6
    assert out.startswith("ERROR") and "diff took over" in out


def test_child_diff_matches_inline_diff(tmp_path, monkeypatch):
    from avrone import tools_extra
    core = _core(tmp_path)
    ws = Path(core.settings.workspace)
    (ws / "a.txt").write_text("one\ntwo\nthree\ncaf\u00e9\nno newline")
    (ws / "b.txt").write_text("one\n2\nthree\ncaf\u00e9 \u2713\n")
    args = {"path_a": "a.txt", "path_b": "b.txt", "context": 1}
    inline = asyncio.run(core.tools.call(_ctx(core), "diff_files", args))
    monkeypatch.setattr(tools_extra, "DIFF_INLINE_CHARS", 0)
    child = asyncio.run(core.tools.call(_ctx(core), "diff_files", args))
    assert child == inline and "No newline at end of file" in child
    same = asyncio.run(core.tools.call(_ctx(core), "diff_files", {"path_a": "a.txt", "path_b": "a.txt"}))
    assert same.startswith("No differences")


def test_text_b_diff_still_capped(tmp_path):
    from avrone.tools_extra import DIFF_MAX_TEXT_CHARS
    core = _core(tmp_path)
    (Path(core.settings.workspace) / "a.txt").write_text("a\n")
    out = asyncio.run(core.tools.call(_ctx(core), "diff_files", {"path_a": "a.txt", "text_b": "b" * (DIFF_MAX_TEXT_CHARS + 1)}))
    assert out.startswith("ERROR")


# ------------------------------------------------------------------ osint_metrics
def test_osint_metrics_runs_off_the_loop(tmp_path):
    core = _core(tmp_path)
    assert getattr(core.tools.get("osint_metrics").fn, "off_loop", False)


# ------------------------------------------------------------------ event fan-out
def test_emit_serializes_once_for_all_subscribers(tmp_path, monkeypatch):
    from avrone import codex as cx
    c = Codex(tmp_path / "d")
    calls = []
    real = cx.dumps_event
    monkeypatch.setattr(cx, "dumps_event", lambda ev: (calls.append(1), real(ev))[1])

    async def go():
        qs = [c.subscribe() for _ in range(20)]
        c.emit("r1", "tool.result", "A", out="x" * 100_000)
        return [cx.wire_text(q.get_nowait()) for q in qs]
    texts = asyncio.run(go())
    assert len(set(texts)) == 1 and '"out": "xxx' in texts[0].replace('":"', '": "')
    assert len(calls) == 1, f"serialized {len(calls)} times for 20 subscribers"


def test_wire_text_matches_the_log_line(tmp_path):
    from avrone.codex import wire_text
    c = Codex(tmp_path / "d")

    async def go():
        q = c.subscribe()
        c.emit("r2", "tool.result", "A", n=float("nan"), s="a\ud800b", b=b"\xff", t=(1, 2))
        return wire_text(q.get_nowait())
    text = asyncio.run(go())
    log_line = (tmp_path / "d" / "runs").glob("*.jsonl").__next__().read_text(errors="replace").strip()
    assert text.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace") == log_line


def test_gap_notice_without_cached_wire_still_serializes(tmp_path):
    from avrone.codex import Event, wire_text
    import json
    ev = Event(run_id="r", kind="stream.gap", agent="CodeX", data={"missed": 3})
    assert json.loads(wire_text(ev))["data"] == {"missed": 3}


def test_ws_stream_delivers_cached_wire(monkeypatch):
    from fastapi.testclient import TestClient
    from avrone import server
    with TestClient(server.app) as c:
        tok = server.core().settings.admin_token or ""
        with c.websocket_connect(f"/ws/events?token={tok}") as ws:
            server.core().codex.emit("r55ws", "tool.result", "A", out="hello")
            msg = ws.receive_json()
            assert msg["run_id"] == "r55ws" and msg["data"]["out"] == "hello"
