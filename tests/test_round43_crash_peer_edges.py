"""Round 43: crash, multi-process and hostile-peer edges.

  * capgate receipt chain with several PROCESSES writing at once (two uvicorn workers, the MCP
    server and the HTTP node can share one data dir): the chain must stay one linked line;
  * power cut mid-append (torn last line): the next receipt repairs the tail, keeps the torn bytes
    as evidence, and the chain verifies again;
  * tail truncation (last receipts deleted) is caught through the signed head pointer, and a
    forged / foreign head is refused;
  * memstore.merge against seeded hostile peer payloads: never raises, never stores a key or value
    over the caps, never lets an old entry overwrite a newer one, and stats always add up;
  * gateway: sanitize_messages / extract_choice / retry_after_seconds / safe_temperature on
    seeded junk keep their contracts (valid roles, JSON-encodable UTF-8, no NUL, finite clamps);
  * MCP server: a stream of random lines (junk, huge ints, deep nesting, bad envelopes, batches)
    is answered with strict JSON-RPC and never kills the server.
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import multiprocessing as mp
import os
import random
import string
from pathlib import Path

import pytest

from avrone import capgate
from avrone.gateway import (_UpstreamError, extract_choice, retry_after_seconds, safe_temperature,
                            sanitize_messages)
from avrone.memstore import MemoryRefused, MemoryStore, max_value

ITERS = max(50, int(os.environ.get("FUZZ_ITERS", "400") or 400))
SEEDS = [43, 4343, 20261103]
KEY = "round43-capgate-key-" + "z" * 32


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AVRONE_CAPGATE_KEY", KEY)


def junk(r: random.Random, n: int | None = None) -> str:
    al = string.printable + "\x00\ud800\udfff\u202e\ufeff漢😀{}[]\":"
    return "".join(r.choice(al) for _ in range(r.randint(0, 120) if n is None else n))


def jval(r: random.Random, d: int = 0):
    k = r.choice(["s", "i", "f", "n", "b", "l", "d", "big", "nan"] if d < 3 else ["s", "i", "n"])
    return {"s": lambda: junk(r, r.randint(0, 30)), "i": lambda: r.randint(-10**9, 10**9),
            "f": lambda: r.uniform(-1e12, 1e12), "n": lambda: None, "b": lambda: r.random() < .5,
            "big": lambda: 10 ** r.randint(19, 300),
            "nan": lambda: r.choice([math.nan, math.inf, -math.inf]),
            "l": lambda: [jval(r, d + 1) for _ in range(r.randint(0, 4))],
            "d": lambda: {junk(r, 6): jval(r, d + 1) for _ in range(r.randint(0, 4))}}[k]()


# ------------------------------------------------------------------ capgate: processes + crashes
def _proc_writer(data_dir: str, wid: int, n: int) -> None:
    os.environ["AVRONE_CAPGATE_KEY"] = KEY
    for i in range(n):
        capgate.record(data_dir, caller=f"P{wid}", tool="echo", args={"i": i}, outcome="ok")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_chain_survives_concurrent_processes(tmp_path):
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_proc_writer, args=(str(tmp_path), w, 60)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    v = capgate.verify_chain(tmp_path)
    assert v["ok"], v
    assert v["count"] == 240


def test_torn_tail_is_repaired_and_kept_as_evidence(tmp_path):
    for i in range(5):
        capgate.record(tmp_path, caller="A", tool="echo", args=i, outcome="ok")
    p = capgate.receipts_path(tmp_path)
    with open(p, "ab") as f:  # the process died halfway through writing receipt 6
        f.write(b'{"ts":1,"caller":"A","tool":"ec')
    assert not capgate.verify_chain(tmp_path)["ok"]
    capgate.record(tmp_path, caller="A", tool="echo", args=6, outcome="ok")
    v = capgate.verify_chain(tmp_path)
    assert v["ok"] and v["count"] == 6, v
    assert b'"tool":"ec' in Path(str(p) + ".torn").read_bytes()


def test_torn_only_line_file(tmp_path):
    p = capgate.receipts_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xfe garbage with no newline")
    capgate.record(tmp_path, caller="A", tool="echo", args=0, outcome="ok")
    assert capgate.verify_chain(tmp_path)["ok"]


def test_tail_truncation_detected_by_signed_head(tmp_path):
    for i in range(8):
        capgate.record(tmp_path, caller="A", tool="echo", args=i, outcome="ok")
    p = capgate.receipts_path(tmp_path)
    lines = p.read_text().splitlines()
    p.write_text("\n".join(lines[:5]) + "\n")  # last three receipts deleted: what is left still links
    v = capgate.verify_chain(tmp_path)
    assert not v["ok"] and v["error"].startswith("truncated: head says 8"), v


def test_forged_or_foreign_head_refused(tmp_path, monkeypatch):
    for i in range(3):
        capgate.record(tmp_path, caller="A", tool="echo", args=i, outcome="ok")
    hp = capgate.receipts_path(tmp_path).with_name("receipts.jsonl.head")
    h = json.loads(hp.read_text())
    hp.write_text(json.dumps({**h, "count": 1}))  # attacker lowers the count to hide a cut
    assert "not signed" in capgate.verify_chain(tmp_path)["error"]
    hp.write_text("{nope")
    assert "unreadable" in capgate.verify_chain(tmp_path)["error"]


def test_replaced_chain_with_same_count_detected(tmp_path, monkeypatch):
    for i in range(3):
        capgate.record(tmp_path, caller="A", tool="echo", args=i, outcome="ok")
    head = capgate.receipts_path(tmp_path).with_name("receipts.jsonl.head").read_text()
    # rebuild a different, internally valid 3-line chain (attacker holds the key? no: same key,
    # different content) and restore the old head pointer
    capgate.receipts_path(tmp_path).unlink()
    for i in range(3):
        capgate.record(tmp_path, caller="B", tool="shell", args=i, outcome="ok")
    capgate.receipts_path(tmp_path).with_name("receipts.jsonl.head").write_text(head)
    assert capgate.verify_chain(tmp_path)["error"] == "head mismatch (chain replaced)"


def test_head_recount_after_lost_head(tmp_path):
    for i in range(4):
        capgate.record(tmp_path, caller="A", tool="echo", args=i, outcome="ok")
    capgate.receipts_path(tmp_path).with_name("receipts.jsonl.head").unlink()
    capgate.record(tmp_path, caller="A", tool="echo", args=9, outcome="ok")
    h = json.loads(capgate.receipts_path(tmp_path).with_name("receipts.jsonl.head").read_text())
    assert h["count"] == 5 and capgate.verify_chain(tmp_path)["ok"]


# ------------------------------------------------------------------ memstore: hostile peers
@pytest.mark.parametrize("seed", SEEDS)
def test_memstore_merge_hostile_payloads(tmp_path, seed):
    r = random.Random(seed)
    store = MemoryStore(tmp_path / "m.jsonl")
    store.save("remote:peer:ns", "fixed", "NEWEST")
    newest_ts = store.load("remote:peer:ns")["fixed"].get("ts")
    for i in range(max(30, ITERS // 8)):
        entries = r.choice([
            [jval(r) for _ in range(r.randint(0, 10))],
            [{"key": jval(r), "value": jval(r), "ts": jval(r)} for _ in range(r.randint(0, 10))],
            [{"key": "fixed", "value": "OLD", "ts": r.choice([0, -1, 1.0, "1999", None])}],
            [{"key": junk(r, r.randint(200, 3000)), "value": "x"}],
            [{"key": "big", "value": "v" * (max_value() + r.randint(1, 100))}],
        ])
        if not isinstance(entries, list):
            entries = [entries]
        try:
            st = store.merge("remote:peer:ns", entries)
        except MemoryRefused:
            continue
        except Exception as e:  # pragma: no cover
            pytest.fail(f"seed={seed} i={i} merge raised {e!r}")
        assert st["merged"] + st["skipped"] + st["rejected"] == len(entries), (seed, i, st)
    got = MemoryStore(tmp_path / "m.jsonl").load("remote:peer:ns")
    assert got["fixed"]["value"] == "NEWEST" and got["fixed"].get("ts") == newest_ts
    for k, rec in got.items():
        assert isinstance(k, str) and isinstance(rec.get("value"), str)
        assert len(rec["value"]) <= max_value()


# ------------------------------------------------------------------ gateway contracts
_ROLES = {"system", "user", "assistant", "tool"}


@pytest.mark.parametrize("seed", SEEDS)
def test_sanitize_messages_contract(seed):
    r = random.Random(seed)
    for i in range(ITERS):
        msgs = r.choice([jval(r), [jval(r) for _ in range(r.randint(0, 6))],
                         [{"role": r.choice(["user", "SYSTEM", "bot", "function", "tool", jval(r)]),
                           "content": jval(r), "tool_call_id": jval(r), "name": jval(r)}
                          for _ in range(r.randint(0, 6))]])
        out = sanitize_messages(msgs)
        json.dumps(out, ensure_ascii=False).encode("utf-8")  # must be wire-safe
        for m in out:
            assert m["role"] in _ROLES and isinstance(m["content"], str), (seed, i, m)
            assert "\x00" not in m["content"]
            if m["role"] == "tool":
                assert isinstance(m.get("tool_call_id"), str) and m["tool_call_id"].strip()


@pytest.mark.parametrize("seed", SEEDS)
def test_extract_choice_only_raises_what_the_caller_catches(seed):
    r = random.Random(seed)
    for i in range(ITERS):
        body = r.choice([
            junk(r), json.dumps(jval(r), default=str),
            json.dumps({"choices": [jval(r)]}, default=str),
            json.dumps({"choices": [{"message": {"content": jval(r), "tool_calls": jval(r)}}]}, default=str),
            "data: " + json.dumps({"choices": [{"delta": {"content": junk(r, 10)}}]}) + "\n\ndata: [DONE]",
            json.dumps({"error": jval(r)}, default=str), "\ufeff" + json.dumps({"message": {"content": "x"}}),
            "[" * 3000,
        ])
        try:
            choice, msg = extract_choice(body)
        except (ValueError, _UpstreamError, RecursionError,
                KeyError, IndexError, TypeError, AttributeError):  # exactly what Gateway.chat catches
            continue
        assert isinstance(choice, dict) and isinstance(msg, dict), (seed, i)


@pytest.mark.parametrize("seed", SEEDS)
def test_retry_after_and_temperature_are_finite(seed):
    r = random.Random(seed)
    for _ in range(ITERS):
        v = r.choice([jval(r), junk(r, 20), "Wed, 21 Oct 2015 07:28:00 GMT", "-5", "1e400", "nan", "inf"])
        ra = retry_after_seconds(v, now=1_700_000_000.0)
        # +inf is allowed (an explicit "wait forever"; _delay clamps every hint to 30 s), NaN/negative never
        assert ra is None or (ra == ra and ra >= 0), v
        t = safe_temperature(jval(r))
        assert 0.0 <= t <= 2.0 and math.isfinite(t)


# ------------------------------------------------------------------ MCP server line fuzz
def test_mcp_server_survives_random_line_stream():
    from avrone.mcp_server import AvroneMCPServer

    class _Core:
        async def chat(self, task, **kw):
            return {"answer": "ok", "run_id": "r"}

    async def factory():
        return _Core()

    r = random.Random(43)
    lines: list[bytes] = []
    for i in range(300):
        lines.append(r.choice([
            junk(r).encode("utf-8", "surrogatepass"),
            json.dumps({"jsonrpc": "2.0", "id": jval(r), "method": jval(r), "params": jval(r)}, default=str).encode(),
            b'{"jsonrpc":"2.0","id":' + ("9" * r.randint(20, 6000)).encode() + b',"method":"ping"}',
            b"[" * 20000 + b"]" * 20000,
            json.dumps([{"jsonrpc": "2.0", "id": k, "method": "ping"} for k in range(r.randint(0, 300))]).encode(),
            b'{"jsonrpc":"1.0","id":1,"method":"ping"}', b"", b"   ", b"null", b"[]",
        ]))
    lines.append(b'{"jsonrpc":"2.0","id":"final","method":"ping"}')
    data = b"\n".join(lines) + b"\n"

    async def go():
        reader = asyncio.StreamReader(limit=1 << 26)
        reader.feed_data(data)
        reader.feed_eof()
        out = io.BytesIO()

        class W:
            def write(self, b):
                out.write(b if isinstance(b, bytes) else b.encode())

            def flush(self):
                pass

        srv = AvroneMCPServer(core_factory=factory)
        await asyncio.wait_for(srv.serve(reader, W()), 60)
        return out.getvalue()

    raw = asyncio.run(go())
    replies = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]  # strict JSON, every line
    assert replies, "server answered nothing"
    for rep in replies:
        items = rep if isinstance(rep, list) else [rep]
        for it in items:
            assert it.get("jsonrpc") == "2.0"
            assert ("result" in it) != ("error" in it)
    flat = [x for rep in replies for x in (rep if isinstance(rep, list) else [rep])]
    assert any(x.get("id") == "final" and "result" in x for x in flat), "server died before the last line"
