"""Round 50: hostile model providers, codex listing/replay, CLI arguments.

  * gateway: a 200 body nested 200k deep (plain or tool-call arguments) escaped chat() as a raw
    RecursionError instead of LLMError; temperature=10**400 raised OverflowError before the request;
    a 300 MB gzip stream was inflated whole and a 200 MB content string became the agent's reply;
  * gateway fuzz: 6,000 seeded hostile bodies through the full chat() path end in a reply or LLMError;
  * codex: a FIFO named <run>.jsonl hung runs() and replay(); runs() returned 4 MB prompts and
    non-string prompts; junk limits raised;
  * cli: --max-steps -5 gave agents zero turns; `evolve --json PATH` never wrote PATH;
    --first-answer-file on a FIFO hung and a huge file was read whole; accepted proposals with odd
    `files` crashed the listing.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from avrone import cli, evolve, gateway as g
from avrone.codex import Codex


def run(c):
    return asyncio.run(c)


def _settings():
    return SimpleNamespace(provider="custom", llm_base_url="http://up.test/v1", llm_model="m", llm_api_key="",
                           llm_mode="live", llm_retries=1, llm_backoff=0.0, llm_timeout=5)


def _chat(monkeypatch, handler, **kw):
    monkeypatch.setattr(g, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    return run(g.Gateway(_settings()).chat([{"role": "user", "content": "hi"}], **kw))


# ------------------------------------------------------------------ gateway
DEEP = [
    lambda: httpx.Response(200, text="[" * 200_000),
    lambda: httpx.Response(200, text='{"choices": ' + "[" * 200_000),
    lambda: httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
        {"function": {"name": "shell", "arguments": "[" * 100_000}}]}}]}),
    lambda: httpx.Response(200, text="data: " + "[" * 200_000 + "\ndata: [DONE]\n"),
]


@pytest.mark.parametrize("i", range(len(DEEP)))
def test_deeply_nested_provider_bodies_are_llm_errors(monkeypatch, i):
    if i == 2:
        # round 111: deep tool-call arguments are unreadable args like any other junk ({"_raw": ...},
        # incomplete), not a RecursionError mapped to LLMError; the agent sees a bad call it can fix
        out = _chat(monkeypatch, lambda req: DEEP[i]())
        assert '"_raw"' in out
        return
    with pytest.raises(g.LLMError):
        _chat(monkeypatch, lambda req: DEEP[i]())


@pytest.mark.parametrize("t", [10**400, -10**400, "hot", None, True, float("nan"), [1]])
def test_junk_temperature_is_defaulted(monkeypatch, t):
    seen = []

    def h(req):
        seen.append(json.loads(req.content)["temperature"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    assert _chat(monkeypatch, h, temperature=t) == "ok"
    assert seen == [0.7]


def test_huge_bodies_are_cut_off(monkeypatch):
    monkeypatch.setattr(g, "MAX_RESPONSE_BYTES", 1_000_000)
    big = b'{"choices":[{"message":{"content":"' + b"a" * 3_000_000 + b'"}}]}'
    with pytest.raises(g.LLMError, match="larger than"):
        _chat(monkeypatch, lambda req: httpx.Response(200, content=big))
    bomb = gzip.compress(b" " * 20_000_000)
    with pytest.raises(g.LLMError, match="larger than"):
        _chat(monkeypatch, lambda req: httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"}))
    ok = gzip.compress(json.dumps({"choices": [{"message": {"content": "zipped"}}]}).encode())
    assert _chat(monkeypatch, lambda req: httpx.Response(200, content=ok,
                                                         headers={"content-encoding": "gzip"})) == "zipped"


def test_retry_after_still_read_after_bounded_read(monkeypatch):
    calls = []

    def h(req):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, text="slow down", headers={"retry-after": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "second"}}]})
    s = _settings()
    s.llm_retries = 2
    monkeypatch.setattr(g, "HTTP_TRANSPORT", httpx.MockTransport(h))
    assert run(g.Gateway(s).chat([{"role": "user", "content": "hi"}])) == "second" and len(calls) == 2


def _junk(r: random.Random, d: int = 0):
    atoms = [None, True, 0, -1, 1.5, "", "x", "\ud800", "\x00", [], {}, 10**30]
    k = r.random()
    if d > 4 or k < 0.4:
        return r.choice(atoms)
    if k < 0.7:
        return [_junk(r, d + 1) for _ in range(r.randint(0, 3))]
    keys = ["choices", "message", "content", "tool_calls", "function", "name", "arguments", "delta", "text",
            "error", "finish_reason", "refusal", "index", "done_reason"]
    return {r.choice(keys): _junk(r, d + 1) for _ in range(r.randint(0, 4))}


@pytest.mark.parametrize("seed", range(3))
def test_seeded_hostile_bodies_end_in_reply_or_llm_error(monkeypatch, seed):
    r = random.Random(5000 + seed)
    iters = int(os.environ.get("FUZZ_ITERS", "2000"))
    bodies = []
    for _ in range(iters):
        o = _junk(r)
        roll = r.random()
        if roll < 0.4:
            o = {"choices": [{"message": _junk(r, 1), "finish_reason": _junk(r, 2)}]}
        elif roll < 0.6:
            o = {"choices": [{"message": {"content": _junk(r, 2), "tool_calls": _junk(r, 2)}}]}
        text = json.dumps(o, default=str)
        if r.random() < 0.15:
            text = "\n".join("data: " + json.dumps(_junk(r), default=str) for _ in range(r.randint(1, 4)))
        if r.random() < 0.05:
            text = r.choice(["[" * 5000, "{", "\xff\xfe", "", "null", '"str"'])
        bodies.append(text)
    it = iter(bodies)
    monkeypatch.setattr(g, "HTTP_TRANSPORT",
                        httpx.MockTransport(lambda req: httpx.Response(200, text=next(it))))
    gw = g.Gateway(_settings())
    gw.threshold = 10**9  # never open the breaker during the fuzz

    async def go():
        for _ in range(iters):
            try:
                out = await gw.chat([{"role": "user", "content": "hi"}])
                assert isinstance(out, str)
            except g.LLMError:
                pass
    run(go())


# ------------------------------------------------------------------ codex
def _mkfifo_or_skip(p: Path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs")
    os.mkfifo(p)


def _with_deadline(fn, secs=5.0):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("v", fn()), daemon=True)
    t.start()
    t.join(secs)
    assert not t.is_alive(), "hung"
    return out["v"]


def test_fifo_run_log_does_not_hang_codex(tmp_path):
    c = Codex(tmp_path)
    c.emit("good", "run.start", "T", prompt="p")
    _mkfifo_or_skip(c.log_dir / "trap.jsonl")
    runs = _with_deadline(lambda: c.runs())
    assert [r["run_id"] for r in runs] == ["good"]
    assert _with_deadline(lambda: c.replay("trap")) == []


def test_runs_prompts_are_clipped_text(tmp_path):
    c = Codex(tmp_path)
    c.emit("big", "run.start", "T", prompt="p" * 3_000_000)
    c.emit("obj", "run.start", "T", prompt={"nested": [1, 2]})
    c.emit("none", "run.start", "T", prompt=None)
    rows = {r["run_id"]: r["prompt"] for r in c.runs()}
    assert all(isinstance(v, str) for v in rows.values())
    assert len(rows["big"]) <= 2000 and rows["none"] == ""


@pytest.mark.parametrize("lim", ["abc", float("nan"), float("inf"), None, True, -5, 10**400])
def test_codex_junk_limits(tmp_path, lim):
    c = Codex(tmp_path)
    for i in range(3):
        c.emit(f"r{i}", "run.start", "T", prompt=str(i))
    assert isinstance(c.runs(lim), list)
    assert isinstance(c.replay("r0", lim), list)


# ------------------------------------------------------------------ cli
def test_cli_max_steps_clamped(monkeypatch):
    seen = {}

    async def fake_build(settings, extras=True):
        seen["steps"] = settings.max_steps
        raise RuntimeError("stop here")
    monkeypatch.setattr(cli, "build_core", fake_build)
    for v, want in [("-5", 1), ("0", 1), ("1000000000", cli.MAX_CLI_STEPS), ("7", 7)]:
        assert cli.main(["chat", "hi", "--max-steps", v]) == 1
        assert seen["steps"] == want


def test_cli_answer_file_guards(tmp_path):
    f = tmp_path / "big.txt"
    f.write_bytes(b"a" * (cli.MAX_ANSWER_FILE_BYTES + 10))
    with pytest.raises(ValueError):
        cli._read_answer_file(str(f))
    with pytest.raises(ValueError):
        cli._read_answer_file(str(tmp_path))
    fifo = tmp_path / "fifo"
    _mkfifo_or_skip(fifo)
    assert isinstance(_with_deadline(lambda: _raise_to_value(lambda: cli._read_answer_file(str(fifo)))), ValueError)
    ok = tmp_path / "ok.txt"
    ok.write_bytes(b"answer \xff")
    assert cli._read_answer_file(str(ok)) == "answer \ufffd"


def _raise_to_value(fn):
    try:
        fn()
    except ValueError as e:
        return e
    return None


def test_cli_evolve_json_writes_file_and_odd_files_listed(tmp_path, monkeypatch, capsys):
    d = tmp_path / "evolve"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"pid": "p1", "passed": True, "goal": None, "branch": "b",
                                          "files": "notalist"}))
    (d / "b.json").write_text(json.dumps({"pid": "p2", "passed": True, "goal": 5, "branch": "b", "files": [1, None]}))
    (d / "c.json").write_text(json.dumps({"pid": "p3", "passed": "yes", "goal": "g", "files": []}))
    rows = evolve.list_accepted(SimpleNamespace(data_dir=tmp_path))
    assert [r["pid"] for r in rows] == ["p1", "p2"] and rows[1]["files"] == ["1", "None"]

    class Core:
        settings = SimpleNamespace(data_dir=tmp_path)

    async def fake_build(settings, extras=True):
        return Core(), []
    monkeypatch.setattr(cli, "build_core", fake_build)
    out = tmp_path / "out.json"
    assert cli.main(["evolve", "--json", str(out)]) == 0
    assert [r["pid"] for r in json.loads(out.read_text())] == ["p1", "p2"]
    assert "p1 b" in capsys.readouterr().out
