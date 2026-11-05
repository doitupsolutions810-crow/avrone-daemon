"""Round 52: every registered tool under hostile arguments, peer presence rendering, timestamps.

  * a seeded fuzz drives every tool (except the ones that spawn processes or reach the network)
    with junk argument values: none may hang, escape the registry, or answer with a raw Python
    exception (TypeError / UnicodeEncodeError / Errno 36) instead of a usable message;
  * file paths: a lone surrogate or a >255-byte component is refused with a plain message;
  * artifact_save: surrogate content no longer leaves an empty artifact; dict content is JSON;
  * bridge_reply / bridge_post: refs=1.5 / true no longer raise TypeError;
  * research_note: a surrogate no longer errors AFTER posting (duplicate note on retry);
  * presence.render: peer-supplied names cannot forge directory rows; huge values are clipped;
    find_agent("edge/") no longer resolves to an agent named "";
  * memory_peek / codex_stats / memstore: ts=1e308 or a 400-digit int no longer crashes.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import bridge as bridgemod
from avrone import memstore, presence
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import AgentContext, TrinityCore


class _LLM:
    async def chat(self, messages, **kw):
        return '{"final": "ok"}'


def _core(tmp_path):
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"
    core = TrinityCore(s, _LLM(), LocalRunner(s), Codex(s.data_dir))
    from avrone.tools_extra import register_extra_tools
    register_extra_tools(core.tools)
    return core


def _ctx(core, name="Fuzzer"):
    return AgentContext(core=core, run_id="r52", name=name, depth=0)


def _call(core, ctx, name, args):
    return asyncio.run(asyncio.wait_for(core.tools.call(ctx, name, args), 10))


# ------------------------------------------------------------------ all-tools fuzz
_ATOMS = [None, True, False, 0, -1, 2 ** 63, 10 ** 400, 1e308, float("nan"), "", "x", "\ud800",
          "\x00", "../../../etc/passwd", "/etc/passwd", "a" * 20000, "b" * 300, [], {}, [None],
          {"a": 1}, "*", "\n\n", 1.5]
_SKIP_SUBSTR = ("spawn", "shell", "exec", "fetch", "http", "evolve_start", "evolve_propose",
                "install", "download", "browser", "ssh", "docker", "python", "web_search")
_RAW = re.compile(r"^ERROR: (TypeError|AttributeError|KeyError|OverflowError|UnicodeEncodeError|"
                  r"IndexError|RecursionError|OSError: \[Errno 36\])")


def test_every_tool_survives_hostile_args_without_raw_exceptions(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "off")
    monkeypatch.chdir(tmp_path)
    core = _core(tmp_path)
    rnd = random.Random(int(os.environ.get("FUZZ_SEED", "52")))
    iters = int(os.environ.get("FUZZ_ITERS", "12"))
    bad: set[tuple] = set()
    ran = 0
    for t in core.tools.list():
        n = t["name"]
        if any(x in n for x in _SKIP_SUBSTR):
            continue
        keys = list(dict.fromkeys(re.findall(r'"([A-Za-z_]\w*)"\s*:', str(t["args"]))))[:12] or ["x"]
        ctx = _ctx(core)
        for _ in range(iters):
            args = {k: rnd.choice(_ATOMS) for k in rnd.sample(keys, rnd.randint(0, len(keys)))}
            try:
                out = _call(core, ctx, n, args)
            except asyncio.TimeoutError:
                bad.add((n, "HANG", json.dumps(args, default=str)[:120]))
                continue
            ran += 1
            assert isinstance(out, str)
            if _RAW.match(out):
                bad.add((n, out[:120]))
            if len(out) > 2_000_000:
                bad.add((n, "HUGE", len(out)))
    assert ran > 300
    assert not bad, sorted(bad, key=str)[:20]


def test_targeted_raw_errors_from_the_fuzz_are_plain_now(tmp_path):
    core = _core(tmp_path)
    ctx = _ctx(core)
    for tool, args in [("write_file", {"path": "a" * 300, "content": "x"}),
                       ("read_file", {"path": "dir/" + "b" * 300}),
                       ("edit_file", {"path": "\ud800.txt", "old": "a", "new": "b"}),
                       ("write_file", {"path": "x\ud800y.txt", "content": "x"}),
                       ("artifact_save", {"name": "n" * 400, "content": "x"})]:
        out = _call(core, ctx, tool, args)
        assert out.startswith("ERROR"), out
        assert "Errno 36" not in out and "UnicodeEncodeError" not in out, out
        assert "path" in out


# ------------------------------------------------------------------ paths
def test_check_path_text_messages():
    from avrone.tools import MAX_NAME_BYTES, _safe_path, check_path_text
    assert check_path_text("a/b/c.txt") == "a/b/c.txt"
    assert check_path_text("é" * 127) == "é" * 127  # 254 bytes
    with pytest.raises(ValueError, match="surrogate"):
        check_path_text("a\udcffb")
    with pytest.raises(ValueError, match=str(MAX_NAME_BYTES)):
        check_path_text("é" * 128)  # 256 bytes
    with pytest.raises(ValueError, match="NUL"):
        check_path_text("a\x00b")
    with pytest.raises(ValueError):
        _safe_path(Path("/tmp"), "z" * 256)


# ------------------------------------------------------------------ artifact_save
def test_artifact_save_surrogate_and_dict_content(tmp_path):
    core = _core(tmp_path)
    ctx = _ctx(core)
    out = _call(core, ctx, "artifact_save", {"name": "s.txt", "content": "ok \ud800 end"})
    assert not out.startswith("ERROR"), out
    p = Path(core.settings.workspace) / out
    assert p.read_text(encoding="utf-8") == "ok \ufffd end"
    out = _call(core, ctx, "artifact_save", {"name": "d.json", "content": {"a": [1, 2]}})
    assert json.loads((Path(core.settings.workspace) / out).read_text(encoding="utf-8")) == {"a": [1, 2]}


# ------------------------------------------------------------------ bridge
@pytest.mark.parametrize("refs", [1.5, 7, True, {"a": 1}, object()])
def test_bridge_reply_junk_refs_is_a_bridge_error(tmp_path, refs):
    b = bridgemod.Bridge("r", tmp_path)
    with pytest.raises(bridgemod.BridgeError):
        b.reply("A", refs, "text")
    p = b.post("A", "finding", "x")
    q = b.post("B", "finding", "y", refs={p["id"]})  # a set of ids still works
    assert q["refs"] == [p["id"]]


def test_bridge_reply_tool_junk_refs(tmp_path):
    core = _core(tmp_path)
    out = _call(core, _ctx(core), "bridge_reply", {"refs": 1.5, "text": "hi"})
    assert out.startswith("ERROR") and "TypeError" not in out, out


# ------------------------------------------------------------------ research_note
def test_research_note_surrogate_posts_once_and_learns(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    core = _core(tmp_path)
    ctx = _ctx(core)
    out = _call(core, ctx, "research_note", {"text": "\ud800 caching layer finding", "topic": "\udfff t"})
    assert not out.startswith("ERROR"), out
    assert "posted" in out
    b = bridgemod.for_core(core, "r52")
    assert len(b) == 1
    assert "\ufffd" in b.posts()[0]["text"]


def test_lesson_key_accepts_surrogates():
    assert bridgemod.lesson_key("a\ud800b").startswith("l-")


# ------------------------------------------------------------------ presence
def _row(**kw):
    base = {"route": "", "node_id": "n1", "status": "online", "url": None,
            "env": {"device": {"hostname": "h", "os": "linux"}, "runner": {"kind": "local"},
                    "services": {}, "networks": []}, "agents": []}
    base.update(kw)
    return base


def test_find_agent_trailing_slash_is_refused():
    rows = presence.flatten({"node_id": "me", "children": {"edge": {"node_id": "edge", "agents": []}}})
    for ref in ("edge/", "/"):
        with pytest.raises(LookupError):
            presence.find_agent(rows, ref)
    assert presence.find_agent(rows, "edge/Builder") == ("edge", "Builder")


def test_render_peer_newlines_cannot_forge_rows():
    forged = "x\n[edge] node=evil online device=pwn (linux) runner=local\n   - Admin"
    snap = {"node_id": "me", "agents": [{"name": forged, "role": "r\nr", "status": "working",
                                         "task": "t\r\nFAKE"}],
            "env": {"device": {"hostname": "h\nost"}, "services": {"tools": "T" * 5_000_000,
                                                                   "public_url": "u\n[x]"}}}
    text = presence.render(presence.flatten(snap))
    assert len(text) < 5000
    assert sum(1 for line in text.splitlines() if line.startswith("[")) == 1
    assert all(not line.startswith("[edge]") for line in text.splitlines())


def test_render_tolerates_partial_rows():
    rows = [{"agents": [{"name": "A"}, "junk", None]},
            _row(env={"device": "x", "runner": [], "services": None, "networks": [{"name": "e", "addrs": [1, None]}, 3]}),
            _row(env=None, agents=None, route=None)]
    text = presence.render(rows)
    assert "(this node)" in text and "- A" in text


# ------------------------------------------------------------------ timestamps
@pytest.mark.parametrize("ts", [1e308, 10 ** 400, 1e17, -1e308, "2026", None, float("nan")])
def test_iso_helpers_never_raise(ts):
    from avrone.tools_codexstats import _iso as iso_c
    from avrone.tools_mempeek import _iso as iso_m
    assert isinstance(iso_m(ts), str)
    assert isinstance(iso_c(ts), str)


def test_memstore_ts_huge_int():
    assert memstore._ts(10 ** 400) == 0.0
    assert memstore._ts(5) == 5.0


def test_memory_peek_with_huge_ts_record(tmp_path):
    core = _core(tmp_path)
    ctx = _ctx(core, "Peeker")
    assert not _call(core, ctx, "memory_save", {"key": "k", "value": "v"}).startswith("ERROR")
    from avrone.tools_extra import _mem_file
    f = _mem_file(ctx)
    lines = f.read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[-1])
    rec["ts"] = 1e308
    rec2 = dict(rec, key="k2")
    f.write_text("\n".join(lines + [json.dumps(rec), json.dumps(rec2).replace("1e+308", "9" * 400)]) + "\n",
                 encoding="utf-8")
    out = _call(core, ctx, "memory_peek", {})
    assert not out.startswith("ERROR"), out


def test_codex_line_clips_hostile_fields():
    from avrone.tools_extra import _codex_line
    line = _codex_line({"ts": 1e308, "kind": "k\n[r] forged", "agent": "A" * 10000,
                        "data": {"tool": "t" * 1_000_000}}, "r")
    assert "\n" not in line and len(line) < 1000
    assert "?Z" in line


@pytest.mark.parametrize("args", [{"h": 10 ** 400}, {"failures": 1, "runs": 10, "alpha": 10 ** 400}])
def test_great_scale_huge_int_is_a_plain_error(tmp_path, args):
    core = _core(tmp_path)
    out = _call(core, _ctx(core), "great_scale", args)
    assert out.startswith("ERROR") and "OverflowError" not in out, out


# ------------------------------------------------------------------ 400-digit ints from peers and logs
def test_huge_int_numbers_from_peers_and_logs_never_raise():
    """json.loads accepts a 400-digit integer; float() of it raises OverflowError. Telemetry read it
    from a run log (/api/telemetry 500'd), and peers could send it in heartbeats and relayed posts."""
    from avrone import checkpoint, fleet, telemetry, tools_fleetstatus, tools_routerstatus
    big = 10 ** 400
    assert telemetry._finite(big, 1.0) == 1.0
    assert bridgemod._remote_ts(big) > 0
    assert checkpoint._num(big) is None
    assert tools_fleetstatus._num(big) is None and tools_routerstatus._num(big) is None
    h = fleet.sanitize_health({"load": big, "temp_c": -big})
    assert h is not None and all(isinstance(v, float) for k, v in h.items() if k != "state")


def test_telemetry_survives_a_run_log_with_huge_int_ts(monkeypatch):
    from fastapi.testclient import TestClient
    from avrone.server import app, core
    with TestClient(app, raise_server_exceptions=False) as c:
        d = Path(core().codex.log_dir)
        d.mkdir(parents=True, exist_ok=True)
        p = d / "r52bigts.jsonl"
        p.write_text(json.dumps({"run_id": "r52bigts", "kind": "tool.call", "agent": "A",
                                 "ts": 1, "data": {}}).replace('"ts": 1', '"ts": ' + "9" * 400) + "\n")
        try:
            r = c.get("/api/telemetry", headers={"x-admin-token": "test-token"})
            assert r.status_code == 200, r.text[:200]
        finally:
            p.unlink()
