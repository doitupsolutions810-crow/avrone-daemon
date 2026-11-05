"""Round 48: memory store, OSINT rollup, presence tree from peers, agent kit.

  * memstore: a lone surrogate raised UnicodeEncodeError past every `except MemoryRefused`; a key with
    control characters ("k\\x00") was stored as a different key; hand-edited lines with dict keys or
    null values were served as is; trim("abc"/NaN/inf/None) raised. Plus 4 processes x 60 saves, a
    seeded junk-journal replay and a merge storm;
  * osint_metrics: run logs are streamed (no whole-file read), lines over 1 MB skipped, hosts clipped;
  * presence: a peer's presence nested 500 levels, `children` as a list, nameless agents, "/" in ids
    used to break /api/fleet/directory and agent lookup for the whole fleet;
  * agentkit: env_install's lock file name took `manager` unchecked (path traversal, truncating
    <anything>.lock outside the data dir); one torn ledger line broke env_install/resources for good;
    resources limit junk/0/negative; task board capped.
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing as mp
import os
import random
import string
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import agentkit
from avrone.memstore import MemoryRefused, MemoryStore
from avrone.sovereign import CommandResult

FORK = pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")


def run(c):
    return asyncio.run(c)


# ------------------------------------------------------------------ memstore
@pytest.mark.parametrize("key,value", [("k", "\ud800"), ("\udfff", "v"), ("k\udc80x", "v"), ("ok", "a\udbffb")])
def test_bad_text_is_a_memory_refusal(tmp_path, key, value):
    m = MemoryStore(tmp_path / "m.jsonl")
    with pytest.raises(MemoryRefused):
        m.save("agent", key, value)
    assert m.load("agent") == {}


def test_hand_edited_lines_are_read_like_merges(tmp_path):
    m = MemoryStore(tmp_path / "m.jsonl")
    m.save("a", "good", "v")
    with open(m.path, "a") as f:
        for rec in [{"ns": "a", "key": {"x": 1}, "value": "v", "ts": 1}, {"ns": "a", "key": "nul", "value": None},
                    {"ns": "a", "key": True, "value": "v"}, {"ns": "a", "key": ["l"], "value": "v"},
                    {"ns": "a", "key": 7, "value": 3.5, "ts": 2}, {"ns": "a", "key": "obj", "value": {"d": 1}},
                    {"ns": "a", "key": "  ", "value": "blank"}]:
            f.write(json.dumps(rec) + "\n")
        f.write('{"ns":"a","key":"torn","val')
    got = m.load("a")
    assert set(got) == {"good", "7"} and got["7"]["value"] == "3.5"
    m.save("a", "after", "ok")  # the store still saves after reading junk
    assert set(m.load("a")) == {"good", "7", "after"}


@pytest.mark.parametrize("keep", ["abc", float("nan"), float("inf"), None, [], "1e999"])
def test_trim_junk_keep_is_refused(tmp_path, keep):
    m = MemoryStore(tmp_path / "m.jsonl")
    m.save("a", "k", "v")
    with pytest.raises(MemoryRefused):
        m.trim("a", keep)
    assert set(m.load("a")) == {"k"}


def _proc_saves(path: str, wid: int) -> None:
    m = MemoryStore(Path(path))
    for i in range(60):
        m.save(f"ns{wid % 2}", f"w{wid}-{i}", "x" * (i * 50))


@FORK
def test_processes_saving_keep_every_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "5000")
    ctx = mp.get_context("fork")
    ps = [ctx.Process(target=_proc_saves, args=(str(tmp_path / "m.jsonl"), w)) for w in range(4)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(120)
        assert p.exitcode == 0
    spaces = MemoryStore(tmp_path / "m.jsonl").namespaces()
    keys = set(spaces.get("ns0", {})) | set(spaces.get("ns1", {}))
    assert keys == {f"w{w}-{i}" for w in range(4) for i in range(60)}


@pytest.mark.parametrize("seed", range(3))
def test_merge_storm_and_junk_replay(tmp_path, seed, monkeypatch):
    monkeypatch.setenv("AVRONE_MEMORY_MAX_ENTRIES", "40")
    r = random.Random(4800 + seed)
    m = MemoryStore(tmp_path / "m.jsonl")
    junk = [None, True, 1.5, float("nan"), float("inf"), "\ud800", "x" * 70_000, {"a": 1}, [1], 10**400, "", " "]
    for _ in range(150):
        entries = [{"key": r.choice(junk + [f"k{r.randint(0, 80)}"]), "value": r.choice(junk + ["v"]),
                    "ts": r.choice(junk + [r.uniform(0, 2e9)]), "agent": r.choice(junk)} for _ in range(r.randint(0, 12))]
        entries += r.choice([[None, 1, "x"], [], [{"key": "k1"}]])
        st = m.merge(r.choice(["a", "b"]), entries)
        assert sum(st.values()) == len(entries)
        if r.random() < 0.1:
            with open(m.path, "ab") as f:
                f.write(r.choice([b"\xff\xfe\n", b"[" * 20000 + b"\n", b'{"key":', b"null\n"]))
    for ns, space in m.namespaces().items():
        assert len(space) <= 40
        for k, rec in space.items():
            assert isinstance(k, str) and isinstance(rec["value"], str)
            json.dumps(rec, allow_nan=False)


# ------------------------------------------------------------------ osint
def _octx(tmp_path):
    return SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path)))


def test_osint_metrics_streams_hostile_logs(tmp_path):
    from avrone.osint import t_osint_metrics
    runs = tmp_path / "runs"
    runs.mkdir()
    big_host = "h" * 500_000  # under the 1 MB line cap: kept, but clipped
    with open(runs / "a.jsonl", "w") as f:
        f.write(json.dumps({"kind": "recon.dns", "data": {"host": "example.com"}}) + "\n")
        f.write(json.dumps({"kind": "recon.dns", "data": {"host": big_host}}) + "\n")
        f.write("x" * (3 * 1024 * 1024) + "\n")      # an over-long line: skipped
        f.write(json.dumps({"kind": "osint.domain", "data": {"host": "example.com"}}) + "\n")
        f.write(json.dumps({"kind": "recon.refused", "data": {"tool": "t" * 10_000}}) + "\n")
        f.write("[" * 100_000 + "\n" + "\xff\n" + json.dumps({"kind": 5}) + "\n" + '{"kind":"recon.x","data":[1]}\n')
        f.write('{"kind":"recon.tls","data":{"host":"torn')  # torn tail
    (runs / "dir.jsonl").mkdir()
    (runs / "dangling.jsonl").symlink_to(tmp_path / "missing")
    out = run(t_osint_metrics(_octx(tmp_path), {}))
    assert "example.com: 2" in out and len(out) < 4000
    assert "recon.dns: 2" in out and "across 1 run logs" in out


# ------------------------------------------------------------------ presence
def _deep(n: int) -> dict:
    snap: dict = {"node_id": "leaf", "agents": [{"name": "Deep-1", "status": "working"}], "children": {}}
    for i in range(n):
        snap = {"node_id": f"n{i}", "agents": [], "children": {f"n{i}": snap}}
    return snap


HOSTILE_TREES = [
    _deep(500),
    {"node_id": "x", "children": [1, 2, 3], "agents": "nope", "env": "junk"},
    {"node_id": "x", "children": {"a/b": {"agents": [{"name": "Spoof"}]}, "": {}, "ok": None}},
    {"node_id": {"d": 1}, "agents": [None, 1, {"role": "no name"}, {"name": 5}, {"name": "Real-1"}],
     "env": {"networks": [None, {"name": None, "addrs": "1.2.3.4"}, {"name": "eth0", "addrs": [1, None]}],
             "runner": {"container": "str"}, "device": [], "services": 5}},
    {"children": {f"c{i}": {"children": {f"d{j}": {} for j in range(100)}} for i in range(100)}},
]


@pytest.mark.parametrize("i", range(len(HOSTILE_TREES)))
def test_presence_tree_from_peers_never_breaks_directory(i):
    from avrone.presence import MAX_ROWS, MAX_TREE_DEPTH, find_agent, flatten, render
    rows = flatten({"node_id": "root", "agents": [{"name": "Me-1", "status": "working"}],
                    "children": {"peer": HOSTILE_TREES[i]}})
    assert 1 <= len(rows) <= MAX_ROWS
    assert all(r["route"].count("/") < MAX_TREE_DEPTH for r in rows)
    text = render(rows)
    assert "Me-1" in text
    assert find_agent(rows, "Me-1") == ("", "Me-1")
    with pytest.raises(LookupError):
        find_agent(rows, "Nobody-9")
    json.dumps(rows, default=str)


# ------------------------------------------------------------------ agentkit
class _Runner:
    def __init__(self, ws):
        self.workspace, self.kind, self.cmds = ws, "local", []

    async def run(self, cmd, timeout=None):
        self.cmds.append(cmd)
        return CommandResult(cmd=cmd, exit_code=0, stdout="ok", stderr="")


def _kctx(tmp: Path):
    ws = tmp / "ws"
    ws.mkdir(exist_ok=True)
    core = SimpleNamespace(runner=_Runner(ws), settings=SimpleNamespace(data_dir=tmp / "d"),
                           codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r1", name="Agent-1", depth=0)


@pytest.mark.parametrize("mgr", ["x/../../victim", "x/../../../victim", "../../victim", "/tmp/victim", "pip/../../victim", "brew", ""])
def test_env_install_manager_never_names_a_file(tmp_path, mgr):
    victim = tmp_path / "victim.lock"
    victim.write_text("precious")
    c = _kctx(tmp_path)
    # with a ".install-x" dir present, v0.40 opened d/.install-x/../../victim.lock with "w": truncated
    (tmp_path / "d" / ".install-x").mkdir(parents=True, exist_ok=True)
    (tmp_path / "d" / ".install-pip").mkdir(parents=True, exist_ok=True)
    out = run(agentkit.t_env_install(c, {"package": "requests", "manager": mgr}))
    assert out.startswith("ERROR") and victim.read_text() == "precious"
    assert not c.core.runner.cmds
    assert not [p for p in tmp_path.rglob(".install-*") if p.is_file()]


def test_torn_ledger_does_not_break_install_or_resources(tmp_path):
    c = _kctx(tmp_path)
    led = agentkit._ledger(c)
    led.write_text('[1]\n5\n"s"\nnull\n{"ts": "x", "agent": 1}\n{"ok": true, "action": "pip install", '
                   '"target": "requests", "ts": 1e999}\n{"torn\n' + "[" * 50000 + "\n")
    out = run(agentkit.t_env_install(c, {"package": "requests", "manager": "pip"}))
    assert "already installed" in out
    for lim in [None, 0, -5, "abc", 1e999, float("nan"), 10**400, 2]:
        res = run(agentkit.t_resources(c, {} if lim is None else {"limit": lim}))
        assert isinstance(res, str) and not res.startswith("Traceback")
    assert run(agentkit.t_resources(c, {"limit": 1})).count("\n") == 0


def test_task_board_is_capped(tmp_path):
    c = _kctx(tmp_path)
    for i in range(agentkit.MAX_BOARD_TASKS):
        run(agentkit.t_task_board(c, {"op": "add", "title": f"t{i}"}))
    out = run(agentkit.t_task_board(c, {"op": "add", "title": "one too many"}))
    assert out.startswith("ERROR") and "full" in out
    assert run(agentkit.t_task_board(c, {"op": "update", "id": "3", "status": "done"})).count("[done]") == 1
