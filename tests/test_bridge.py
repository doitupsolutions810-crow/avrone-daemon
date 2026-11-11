"""Learning bridge: run-scoped blackboard, tools, persistence, fleet relay hooks, API."""
import asyncio
import json
import threading

import pytest

from avrone import bridge as bridge_mod
from avrone.bridge import Bridge, BridgeError, compact, for_core
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools import ToolRegistry
from avrone.trinity import AgentContext, TrinityCore


def _core(tmp_path, llm=None, **over):
    s = Settings()
    s.data_dir = tmp_path / "d"
    s.workspace = tmp_path / "ws"
    s.max_steps = 3
    for k, v in over.items():
        setattr(s, k, v)
    reg = ToolRegistry()
    bridge_mod.register_bridge_tools(reg)
    return TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir), tools=reg)


# ------------------------------------------------------------------ posting + filters

def test_post_and_read_filters(tmp_path):
    b = Bridge("r1", tmp_path)
    f = b.post("Alpha-1", "finding", "port 22 is open", role="Recon")
    q = b.post("Beta-2", "question", "is 22 filtered upstream?")
    a = b.reply("Alpha-1", [q["id"]], "no, reachable from the LAN")
    c = b.post("Beta-2", "conclusion", "port 22 open on the LAN", refs=[f["id"], a["id"]])
    assert [p["id"] for p in b.posts()] == [f["id"], q["id"], a["id"], c["id"]]
    assert set(f) >= {"id", "run_id", "agent", "role", "kind", "text", "refs", "ts", "origin_node"}
    assert f["origin_node"] == "local" and f["run_id"] == "r1" and f["role"] == "Recon"
    assert [p["kind"] for p in b.read(kind="question")] == ["question"]
    assert {p["agent"] for p in b.read(agent="beta")} == {"Beta-2"}
    assert [p["id"] for p in b.read(agent="recon")] == [f["id"]]  # role match
    assert [p["id"] for p in b.read(since=q["id"])] == [a["id"], c["id"]]
    assert [p["id"] for p in b.read(query="LAN")] == [a["id"], c["id"]]
    assert len(b.read(limit=1)) == 1 and b.read(limit=1)[0]["id"] == c["id"]
    assert a["refs"] == [q["id"]]
    assert b.unresolved() == []
    line = compact(b.read(kind="answer"))
    assert line.startswith(f"[{a['id']}] answer Alpha-1 re {q['id']}:")
    assert compact([]) == "(no posts)"


def test_post_validation(tmp_path):
    b = Bridge("r1", tmp_path)
    with pytest.raises(BridgeError):
        b.post("a", "rumour", "x")
    with pytest.raises(BridgeError):
        b.post("a", "finding", "   ")
    with pytest.raises(BridgeError):
        b.post("a", "finding", "x", refs=["nope"])
    with pytest.raises(BridgeError):
        b.reply("a", [], "x")
    p = b.post("a", "FINDING", "x", refs="")
    assert p["kind"] == "finding" and p["refs"] == []


def test_caps_text_and_posts(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "3")
    b = Bridge("r1", tmp_path)
    p = b.post("a", "finding", "x" * 9000)
    assert len(p["text"]) == bridge_mod.MAX_TEXT and p["text"].endswith("...")
    ids = [b.post("a", "finding", f"n{i}")["id"] for i in range(5)]
    assert [x["id"] for x in b.posts()] == ids[-3:]
    # file is compacted, replay keeps the last 3
    for i in range(10):
        b.post("a", "finding", f"m{i}")
    assert len(Bridge("r1", tmp_path).posts()) == 3
    lines = (tmp_path / "bridge" / "r1.jsonl").read_text().splitlines()
    assert len(lines) <= 6
    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "junk")
    assert bridge_mod.max_posts() == 500
    monkeypatch.setenv("AVRONE_BRIDGE_MAX_POSTS", "999999")
    assert bridge_mod.max_posts() == bridge_mod.HARD_MAX_POSTS


def test_rounds_env(monkeypatch):
    monkeypatch.delenv("AVRONE_BRIDGE_ROUNDS", raising=False)
    assert bridge_mod.bridge_rounds() == 1
    for raw, want in (("0", 0), ("2", 2), ("9", 3), ("-4", 0), ("x", 1)):
        monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", raw)
        assert bridge_mod.bridge_rounds() == want


def test_persistence_and_corrupt_replay(tmp_path):
    b = Bridge("run/../x", tmp_path)
    p1 = b.post("a", "finding", "one")
    p2 = b.post("b", "correction", "two", refs=[p1["id"]])
    path = tmp_path / "bridge" / f"{bridge_mod.safe_run_id('run/../x')}.jsonl"
    assert path.exists() and path.parent == tmp_path / "bridge"
    with path.open("a") as f:
        f.write('{"id": "trunc", "kind": "finding", "te\n')
        f.write("not json\n[1,2]\n")
        f.write(json.dumps({"id": "badkind", "kind": "gossip", "text": "x"}) + "\n")
        f.write(json.dumps({"id": "bad id!", "kind": "finding", "text": "x"}) + "\n")
        f.write("\n")
    again = Bridge("run/../x", tmp_path)
    assert [p["id"] for p in again.posts()] == [p1["id"], p2["id"]]
    assert again.get(p2["id"])["refs"] == [p1["id"]]
    p3 = again.post("c", "conclusion", "three", refs=[p2["id"]])
    assert [p["id"] for p in Bridge("run/../x", tmp_path).posts()][-1] == p3["id"]


def test_memory_only_bridge_and_thread_safety():
    b = Bridge("mem")
    def worker(n):
        for i in range(50):
            b.post(f"w{n}", "finding", f"{n}-{i}")
    ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(b) == 400 and len({p["id"] for p in b.posts()}) == 400


def test_wave1_shares_bridge_and_digest(tmp_path):
    assert bridge_mod.bridge_run_id("abc-wave1") == "abc"
    assert bridge_mod.bridge_run_id("-wave1") == "-wave1"
    core = _core(tmp_path)
    assert for_core(core, "abc-wave1") is for_core(core, "abc")
    b = for_core(core, "abc")
    assert b.digest() == ""
    b.post("a", "finding", "f")
    assert "no conclusions" in b.digest()
    q = b.post("a", "question", "why?")
    c = b.post("b", "correction", "fix it")
    b.post("c", "conclusion", "agreed", refs=[c["id"]])
    d = b.digest()
    assert "What agents concluded together" in d and "agreed" in d
    assert "Unresolved" in d and q["id"] in d.split("Unresolved")[1]
    assert c["id"] not in d.split("Unresolved")[1]
    assert len(b.digest(max_chars=40)) == 40


def test_export_merge_remote(tmp_path):
    a = Bridge("r", tmp_path / "a", node_id="node-a")
    p = a.post("x", "finding", "local fact")
    a2 = a.post("x", "conclusion", "local conclusion")
    assert [x["id"] for x in a.export()] == [p["id"], a2["id"]]
    assert [x["id"] for x in a.export(since=p["id"])] == [a2["id"]]
    assert len(a.export(since="unknown")) == 2
    b = Bridge("r", tmp_path / "b", node_id="node-b")
    mine = b.post("y", "finding", "b's own")
    forged = dict(mine, text="overwritten!", origin_node="node-a")
    stats = b.merge_remote(a.export() + [forged, {"junk": 1}, "x"], "node-a")
    assert stats == {"merged": 2, "skipped": 1, "rejected": 2}
    assert b.get(mine["id"])["text"] == "b's own"
    assert b.get(p["id"])["origin_node"] == "node-a"
    # relayed again (and via a third node): still deduped, original origin kept
    stats2 = b.merge_remote([dict(a.get(p["id"]), origin_node=None)], "node-c")
    assert stats2["skipped"] == 1
    c = Bridge("r", tmp_path / "c", node_id="node-c")
    c.merge_remote(b.export(), "node-b")
    assert c.get(p["id"])["origin_node"] == "node-a" and c.get(mine["id"])["origin_node"] == "node-b"
    # persisted
    assert Bridge("r", tmp_path / "c").get(p["id"])["text"] == "local fact"


def test_codex_event_on_post(tmp_path):
    core = _core(tmp_path)
    for_core(core, "r9").post("A-1", "finding", "hello")
    evs = [e for e in core.codex.replay("r9") if e["kind"] == "bridge.post"]
    assert evs and evs[0]["agent"] == "A-1" and evs[0]["data"]["text"] == "hello"


# ------------------------------------------------------------------ tools (shared across depths)

def test_tools_same_bridge_any_depth(tmp_path):
    core = _core(tmp_path)
    core.active["W-1"] = {"role": "Worker", "status": "working"}
    w = AgentContext(core, "run7", "W-1", 0)
    sub = AgentContext(core, "run7", "Helper-5", 2)
    other = AgentContext(core, "run8", "Z-9", 0)

    async def go():
        out = await core.tools.call(w, "bridge_post", {"text": "found X", "kind": "finding"})
        pid = out.split("[")[1].split("]")[0]
        seen = await core.tools.call(sub, "bridge_read", {})
        rep = await core.tools.call(sub, "bridge_reply", {"refs": [pid], "text": "confirmed X"})
        bad = await core.tools.call(sub, "bridge_reply", {"refs": [], "text": "x"})
        bad2 = await core.tools.call(w, "bridge_post", {"text": "x", "kind": "gossip"})
        filt = await core.tools.call(w, "bridge_read", {"kind": "answer", "limit": "junk"})
        iso = await core.tools.call(other, "bridge_read", {})
        return out, pid, seen, rep, bad, bad2, filt, iso
    out, pid, seen, rep, bad, bad2, filt, iso = asyncio.run(go())
    assert "posted finding" in out and "found X" in seen and "W-1" in seen
    assert rep.startswith("replied answer") and pid in rep
    assert bad.startswith("ERROR") and "refs" in bad and bad2.startswith("ERROR")
    assert "confirmed X" in filt and "found X" not in filt
    assert iso == "(no posts)"
    assert for_core(core, "run7").posts()[0]["role"] == "Worker"
    assert core.tools.check("bridge_reply", {"text": "x"}) is not None


def test_extra_tools_register_bridge():
    from avrone.tools import builtin_registry
    from avrone.tools_extra import register_extra_tools
    r = builtin_registry()
    register_extra_tools(r)
    assert {"bridge_post", "bridge_read", "bridge_reply"} <= set(r.names())


def test_for_core_cache_bounded(tmp_path):
    core = _core(tmp_path)
    for i in range(bridge_mod._MAX_OPEN + 5):
        for_core(core, f"r{i}")
    assert len(core._bridges) == bridge_mod._MAX_OPEN
    assert bridge_mod.load_posts(tmp_path / "d", "nope") is None


# ------------------------------------------------------------------ API

def test_api_run_bridge(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from avrone import server
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        core = server.state["core"]
        b = for_core(core, "runA")
        p = b.post("A-1", "conclusion", "we agree")
        r = c.get("/api/runs/runA/bridge")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1 and body["posts"][0]["id"] == p["id"]
        # persisted bridge, not open in this core
        Bridge("runB", tmp_path / "data").post("B-1", "finding", "on disk")
        assert c.get("/api/runs/runB/bridge").json()["posts"][0]["text"] == "on disk"
        # a run with a log but no bridge -> empty; unknown -> 404; junk id -> 400
        core.codex.emit("runC", "run.start", "Trinity", prompt="x")
        assert c.get("/api/runs/runC/bridge").json() == {"run_id": "runC", "count": 0, "posts": []}
        assert c.get("/api/runs/ghost/bridge").status_code == 404
        assert c.get("/api/runs/a%20b/bridge").status_code == 400


# ------------------------------------------------------------------ cross-examination in squad

PLAN3 = json.dumps({"subtasks": [
    {"role": "A", "focus": "alpha"}, {"role": "B", "focus": "beta"}, {"role": "C", "focus": "gamma"}]})


class ScriptLLM:
    """Planner/lead scripted; worker finals per focus; cross-exam replies scripted per round."""

    def __init__(self, xexam=None, boom=(), worker_text=None):
        self.xexam = xexam or (lambda label, rnd, user: '{"posts": [], "revised": ""}')
        self.boom = set(boom)
        self.worker_text = worker_text or {}
        self.lead_inputs: list[str] = []
        self.worker_systems: list[str] = []
        self.xcalls: list[tuple[str, int]] = []

    async def chat(self, messages, **kw):
        system = messages[0]["content"]
        if "You are the PLANNER" in system:
            return PLAN3
        if "You are the LEAD" in system:
            self.lead_inputs.append(messages[1]["content"])
            return "LEAD MERGED " + ("x" * 400)
        if "CROSS-EXAMINATION agent" in system:
            label = system.split("CROSS-EXAMINATION agent ")[1].split(" ")[0]
            rnd = int(system.split("(round ")[1].split(")")[0])
            self.xcalls.append((label, rnd))
            out = self.xexam(label, rnd, messages[1]["content"])
            if isinstance(out, BaseException):
                raise out
            return out
        self.worker_systems.append(system)
        for focus in self.boom:
            if f"Your focus: {focus}" in system:
                raise RuntimeError("worker crashed")
        for focus, text in self.worker_text.items():
            if f"Your focus: {focus}" in system:
                return json.dumps({"final": text})
        return '{"final": "worker ok"}'


def test_rounds_zero_is_old_behaviour(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", "0")
    llm = ScriptLLM()
    core = _core(tmp_path, llm)
    out = asyncio.run(core.squad("task", n=3, run_id="z0"))
    assert llm.xcalls == [] and "bridge" not in out
    assert llm.lead_inputs[0] == ("TASK:\ntask\n\nAGENT REPORTS:\n" + "\n\n".join(
        f"### Agent {i + 1}: {r} (focus: {f})\nworker ok" for i, (r, f) in enumerate(
            [("A", "alpha"), ("B", "beta"), ("C", "gamma")])))
    assert for_core(core, "z0").posts() == []
    assert not any(e["kind"].startswith("bridge.") for e in core.codex.replay("z0"))


def test_cross_exam_round_posts_and_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", "3")

    def xexam(label, rnd, user):
        assert "OTHER AGENTS' RESULTS" in user and "BRIDGE POSTS" in user
        if rnd == 1 and label == "Agent2:B":
            fid = user.split("BRIDGE POSTS:\n")[1].split("[")[1].split("]")[0]
            return json.dumps({"posts": [
                {"kind": "correction", "text": "Agent1 got the port wrong: it is 443", "refs": [fid, "ghost"]},
                {"kind": "question", "text": "is TLS terminated upstream?"}],
                "revised": "B revised: port 443"})
        if rnd == 2 and label == "Agent1:A":
            return '```json\n{"posts": [{"kind": "conclusion", "text": "port is 443"}], "revised": "A: 443"}\n```'
        return "no json at all"
    llm = ScriptLLM(xexam=xexam, worker_text={"alpha": "port 80", "beta": "port 443"})
    core = _core(tmp_path, llm)
    out = asyncio.run(core.squad("which port?", n=3, run_id="x1"))
    br = out["bridge"]
    assert br["rounds_run"] == 2 and br["stop_reason"] == "converged"
    assert br["corrections"] == 1 and br["revised"] == 2 and br["max_rounds"] == 3
    assert br["unresolved"] == 2  # the correction and the question are not referenced by a conclusion
    assert [a["result"] for a in out["agents"]] == ["A: 443", "B revised: port 443", "worker ok"]
    lead = llm.lead_inputs[0]
    assert "BRIDGE DIGEST" in lead and "port is 443" in lead and "Unresolved" in lead
    assert "A: 443" in lead.split("BRIDGE DIGEST")[0]
    posts = for_core(core, "x1").posts()
    kinds = [p["kind"] for p in posts]
    assert kinds[:3] == ["finding"] * 3 and "correction" in kinds and "conclusion" in kinds
    corr = next(p for p in posts if p["kind"] == "correction")
    assert corr["refs"] == [posts[0]["id"]] and corr["agent"] == "Agent2:B"  # unknown ref dropped
    assert {r for _, r in llm.xcalls} == {1, 2} and len(llm.xcalls) == 6
    evs = [e["kind"] for e in core.codex.replay("x1")]
    assert evs.count("bridge.round.done") == 2 and "bridge.post" in evs
    assert evs.index("bridge.round.done") < evs.index("lead.merge")


def test_cross_exam_stops_at_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", "2")
    llm = ScriptLLM(xexam=lambda label, rnd, u: json.dumps(
        {"posts": [{"kind": "correction", "text": f"{label} fix r{rnd}"}]}))
    core = _core(tmp_path, llm)
    out = asyncio.run(core.squad("t", n=3, run_id="x2"))
    assert out["bridge"]["rounds_run"] == 2 and out["bridge"]["stop_reason"] == "cap"
    assert out["bridge"]["corrections"] == 6


def test_cross_exam_skips_crashed_and_survives_turn_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", "1")

    def xexam(label, rnd, u):
        if label == "Agent1:A":
            return RuntimeError("model down")
        assert "worker crashed" not in u  # the crashed worker's note is not shown to others
        return '{"posts": [{"kind": "conclusion", "text": "ok"}]}'
    llm = ScriptLLM(xexam=xexam, boom={"beta"})
    core = _core(tmp_path, llm)
    out = asyncio.run(core.squad("t", n=3, run_id="x3"))
    assert sorted(lbl for lbl, _ in llm.xcalls) == ["Agent1:A", "Agent3:C"]
    assert out["answer"].startswith("LEAD MERGED") and out["bridge"]["stop_reason"] == "converged"
    assert "failed" in out["agents"][1]["result"]
    assert any(e["kind"] == "error" and "cross-exam" in e["data"].get("message", "")
               for e in core.codex.replay("x3"))
    assert [p["agent"] for p in for_core(core, "x3").read(kind="finding")] == ["Agent1:A", "Agent3:C"]


def test_cross_exam_too_few_workers(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", "1")
    llm = ScriptLLM(boom={"beta", "gamma"})
    core = _core(tmp_path, llm)
    out = asyncio.run(core.squad("t", n=3, run_id="x4"))
    assert llm.xcalls == [] and out["bridge"]["stop_reason"] == "too_few_workers"
    assert out["bridge"]["rounds_run"] == 0


def test_parse_revise_junk():
    assert bridge_mod.parse_revise(None) == ([], "")
    assert bridge_mod.parse_revise("{bad json}") == ([], "")
    assert bridge_mod.parse_revise("[1]") == ([], "")
    posts, rev = bridge_mod.parse_revise(json.dumps({"posts": [
        {"kind": "gossip", "text": "x"}, {"kind": "conclusion", "text": " "}, "str",
        {"kind": "Answer", "text": "y", "refs": "abc"}], "revised": 5}))
    assert posts == [{"kind": "answer", "text": "y", "refs": ["abc"]}] and rev == ""


# ------------------------------------------------------------------ recursive learning

def test_lessons_distill_dedupe_evict(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS_MAX", "3")
    b = Bridge("L1", tmp_path)
    b.post("a", "correction", "Nginx default port is 80, not 8080")
    for t in ("Port 443 needs TLS cert", "port 443 needs  TLS cert.", "Use certbot for TLS certs"):
        b.post("a", "conclusion", t)
    saved = bridge_mod.distill_lessons(tmp_path, b, "tls task", k=5)
    assert len(saved) == 3  # the two 443 lines normalize to one
    assert bridge_mod.distill_lessons(tmp_path, b, "again") == []  # deduped across runs
    store = bridge_mod._store(tmp_path)
    vals = {r["value"] for r in store.load("lessons").values()}
    assert "Use certbot for TLS certs" in vals and "Nginx default port is 80, not 8080" in vals
    b2 = Bridge("L2", tmp_path)
    b2.post("b", "conclusion", "Rotate SSH keys yearly")
    b2.post("b", "conclusion", "Disable SSH password auth")
    assert len(bridge_mod.distill_lessons(tmp_path, b2, "ssh", k=1)) == 1
    bridge_mod.distill_lessons(tmp_path, b2, "ssh", k=5)
    live = store.load("lessons")
    assert len(live) == 3  # capped, oldest evicted
    assert "Rotate SSH keys yearly" in {r["value"] for r in live.values()}
    assert all(r.get("run_id") for r in live.values())
    assert bridge_mod.distill_lessons(tmp_path, Bridge("empty", tmp_path), "x") == []


def test_lessons_relevance_and_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    b = Bridge("L", tmp_path)
    b.post("a", "conclusion", "nginx reverse proxy needs proxy_pass to the upstream port")
    b.post("a", "conclusion", "postgres vacuum keeps tables healthy")
    b.post("a", "conclusion", "nginx tls config: ssl_certificate and ssl_certificate_key " + "z" * 300)
    bridge_mod.distill_lessons(tmp_path, b, "", k=5)
    got = bridge_mod.relevant_lessons(tmp_path, "configure nginx tls ssl_certificate")
    assert got[0].startswith("nginx tls config") and not any("postgres" in g for g in got)
    assert len(got) == 2
    assert bridge_mod.relevant_lessons(tmp_path, "the and for") == []
    small = bridge_mod.relevant_lessons(tmp_path, "nginx tls ssl_certificate proxy", max_chars=100)
    assert small == ["nginx reverse proxy needs proxy_pass to the upstream port"]
    block = bridge_mod.lessons_block(tmp_path, "nginx proxy")
    assert block.startswith("\n\nLESSONS FROM EARLIER RUNS") and len(block) < 2200


def test_lessons_off_and_env(tmp_path, monkeypatch):
    b = Bridge("L", tmp_path)
    b.post("a", "conclusion", "nginx needs a reload after config edits")
    bridge_mod.distill_lessons(tmp_path, b, "", k=5)
    monkeypatch.setenv("AVRONE_LESSONS", "off")
    assert bridge_mod.lessons_block(tmp_path, "nginx reload") == ""
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    assert "reload" in bridge_mod.lessons_block(tmp_path, "nginx reload")
    monkeypatch.setenv("AVRONE_LESSONS_K", "99")
    assert bridge_mod.lessons_k() == 20
    monkeypatch.delenv("AVRONE_LESSONS_MAX", raising=False)
    assert bridge_mod.lessons_max() == 200


def test_recursive_learning_across_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_BRIDGE_ROUNDS", "1")
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    llm = ScriptLLM(xexam=lambda label, rnd, u: json.dumps(
        {"posts": [{"kind": "conclusion", "text": "redis default port is 6379"}]}))
    core = _core(tmp_path, llm)
    out1 = asyncio.run(core.squad("find the redis port", n=3, run_id="run1"))
    assert out1["lessons_learned"] == 1  # three identical conclusions -> one lesson
    assert not any("LESSONS FROM EARLIER RUNS" in s for s in llm.worker_systems)
    llm.worker_systems.clear()
    out2 = asyncio.run(core.squad("is redis on its default port?", n=3, run_id="run2"))
    assert len(llm.worker_systems) == 3
    assert all("LESSONS FROM EARLIER RUNS" in s and "6379" in s for s in llm.worker_systems)
    assert "lessons_learned" not in out2  # nothing new
    assert out2["agents"][0]["focus"] == "alpha"  # reported focus stays the planner's
    assert any(e["kind"] == "lessons.inject" for e in core.codex.replay("run2"))
    # unrelated task: nothing injected
    llm.worker_systems.clear()
    asyncio.run(core.squad("bake sourdough bread", n=3, run_id="run3"))
    assert not any("LESSONS FROM EARLIER RUNS" in s for s in llm.worker_systems)


def test_deep_reviewers_get_lessons(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    b = Bridge("old", tmp_path / "d")
    b.post("a", "conclusion", "kubernetes pods restart on OOMKilled")
    bridge_mod.distill_lessons(tmp_path / "d", b, "", k=5)
    seen = []

    class L:
        async def chat(self, messages, **kw):
            sysm = messages[0]["content"]
            if "DEEP LEAD" in sysm:
                return "## Verdict\nConfirmed. Confidence: High\n## Corrections\nNone\n## Bottom line\nok"
            seen.append(sysm)
            return '{"final": "fine"}'
    core = _core(tmp_path, L())
    out = asyncio.run(core.deep("why do kubernetes pods restart?", first_answer="OOM", n=2, run_id="dp"))
    assert out["converged"] and len(seen) == 2 and all("OOMKilled" in s for s in seen)
    assert out["agents"][0]["focus"].startswith("Verify each factual claim")


def test_distill_failure_never_sinks_run(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    monkeypatch.setattr(bridge_mod, "distill_lessons", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    monkeypatch.setattr(bridge_mod, "lessons_block", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    core = _core(tmp_path, ScriptLLM())
    out = asyncio.run(core.squad("t", n=3, run_id="f1"))
    assert out["answer"].startswith("LEAD MERGED") and "lessons_learned" not in out
    assert any("lessons distill failed" in e["data"].get("message", "")
               for e in core.codex.replay("f1") if e["kind"] == "error")


def test_bridge_run_id_wave_suffix_is_removed_once_and_only_at_end():
    assert bridge_mod.bridge_run_id("run-wave1-wave1") == "run-wave1"
    assert bridge_mod.bridge_run_id("run-wave10") == "run-wave10"
    assert bridge_mod.bridge_run_id("run-wave1-extra") == "run-wave1-extra"
    assert bridge_mod.bridge_run_id("") == "run"
    assert bridge_mod.bridge_run_id(None) == "run"
