"""Round 76: node records loaded from fleet.json go through register()'s checks.

The file outlives versions and can be edited by hand, but records were loaded as is: a url at a
cloud metadata endpoint or carrying credentials was dispatched to without check_node_url, a key
like "../x" or this node's own id became a target, a record without node_id raised KeyError in
pick(), and capabilities / children / health of the wrong type reached node_has, fleet_nodes and
health_score unclamped.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import fleet as _fl
from avrone.fleet import FleetManager, health_score, node_has

clean_saved_node = getattr(_fl, "clean_saved_node", None)


def _fm(d: Path) -> FleetManager:
    core = SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None),
                           settings=SimpleNamespace(data_dir=str(d)), active={})
    return FleetManager(core, data_dir=d)


@pytest.fixture
def d(monkeypatch, tmp_path):
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "r76-" + "k" * 40)
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    for v in ("AVRONE_FLEET_ALLOW", "AVRONE_FLEET_REQUIRE_ALLOW", "AVRONE_FLEET_PARENT"):
        monkeypatch.delenv(v, raising=False)
    return tmp_path


def _write(d, nodes):
    (d / "fleet.json").write_text(json.dumps({"node_id": "root", "nodes": nodes}))


def test_unsafe_urls_and_ids_dropped_at_load(d):
    now = time.time()
    _write(d, {
        "meta": {"url": "http://169.254.169.254/latest", "last_seen": now},
        "creds": {"url": "http://user:pw@peer:8080", "last_seen": now},
        "ftp": {"url": "ftp://peer", "last_seen": now},
        "nourl": {"last_seen": now},
        "../x": {"url": "http://peer:8080", "last_seen": now},
        "UPPER": {"url": "http://peer:8080", "last_seen": now},
        "root": {"url": "http://peer:8080", "last_seen": now},
        "good": {"url": "http://peer:8080/", "last_seen": now},
    })
    fm = _fm(d)
    assert set(fm.nodes) == {"good"}
    assert fm.nodes["good"]["url"] == "http://peer:8080"
    assert fm.pick("auto")["node_id"] == "good"


def test_record_without_node_id_gets_its_key_and_pick_works(d):
    now = time.time()
    _write(d, {"a1": {"url": "http://a1:1", "last_seen": now, "node_id": "spoofed"}})
    fm = _fm(d)
    assert fm.nodes["a1"]["node_id"] == "a1"  # key wins: the record can't claim another id
    assert fm.pick("a1")["node_id"] == "a1"


def test_pick_error_path_tolerates_hot_nodes_from_disk(d):
    now = time.time()
    _write(d, {"h1": {"url": "http://h1:1", "last_seen": now, "health": {"state": "hot"}}})
    fm = _fm(d)
    with pytest.raises(RuntimeError, match="hot or throttled"):
        fm.pick("auto")


def test_wrong_types_normalized_at_load(d):
    now = time.time()
    _write(d, {"w1": {"url": "http://w1:1", "last_seen": "soon", "capabilities": ["runner"],
                      "children": "kid", "load": "lots", "presence": [1, 2],
                      "health": {"headroom": "abc", "load": 1e9, "state": "melting", "temp_c": "hot",
                                 "active_agents": 5.5}}})
    fm = _fm(d)
    n = fm.nodes["w1"]
    assert n["capabilities"] == {} and n["children"] == [] and n["load"] == 0
    assert n["presence"] is None and n["last_seen"] == 0.0
    assert n["health"] == {"load": 1000.0}
    assert 0 <= health_score(n) <= 100 and node_has(n, "docker") is False
    fm.list_nodes()
    fm.health_table()
    fm.info()


def test_good_record_round_trips(d):
    now = time.time()
    rec = {"url": "http://g:9", "last_seen": now, "capabilities": {"runner": "docker", "tools": ["shell"]},
           "children": ["c1", 3, "c2"], "load": 2, "presence": {"agents": []},
           "health": {"state": "ok", "headroom": 0.9, "active_agents": 1}}
    _write(d, {"g1": rec})
    n = _fm(d).nodes["g1"]
    assert n["capabilities"] == rec["capabilities"] and n["children"] == ["c1", "c2"]
    assert n["load"] == 2 and n["presence"] == {"agents": []}
    assert n["health"] == {"state": "ok", "headroom": 0.9, "active_agents": 1}
    assert n["last_seen"] == now


def test_clean_saved_node_direct():
    assert clean_saved_node(5, {"url": "http://a:1"}, "root") is None
    assert clean_saved_node("a", "notadict", "root") is None
    assert clean_saved_node("a", {"url": "http://a:1"}, "a") is None
    assert clean_saved_node("a", {"url": "http://a:1"}, "root")["node_id"] == "a"
