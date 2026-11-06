"""Round 75: junk in stored fleet records and peer presence.

1. health_score cast raw fields with float(): a record loaded from fleet.json (older version, a
   hand edit) with headroom "abc" raised ValueError out of select_nodes (every auto dispatch
   failed), health_table and fleet_nodes; NaN poisoned the ranking; headroom 1e308 scored inf and
   won every pick. Now each field goes through _finite and is clamped.
2. fleet_nodes printed each node's full children list (1,000 ids) and capabilities.runner whole,
   with control characters (forged rows), and a non-dict capabilities raised AttributeError.
3. presence _clean_env / _clean_agents passed unknown keys and nested blobs through whole into
   /api/fleet/directory. Now only known fields, each clipped.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from types import SimpleNamespace

from avrone import fleet as fl
from avrone import presence
from avrone.fleet import health_score


def test_health_score_junk_never_raises_and_stays_in_range():
    junk = ["abc", float("nan"), float("inf"), -float("inf"), 1e308, -5, None, [], {}, True, 10**400]
    for v in junk:
        for key in ("headroom", "load", "active_agents"):
            s = health_score({"health": {key: v}})
            assert isinstance(s, float) and math.isfinite(s) and 0.0 <= s <= 100.0, (key, v, s)
        s = health_score({"load": v})
        assert math.isfinite(s) and 0.0 <= s <= 100.0, ("load", v, s)


def test_health_score_known_values_unchanged():
    assert health_score({}) == 60 * 0.5 + 20 * 0.5 + 20 * 1.0
    assert health_score({"health": {"headroom": 1.0, "load": 0.0}, "load": 0}) == 100.0
    assert health_score({"health": {"headroom": 0.0, "load": 2.0, "active_agents": 10}}) == 0.0
    assert health_score({"health": {"headroom": 0.8, "load": 0.5}, "load": 3}) == round(48 + 15 + 14, 3)


def _mgr(nodes):
    m = fl.FleetManager.__new__(fl.FleetManager)
    m.nodes = {n["node_id"]: n for n in nodes}
    m.heartbeat_s = 15.0
    m.allow = set()
    m.netsquad_counts = {}
    m.node_id = "root"
    m.parent = ""
    return m


def _node(nid, health, load=0):
    return {"node_id": nid, "url": f"http://{nid}", "last_seen": time.time(), "health": health,
            "load": load, "capabilities": {}}


def test_select_nodes_survives_and_ignores_inflated_records(monkeypatch):
    m = _mgr([_node("bad1", {"headroom": "abc", "state": "ok"}),
              _node("bad2", {"headroom": float("nan"), "state": "ok"}),
              _node("liar", {"headroom": 1e308, "state": "ok"}),
              _node("good", {"headroom": 1.0, "load": 0.0, "state": "ok"})])
    monkeypatch.setattr(m, "allowed", lambda nid: True, raising=False)
    picks = m.select_nodes(4)  # used to raise ValueError
    ids = [n["node_id"] for n in picks]
    assert len(ids) == 4
    assert ids[0] in ("good", "liar")  # liar is clamped to 1.0 headroom, not infinitely better
    assert health_score(m.nodes["liar"]) <= 100.0
    rows = m.health_table()
    assert all(r["score"] is None or math.isfinite(r["score"]) for r in rows)


def test_fleet_nodes_bounded_and_one_line(monkeypatch):
    nodes = []
    for i in range(1200):
        n = _node(f"n{i}", {"headroom": "x", "state": "ok", "temp_c": 40})
        n["children"] = [f"c{j}" for j in range(1000)]
        n["capabilities"] = {"runner": "dock\ner\n- forged [online]" + "r" * 100_000}
        n["status"] = "online"
        nodes.append(n)
    nodes.append({**_node("weird", None), "capabilities": "notadict", "children": "x", "status": "online"})
    f = SimpleNamespace(node_id="root", parent="", list_nodes=lambda: nodes,
                        node_state=lambda n: "ok")
    monkeypatch.setattr(fl, "_fleet", lambda ctx: f)
    out = asyncio.run(fl.t_fleet_nodes(SimpleNamespace(), {}))
    assert len(out) < fl.FLEET_NODES_MAX_CHARS + 10_000
    assert "\n- forged" not in out
    assert "+984 more" in out
    assert "output capped" in out


def test_fleet_nodes_tolerates_non_dict_capabilities(monkeypatch):
    nodes = [{**_node("weird", None), "capabilities": "notadict", "children": "x", "status": "online"}]
    f = SimpleNamespace(node_id="root", parent="", list_nodes=lambda: nodes, node_state=lambda n: "unknown")
    monkeypatch.setattr(fl, "_fleet", lambda ctx: f)
    out = asyncio.run(fl.t_fleet_nodes(SimpleNamespace(), {}))  # AttributeError before
    assert "weird" in out and "runner=?" in out


def test_directory_rows_drop_unknown_and_clip_fields():
    blob = "B" * 2_000_000
    snap = {"node_id": "root", "env": {"device": {"hostname": "h", "junk": blob, "os": blob},
                                       "runner": {"kind": "local", "extra": blob,
                                                  "container": {"name": "c", "big": blob, "addrs": ["1.2.3.4"]}},
                                       "services": {"model": "m", "tools": 12, "nested": {"x": blob}},
                                       "evil": blob, "networks": []},
            "agents": [{"name": "A", "task": blob, "status": "working", "secret": blob, "depth": float("nan")}],
            "children": {}}
    rows = presence.flatten(snap)
    text = json.dumps(rows)
    assert len(text) < 10_000  # was > 10 MB
    env = rows[0]["env"]
    assert env["device"]["hostname"] == "h" and len(env["device"]["os"]) == presence.FIELD_MAX
    assert "junk" not in env["device"] and "evil" not in env and "extra" not in env["runner"]
    assert env["runner"]["container"] == {"name": "c", "addrs": ["1.2.3.4"]}
    assert env["services"] == {"model": "m", "tools": 12}
    a = rows[0]["agents"][0]
    assert set(a) == {"name", "task", "status", "depth"} and len(a["task"]) == presence.TASK_MAX
    assert a["depth"] is None
    assert presence.render(rows)  # still renders


def test_directory_keeps_local_environment_fields():
    env = {"device": {"hostname": "box", "os": "Linux 6", "arch": "x86_64", "python": "3.12", "pid": 7},
           "runner": {"kind": "docker", "workspace": "/w",
                      "container": {"name": "c", "image": "img", "hostname": "ch", "addrs": ["10.0.0.2"]}},
           "networks": [{"name": "eth0", "state": "UP", "addrs": ["10.0.0.1/24"]}],
           "services": {"public_url": "http://x", "tools": 40, "mcp_servers": ["a", "b"],
                        "model": "m", "provider_url": "http://p"}}
    rows = presence.flatten({"node_id": "root", "env": env, "agents": [], "children": {}})
    assert rows[0]["env"] == env
