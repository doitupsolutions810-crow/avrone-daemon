"""Round 9 coverage: defensive branches in bridge, tools_agenthome, presence, research_note,
lesson_recall (each test asserts the behavior the branch promises, not just that it runs)."""
import asyncio
import json
import os
import socket
import subprocess
from types import SimpleNamespace

import pytest

from avrone import bridge as bm
from avrone import presence
from avrone import tools_agenthome as ah
from avrone import tools_lessonrecall as lr
from avrone import tools_researchnote as rn


# ------------------------------------------------------------------ bridge
def test_refs_capped():
    assert len(bm._refs([f"id{i}" for i in range(50)])) == bm.MAX_REFS


def test_load_oserror_leaves_empty(tmp_path, monkeypatch):
    b = bm.Bridge("r", tmp_path)
    b.post("a", "finding", "x")
    # round 84: the journal is read through bridge._read_journal (regular files only)
    monkeypatch.setattr(bm, "_read_journal", lambda *a, **k: (_ for _ in ()).throw(OSError("eio")))
    assert len(bm.Bridge("r", tmp_path)) == 0


@pytest.mark.parametrize("line", ['{"tombstones": [bad', '{"tombstones": "x"}', '{"tombstones": [], "id": 1}'])
def test_bad_tombstone_lines_ignored(line):
    assert bm.Bridge._parse_tombstones(line) is None


def test_tombstone_cap(tmp_path):
    b = bm.Bridge("r", tmp_path, limit=1)
    for i in range(80):
        b.post("a", "finding", f"p{i}")
    assert len(b._evicted) == 64 and len(b) == 1


def test_write_oserror_keeps_memory(tmp_path, monkeypatch):
    b = bm.Bridge("r", tmp_path)
    real = type(b.path).open
    monkeypatch.setattr(type(b.path), "open", lambda self, *a, **k: (_ for _ in ()).throw(OSError("ro")))
    p = b.post("a", "finding", "still here")
    assert b.get(p["id"])["text"] == "still here"
    monkeypatch.setattr(type(b.path), "open", real)


class BoomCodex:
    def emit(self, *a, **k):
        raise RuntimeError("audit down")


def test_codex_failures_never_sink_post_or_merge(tmp_path):
    b = bm.Bridge("r", tmp_path, codex=BoomCodex())
    assert b.post("a", "finding", "ok")["text"] == "ok"
    assert b.merge_remote([{"id": "rem1", "kind": "finding", "text": "t"}], "n")["merged"] == 1


def test_for_core_creates_cache():
    core = SimpleNamespace(settings=SimpleNamespace(data_dir=None), codex=None)
    b = bm.for_core(core, "rx")
    assert core._bridges and bm.for_core(core, "rx") is b


def test_relevant_lessons_oserror(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_store", lambda d: (_ for _ in ()).throw(OSError("x")))
    assert bm.relevant_lessons(tmp_path, "anything here") == []


def test_lessons_block_swallows(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    monkeypatch.setattr(bm, "relevant_lessons", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    assert bm.lessons_block(tmp_path, "task words") == ""


def test_parse_revise_non_dict_and_cap():
    assert bm.parse_revise('```json\n[1, 2]\n```') == ([], "")
    many = {"posts": [{"kind": "finding", "text": f"t{i}"} for i in range(20)], "revised": "r"}
    posts, _ = bm.parse_revise(json.dumps(many))
    assert len(posts) == 10


# ------------------------------------------------------------------ tools_agenthome
def test_clean_values():
    assert ah._clean(True) is True and ah._clean(None) is None
    assert ah._clean(float("nan")) is None and ah._clean(3) == 3
    assert ah._clean("a\x00b") == "a?b" and ah._clean("x" * 100).endswith("...")


def test_scan_home_special_and_lstat_error(tmp_path, monkeypatch):
    os.mkfifo(tmp_path / "pipe")
    (tmp_path / "f").write_text("abc")
    out = ah.scan_home(tmp_path)
    assert out["special"] == 1 and out["files"] == 1 and out["bytes"] == 3
    real = os.lstat
    monkeypatch.setattr(ah.os, "lstat", lambda p, *a, **k: (_ for _ in ()).throw(OSError("gone"))
                        if str(p).endswith("f") else real(p, *a, **k))
    assert ah.scan_home(tmp_path)["files"] == 0


def test_container_odd_clock(monkeypatch):
    aid, node = "a1", "n1"
    name = ah.agentcontainer.container_name(node, aid)
    pool = SimpleNamespace(node=node, homes={name: SimpleNamespace(last_used="later")}, clock=lambda: 5)
    ctx = SimpleNamespace(core=SimpleNamespace(runner=SimpleNamespace(kind="docker", agent_pool=pool)))
    out = ah._container(ctx, aid, node)
    assert out.get("live") is None or out["live"].get("idle_s") is None


def test_packs_scan_cap_and_oserror(tmp_path, monkeypatch):
    d = ah.agentpack.pack_dir(tmp_path)
    d.mkdir(parents=True)
    for i in range(5):
        (d / f"a1-{1000 + i}.tar.gz").write_bytes(b"x")
    monkeypatch.setattr(ah, "PACKS_MAX_SCAN", 2)
    assert ah._packs(tmp_path, "a1")["count"] <= 2
    monkeypatch.setattr(ah.os, "scandir", lambda p: (_ for _ in ()).throw(OSError("x")))
    assert ah._packs(tmp_path, "a1") == {"count": 0, "newest": None}


# ------------------------------------------------------------------ presence
def test_host_interfaces_fallbacks(monkeypatch):
    monkeypatch.setattr(presence.shutil, "which", lambda n: "/sbin/ip")
    monkeypatch.setattr(presence.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.SubprocessError()))
    monkeypatch.setattr(presence.socket, "getaddrinfo", lambda *a: (_ for _ in ()).throw(OSError()))

    class S:
        def __init__(self, *a):
            pass

        def connect(self, a):
            raise OSError("no route")

    monkeypatch.setattr(presence.socket, "socket", S)
    out = presence._host_interfaces()
    assert isinstance(out, list)
    assert all(a for i in out for a in i.get("addrs", []))


# ------------------------------------------------------------------ research_note / lesson_recall
def test_learn_oserror(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "_store", lambda d: (_ for _ in ()).throw(OSError("disk")))
    status, key = rn.learn(tmp_path, "some text", "r")
    assert status == "refused: OSError" and key


def test_research_note_bridge_error(tmp_path, monkeypatch):
    class B:
        run_id = "r"

        def post(self, *a, **k):
            raise bm.BridgeError("nope")
    monkeypatch.setattr(bm, "for_core", lambda core, rid: B())
    ctx = SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path))), run_id="r", name="w")
    assert asyncio.run(rn.t_research_note(ctx, {"text": "x"})) == "ERROR: nope"


def test_lesson_recall_read_error_and_bad_ts(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_LESSONS", "on")
    ctx = SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=str(tmp_path))))
    bm._store(tmp_path).save(bm.LESSONS_NS, "k1", "a lesson")
    real = bm._store

    class St:
        def load(self, ns):
            return {"k1": {"key": "k1", "value": "a lesson", "ts": "bad"}}
    monkeypatch.setattr(bm, "_store", lambda d: St())
    assert "[? ago]" in asyncio.run(lr.t_lesson_recall(ctx, {}))
    monkeypatch.setattr(bm, "_store", lambda d: (_ for _ in ()).throw(OSError()))
    assert "could not read" in asyncio.run(lr.t_lesson_recall(ctx, {}))
    monkeypatch.setattr(bm, "_store", real)
