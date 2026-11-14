"""Hardening round 56: presence (environment, agents, find_agent) and bootstrap (_describe,
hook error text, run_cleanups, apply_tool_focus)."""
from __future__ import annotations

import asyncio
import gc
import json
import subprocess
import time
from types import SimpleNamespace as S

import pytest

from avrone import bootstrap as bs
from avrone import presence as pr
from avrone.tools import builtin_registry


def run(c):
    return asyncio.run(c)


class Reg:
    def names(self):
        return ["a", "b"]


class Core:
    def __init__(self, active=None, host="h1"):
        self.runner = S(kind="local", workspace="/w")
        self.settings = S(llm_model="m", llm_base_url=None, docker_image=None)
        self.tools = Reg()
        self.active = active if active is not None else {}
        self.extensions = {}
        self.fleet = None
        self.tag = host


# ------------------------------------------------------------ agents()
@pytest.mark.parametrize("started", [None, "yesterday", float("nan"), True, 10 ** 400, [1]])
def test_agents_odd_started(started):
    c = Core({"A": {"status": "working", "started": started}, "B": {"status": "idle", "started": 5}})
    out = pr.agents(c)
    assert [a["name"] for a in out] == ["A", "B"]


def test_agents_non_dict_entry_skipped():
    c = Core({"A": None, "B": "x", "C": {"status": "working", "started": 1}, 7: {"status": "idle"}})
    assert [a["name"] for a in pr.agents(c)] == ["C", "7"]


@pytest.mark.parametrize("active", [None, [], "x"])
def test_agents_non_dict_active(active):
    c = Core()
    c.active = active
    assert pr.agents(c) == []


def test_agents_order_kept():
    c = Core({"old": {"status": "working", "started": 1}, "new": {"status": "working", "started": 9},
              "idle": {"status": "idle", "started": 99}})
    assert [a["name"] for a in pr.agents(c)] == ["new", "old", "idle"]


# ------------------------------------------------------------ _host_interfaces
@pytest.mark.parametrize("payload", ['{"a": 1}', "[1, null, \"x\"]", '[{"ifname": "e", "addr_info": null}]',
                                     '[{"ifname": "e", "addr_info": [1, {"local": "10.0.0.1"}]}]', "null", "[[]]"])
def test_host_interfaces_odd_ip_json(monkeypatch, payload):
    monkeypatch.setattr(pr.shutil, "which", lambda n: "/sbin/ip")
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, payload, ""))
    out = pr._host_interfaces()
    assert isinstance(out, list) and all(isinstance(i, dict) for i in out)


def test_host_interfaces_good(monkeypatch):
    monkeypatch.setattr(pr.shutil, "which", lambda n: "/sbin/ip")
    j = json.dumps([{"ifname": "eth0", "operstate": "UP", "addr_info": [{"local": "10.1.2.3", "prefixlen": 24}]}])
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, j, ""))
    assert pr._host_interfaces() == [{"name": "eth0", "state": "UP", "addrs": ["10.1.2.3/24"]}]


# ------------------------------------------------------------ environment(): off-loop + cache
def test_environment_does_not_block_loop(monkeypatch):
    pr._CACHE.clear()

    def slow():
        time.sleep(0.4)
        return []
    monkeypatch.setattr(pr, "_host_interfaces", slow)

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(20):
                await asyncio.sleep(0.01)
                ticks += 1
        await asyncio.gather(pr.environment(Core()), ticker())
        return ticks
    assert run(main()) >= 15


def test_cache_not_reused_for_new_core_at_same_id(monkeypatch):
    pr._CACHE.clear()
    monkeypatch.setattr(pr, "_host_interfaces", lambda: [])
    c1 = Core()
    env1 = run(pr.environment(c1))
    # simulate a recycled id: an entry for c2's id whose weakref points elsewhere
    c2 = Core()
    c2.settings.llm_model = "other"
    pr._CACHE[id(c2)] = pr._CACHE[id(c1)]
    env2 = run(pr.environment(c2))
    assert env2["services"]["model"] == "other" and env1["services"]["model"] == "m"


def test_cache_hit_same_core(monkeypatch):
    pr._CACHE.clear()
    calls = []
    monkeypatch.setattr(pr, "_host_interfaces", lambda: calls.append(1) or [])
    c = Core()
    run(pr.environment(c))
    run(pr.environment(c))
    assert len(calls) == 1


def test_cache_bounded(monkeypatch):
    pr._CACHE.clear()
    monkeypatch.setattr(pr, "_host_interfaces", lambda: [])
    keep = [Core() for _ in range(pr._CACHE_MAX + 20)]
    for c in keep:
        run(pr.environment(c))
    assert len(pr._CACHE) <= pr._CACHE_MAX


def test_cache_drops_dead_cores_first(monkeypatch):
    pr._CACHE.clear()
    monkeypatch.setattr(pr, "_host_interfaces", lambda: [])
    for _ in range(pr._CACHE_MAX):
        run(pr.environment(Core()))
    gc.collect()
    live = Core()
    run(pr.environment(live))
    assert id(live) in pr._CACHE


# ------------------------------------------------------------ find_agent
ROWS = [{"route": "", "agents": [{"name": "Builder-3", "status": "working"}]},
        {"route": "edge", "agents": [{"name": "W\nFORGED", "status": "idle"}, {"name": "dup", "status": "idle"}]},
        {"route": "edge2\u202e", "agents": [{"name": "W\nFORGED", "status": "idle"}, {"name": "dup", "status": "idle"}]}]


@pytest.mark.parametrize("ref", [" Builder-3", "Builder-3\n", "\tBuilder-3 "])
def test_find_agent_trims(ref):
    assert pr.find_agent(ROWS, ref) == ("", "Builder-3")


@pytest.mark.parametrize("ref", ["", "   ", None, 5, ["Builder-3"]])
def test_find_agent_bad_ref(ref):
    with pytest.raises(LookupError):
        pr.find_agent(ROWS, ref)


def test_ambiguous_error_escaped():
    with pytest.raises(LookupError) as ei:
        pr.find_agent(ROWS, "W\nFORGED")
    msg = str(ei.value)
    assert msg.count("\n") == 0 and "\u202e" not in msg


def test_route_form_still_works():
    assert pr.find_agent(ROWS, "edge/dup") == ("edge", "dup")


# ------------------------------------------------------------ bootstrap._describe
def test_describe_self_referencing_list():
    a = [1]
    a.append(a)
    assert bs._describe(a) == [1, "<...>"]


def test_describe_self_referencing_dict():
    d = {"x": 1}
    d["me"] = d
    out = bs._describe(d)
    assert out["x"] == 1 and out["me"] == "<...>"
    json.dumps(out)


def test_describe_status_returning_self():
    class M:
        name = "mgr"

        def status(self):
            return {"self": self, "ok": True}
    out = bs._describe(M())
    json.dumps(out)
    assert out["ok"] is True


def test_describe_deep_nesting():
    d = cur = {}
    for _ in range(5000):
        cur["n"] = {}
        cur = cur["n"]
    json.dumps(bs._describe(d))


@pytest.mark.parametrize("v,want", [(float("nan"), "nan"), (float("inf"), "inf"), (-float("inf"), "-inf"), (1.5, 1.5)])
def test_describe_floats_json_safe(v, want):
    assert bs._describe(v) == want
    json.dumps(bs._describe({"v": v}), allow_nan=False)


def test_describe_big_list_capped():
    out = bs._describe(list(range(10 ** 5)))
    assert len(out) == bs.DESCRIBE_ITEMS + 1 and out[-1].startswith("...")


def test_describe_big_dict_capped():
    out = bs._describe({i: i for i in range(10 ** 4)})
    assert len(out) == bs.DESCRIBE_ITEMS + 1 and out["..."].endswith("more")


def test_describe_raising_property():
    class P:
        @property
        def status(self):
            raise RuntimeError("no")

        @property
        def name(self):
            raise RuntimeError("no")

        def __repr__(self):
            raise RuntimeError("no")
    assert bs._describe(P()) == "<P>"


def test_describe_long_string_clipped():
    assert len(bs._describe("x" * 10 ** 6)) == bs.DESCRIBE_STR


def test_describe_plain_values():
    assert bs._describe({"a": (1, "b", None, True)}) == {"a": [1, "b", None, True]}


# ------------------------------------------------------------ hook error text
def test_errtext_clipped_and_escaped():
    t = bs._errtext(ValueError("x\nFORGED\u202e" + "y" * 10 ** 6))
    assert len(t) <= bs.ERROR_MAX + 40 and "\n" not in t and "\u202e" not in t


def test_errtext_unprintable():
    class E(Exception):
        def __str__(self):
            raise RuntimeError
    assert bs._errtext(E()) == "E: <unprintable>"


# ------------------------------------------------------------ run_cleanups
def test_cleanup_sys_exit_does_not_skip_rest():
    ran = []

    def a():
        ran.append("a")

    def b():
        raise SystemExit(3)

    async def c():
        ran.append("c")
    run(bs.run_cleanups([a, b, c]))
    assert ran == ["c", "a"]


def test_cleanup_exception_does_not_skip_rest():
    ran = []
    run(bs.run_cleanups([lambda: ran.append(1), lambda: 1 / 0]))
    assert ran == [1]


# ------------------------------------------------------------ apply_tool_focus
@pytest.mark.parametrize("spec", ["nope", "none", "read_fil", " , ,nope2"])
def test_focus_unknown_only_keeps_all(spec):
    r = builtin_registry()
    n = len(r.names())
    assert bs.apply_tool_focus(r, spec) == [] and len(r.names()) == n


def test_focus_partial_known_still_trims():
    r = builtin_registry()
    bs.apply_tool_focus(r, "read_file,nope")
    assert r.names() == ["read_file"]


def test_focus_env(monkeypatch):
    monkeypatch.setenv("AVRONE_TOOLS_ONLY", "zzz_not_a_tool")
    r = builtin_registry()
    n = len(r.names())
    bs.apply_tool_focus(r)
    assert len(r.names()) == n
