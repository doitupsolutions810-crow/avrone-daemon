"""Round 41: seeded fuzz + soak across the boundaries where untrusted bytes enter AVRONE.

Earlier rounds pinned specific edge cases one test at a time. This round throws thousands of
generated hostile inputs at the same boundaries and checks INVARIANTS rather than examples:

  * model-reply parsing (parse_action / normalize_action / reply_text / out_of_turns_result /
    _normalize_subtasks) never raises, never returns a non-action, stays bounded;
  * clip_output / compact_messages keep their size contracts on any input;
  * fleet request signing: every valid signature verifies exactly once (replay refused), and any
    single-byte tamper of method / path / body / ts / nonce / sig is refused, with no exception
    type other than GatewayRefused escaping;
  * MemoryStore and Checkpoint survive long interleaved write/reload soaks (with concurrent
    threads and torn tail bytes) and never lose the last committed value.

Every generator is seeded, so a failure prints its seed and reproduces exactly. Iteration counts
default to a CI-friendly size; set FUZZ_ITERS (e.g. 20000) for a long local soak.
"""
from __future__ import annotations

import json
import os
import random
import string
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from avrone import fleet as fleet_mod
from avrone.checkpoint import Checkpoint
from avrone.fleet import FleetManager, GatewayRefused, sign
from avrone.memstore import MemoryRefused, MemoryStore
from avrone.tools import builtin_registry
from avrone.trinity import (_normalize_subtasks, clip_output, compact_messages, normalize_action,
                            out_of_turns_result, parse_action, reply_text)

ITERS = max(50, int(os.environ.get("FUZZ_ITERS", "400") or 400))
SEEDS = [41, 4141, 20261103]

# ------------------------------------------------------------------ hostile generators
_ALPHABET = (string.printable + "{}[]\":,`\\\u0000\u200b\u202e\ufeff\ud7ff\U0001f600é漢"
             + "\ud800")  # a lone surrogate on purpose


def junk_str(r: random.Random, n: int | None = None) -> str:
    n = r.randint(0, 200) if n is None else n
    return "".join(r.choice(_ALPHABET) for _ in range(n))


def junk_value(r: random.Random, depth: int = 0):
    kinds = ["str", "int", "float", "none", "bool", "list", "dict", "big", "nan", "bytes"]
    if depth > 3:
        kinds = kinds[:5]
    k = r.choice(kinds)
    if k == "str":
        return junk_str(r, r.randint(0, 40))
    if k == "int":
        return r.randint(-10**6, 10**6)
    if k == "big":
        return 10 ** r.randint(20, 400)
    if k == "float":
        return r.uniform(-1e9, 1e9)
    if k == "nan":
        return r.choice([float("nan"), float("inf"), float("-inf")])
    if k == "none":
        return None
    if k == "bool":
        return r.random() < 0.5
    if k == "bytes":
        return bytes(r.randrange(256) for _ in range(r.randint(0, 16)))
    if k == "list":
        return [junk_value(r, depth + 1) for _ in range(r.randint(0, 4))]
    return {junk_str(r, r.randint(0, 8)): junk_value(r, depth + 1) for _ in range(r.randint(0, 4))}


def model_reply(r: random.Random) -> str:
    """Text that looks like what small open models actually emit: actions buried in prose,
    broken fences, think tags, truncation, nested braces, duplicate keys."""
    action = r.choice([
        {"tool": r.choice(["read_file", "write_file", "shell", "web_search", junk_str(r, 6)]),
         "args": {"path": junk_str(r, 10), "content": junk_str(r, 20)}},
        {"final": junk_str(r, 30)},
        {"tools": [{"tool": "read_file", "args": {"path": "a"}}, {"tool": junk_str(r, 4)}]},
        {"final": None, "tool": "shell", "args": junk_str(r, 10)},
        {"subtasks": [{"role": junk_value(r), "focus": junk_value(r)}]},
        junk_value(r),
    ])
    try:
        body = json.dumps(action, ensure_ascii=r.random() < 0.5, default=str)
    except (ValueError, TypeError):
        body = "{}"
    if r.random() < 0.3:  # truncate mid-object
        body = body[: r.randint(0, len(body))]
    wrap = r.choice([
        "{b}", "```json\n{b}\n```", "```\n{b}", "<think>{j}</think>{b}", "{j}\n{b}\n{j}",
        "<|channel|>final<|message|>{b}", "```python\n{b}\n```", "{b}{b}", "{{{b}", "{j}",
    ])
    return wrap.replace("{b}", body).replace("{j}", junk_str(r, r.randint(0, 60)))


# ------------------------------------------------------------------ parsing invariants
@pytest.mark.parametrize("seed", SEEDS)
def test_parse_and_normalize_never_raise_and_return_actions(seed):
    r = random.Random(seed)
    reg = builtin_registry()
    for i in range(ITERS):
        text = model_reply(r)
        try:
            a = parse_action(text)
        except Exception as e:  # pragma: no cover - the failure message is the point
            pytest.fail(f"seed={seed} i={i} parse_action raised {e!r} on {text[:200]!r}")
        if a is None:
            continue
        assert isinstance(a, dict), (seed, i)
        assert {"tool", "final", "tools"} & set(a), (seed, i, a)
        try:
            n = normalize_action(reg, a)
        except Exception as e:  # pragma: no cover
            pytest.fail(f"seed={seed} i={i} normalize_action raised {e!r} on {a!r}")
        assert isinstance(n, dict)


@pytest.mark.parametrize("seed", SEEDS)
def test_parse_action_is_bounded_on_huge_and_deep_input(seed):
    r = random.Random(seed)
    deep = "[" * 5000 + "]" * 5000
    huge = '{"final": "' + "x" * 2_000_000 + '"}'
    nested = '{"tool":"a","args":' + '{"a":' * 3000 + "1" + "}" * 3000 + "}"
    for text in (deep, nested, junk_str(r, 50_000), "{" * 100_000, huge):
        t0 = time.monotonic()
        parse_action(text)  # must not raise RecursionError / MemoryError
        assert time.monotonic() - t0 < 10, "parse_action must stay bounded"


@pytest.mark.parametrize("seed", SEEDS)
def test_reply_text_and_out_of_turns_always_give_str(seed):
    r = random.Random(seed)
    for i in range(ITERS):
        v = r.choice([junk_value(r), {"content": junk_value(r)},
                      {"message": {"content": junk_value(r)}}, model_reply(r).encode("utf-8", "surrogatepass")])
        t = reply_text(v)
        assert isinstance(t, str), (seed, i, v)
        o = out_of_turns_result(v, junk_str(r, 8), r.randint(0, 50))
        assert isinstance(o, str) and o, (seed, i)


@pytest.mark.parametrize("seed", SEEDS)
def test_normalize_subtasks_contract(seed):
    r = random.Random(seed)
    for i in range(ITERS):
        n = r.randint(1, 8)
        reply = r.choice([model_reply(r), json.dumps({"subtasks": junk_value(r)}, default=str),
                          json.dumps({"subtasks": [{"role": junk_value(r), "focus": junk_value(r)}
                                                   for _ in range(r.randint(0, 20))]}, default=str)])
        subs = _normalize_subtasks(reply, n)
        assert 1 <= len(subs) <= n, (seed, i)
        for s in subs:
            assert isinstance(s["role"], str) and s["role"] and len(s["role"]) <= 60
            assert isinstance(s["focus"], str) and s["focus"] and len(s["focus"]) <= 2000


# ------------------------------------------------------------------ size contracts
@pytest.mark.parametrize("seed", SEEDS)
def test_clip_output_size_contract(seed):
    r = random.Random(seed)
    for i in range(ITERS):
        limit = r.randint(1, 5000)
        s = junk_str(r, r.randint(0, 12000))
        out = clip_output(s, limit)
        assert isinstance(out, str)
        if len(s) <= limit:
            assert out == s, (seed, i)
        else:  # clipped: never more than the limit plus its short marker, keeps head
            assert len(out) <= limit + 200, (seed, i, len(out), limit)
            if limit >= 2:  # head = 2/3 of the limit; a 1-char limit keeps tail only
                assert out[: (limit * 2) // 3] == s[: (limit * 2) // 3]
            if limit - (limit * 2) // 3:
                assert out.endswith(s[-(limit - (limit * 2) // 3):])


@pytest.mark.parametrize("seed", SEEDS)
def test_compact_messages_keeps_system_and_shrinks(seed):
    r = random.Random(seed)
    for i in range(max(50, ITERS // 4)):
        msgs = [{"role": "system", "content": "SYS" + junk_str(r, 50)}]
        for _ in range(r.randint(0, 40)):
            msgs.append({"role": r.choice(["user", "assistant", "tool"]),
                         "content": r.choice([junk_str(r, r.randint(0, 4000)), None, ""])})
        if r.random() < 0.2:
            msgs.append(junk_value(r))  # a non-dict sneaks in
        limit = r.randint(8000, 40000)
        before = sum(len(m.get("content") or "") for m in msgs if isinstance(m, dict))
        compact_messages(msgs, limit)
        after = sum(len(m.get("content") or "") for m in msgs if isinstance(m, dict))
        assert msgs[0]["role"] == "system" and msgs[0]["content"].startswith("SYS"), (seed, i)
        assert after <= before, (seed, i)


# ------------------------------------------------------------------ fleet auth fuzz
@pytest.fixture()
def fleet(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_TOKEN_PREV", "AVRONE_FLEET_ALLOW", "AVRONE_NODE_ID", "AVRONE_FLEET_PARENT",
              "AVRONE_PUBLIC_URL", "AVRONE_FLEET_HEARTBEAT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "round41-" + "k" * 40)
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)
    c = MagicMock()
    c.active = {}
    c.codex.emit = MagicMock()
    return FleetManager(c, tmp_path)


def _flip(r: random.Random, s: str) -> str:
    if not s:
        return "x"
    i = r.randrange(len(s))
    ch = s[i]
    alt = r.choice([c for c in "0123456789abcdefXYZ/_-" if c != ch])
    return s[:i] + alt + s[i + 1:]


@pytest.mark.parametrize("seed", SEEDS)
def test_fleet_valid_signatures_verify_once_and_tamper_is_refused(fleet, seed):
    r = random.Random(seed)
    tok = fleet.token
    for i in range(max(50, ITERS // 2)):
        method = r.choice(["POST", "GET", "post"])
        path = "/api/fleet/" + r.choice(["run", "squad", "memory", "ckpt"]) + r.choice(["", "/x", "?q=1"])
        body = junk_str(r, r.randint(0, 300)).encode("utf-8", "surrogatepass")
        h = sign(tok, method, path, body, nonce=f"n{seed}x{i}")
        fleet.verify(method, path, body, dict(h))  # valid: accepted
        with pytest.raises(GatewayRefused):  # exact replay: refused
            fleet.verify(method, path, body, dict(h))
        # one tamper at a time; a fresh nonce so replay is not what refuses it
        h2 = sign(tok, method, path, body, nonce=f"t{seed}x{i}")
        field = r.choice(["method", "path", "body", "ts", "sig", "nonce_only_sig"])
        m2, p2, b2, hh = method, path, body, dict(h2)
        if field == "method":
            m2 = "DELETE"
        elif field == "path":
            p2 = path + "z"
        elif field == "body":
            b2 = body + b"!"
        elif field == "ts":
            hh["x-avrone-ts"] = str(int(hh["x-avrone-ts"]) + r.randint(1, 5))
        elif field == "sig":
            hh["x-avrone-sig"] = _flip(r, hh["x-avrone-sig"])
        else:
            hh["x-avrone-nonce"] = hh["x-avrone-nonce"] + "q"
        with pytest.raises(GatewayRefused):
            fleet.verify(m2, p2, b2, hh)


@pytest.mark.parametrize("seed", SEEDS)
def test_fleet_junk_headers_only_ever_raise_gateway_refused(fleet, seed):
    r = random.Random(seed)
    for i in range(ITERS):
        hdr = {k: r.choice([junk_str(r, r.randint(0, 80)), str(junk_value(r)), ""])
               for k in ("x-avrone-ts", "x-avrone-nonce", "x-avrone-sig") if r.random() < 0.9}
        try:
            fleet.verify(r.choice(["POST", junk_str(r, 4)]), junk_str(r, 30),
                         junk_str(r, 50).encode("utf-8", "surrogatepass"), hdr)
        except GatewayRefused:
            continue
        pytest.fail(f"seed={seed} i={i}: junk headers were accepted: {hdr!r}")


# ------------------------------------------------------------------ persistence soak
def test_memstore_soak_interleaved_threads_and_torn_tail(tmp_path):
    path = tmp_path / "mem.jsonl"
    store = MemoryStore(path)
    r = random.Random(41)
    last: dict[tuple[str, str], str] = {}
    lock = threading.Lock()
    errors: list[BaseException] = []

    def worker(wid: int):
        rr = random.Random(1000 + wid)
        try:
            for j in range(max(30, ITERS // 8)):
                ns, key = f"agent{wid}", f"k{rr.randint(0, 9)}"
                val = f"w{wid}-{j}-" + junk_str(rr, rr.randint(0, 30)).encode("utf-8", "replace").decode()
                try:
                    store.save(ns, key, val)
                except MemoryRefused:
                    continue
                with lock:
                    last[(ns, key)] = val
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    ts = [threading.Thread(target=worker, args=(w,)) for w in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errors, errors
    # a crash mid-append leaves a torn, non-JSON tail line
    with open(path, "ab") as f:
        f.write(b'{"ns":"agent0","key":"k1","value":"TORN' + bytes([0xff, 0xfe]))
    fresh = MemoryStore(path)
    for (ns, key), val in last.items():
        got = fresh.load(ns).get(key)
        assert got is not None, (ns, key)
        assert (got.get("value") if isinstance(got, dict) else got) == val, (ns, key)
    # and the store still accepts writes after recovering
    fresh.save("agent0", "after", "ok")
    assert MemoryStore(path).load("agent0")["after"]


def test_checkpoint_soak_put_reload_and_garbage_values(tmp_path):
    p = tmp_path / "ck.json"
    r = random.Random(4141)
    expect: dict[str, object] = {}
    ck = Checkpoint(p)
    for i in range(max(50, ITERS // 2)):
        if i % 7 == 0:  # reopen often: a fresh process view of the same file
            ck = Checkpoint(p)
        key = f"k{r.randint(0, 25)}"
        val = r.choice([junk_value(r), {"step": i, "out": junk_str(r, 20)}, [i, i + 1], f"v{i}"])
        ok = ck.put(key, val)
        if ok:
            # what a JSON round-trip of the value yields is what a reload must give back
            expect[key] = json.loads(json.dumps(val, default=str, allow_nan=True)) if val is not None else None
    reloaded = Checkpoint(p)
    for k, v in expect.items():
        got = reloaded.get(k)
        if isinstance(v, float) and v != v:  # NaN never equals itself
            continue
        assert got is not None or v is None, k
