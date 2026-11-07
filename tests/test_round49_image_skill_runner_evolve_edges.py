"""Round 49: image tools, skills loader, shell runner timeouts, evolve listings.

  * image_inspect / image_decode: a FIFO path hung the event loop inside Image.open; non-image,
    empty, truncated, directory and missing paths raised raw exceptions; a 48 KB PNG declaring
    400 Mpx raised DecompressionBombError; big images were decoded on the event loop;
  * skills: a file that grew after stat() was read whole; 100k skill files were all read and listed;
  * runner: timeout 10**400 / "5" raised AFTER the child was spawned (child + readers leaked),
    inf never timed out, NaN/negative killed at once as a TIMEOUT; CommandResult.text(-10) uncut;
  * evolve: deeply nested JSON (RecursionError), iterations as a dict or holding ints, a str ts,
    a huge owner_pid and an UnboundLocalError in history writing broke evolve_list/evolve_status;
    loop_limits(10**400) raised; 100k edits were accepted.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import agentkit, evolve
from avrone import sovereign
from avrone.sovereign import CommandResult, LocalRunner, exit_label

MAX_EDITS = getattr(evolve, "MAX_EDITS", 200)
MAX_EDITS_BYTES = getattr(evolve, "MAX_EDITS_BYTES", 16 * 1024 * 1024)


def run(c):
    return asyncio.run(c)


def _kctx(tmp: Path):
    ws = tmp / "ws"
    ws.mkdir(exist_ok=True)
    core = SimpleNamespace(runner=SimpleNamespace(workspace=ws, kind="local"),
                           settings=SimpleNamespace(data_dir=tmp / "d"), codex=SimpleNamespace(emit=lambda *a, **k: None))
    return SimpleNamespace(core=core, run_id="r1", name="Agent-1", depth=0)


# ------------------------------------------------------------------ images
@pytest.fixture(scope="module")
def images(tmp_path_factory):
    from PIL import Image
    c = _kctx(tmp_path_factory.mktemp("img"))
    ws = c.core.runner.workspace
    (ws / "notimg.png").write_bytes(b"hello, not a png")
    (ws / "empty.png").write_bytes(b"")
    Image.new("1", (20000, 20000)).save(ws / "bomb.png")         # 400 Mpx in ~48 KB
    Image.new("1", (8000, 6000)).save(ws / "over_cap.png")        # 48 Mpx: under PIL's limit, over ours
    im = Image.new("RGB", (64, 64), "red")
    im.save(ws / "trunc.png")
    raw = (ws / "trunc.png").read_bytes()
    (ws / "trunc.png").write_bytes(raw[:len(raw) // 2])
    (ws / "dir.png").mkdir()
    Image.new("RGB", (1, 1), "white").save(ws / "one.png")
    if hasattr(os, "mkfifo"):
        os.mkfifo(ws / "fifo.png")
    return c


BAD = ["notimg.png", "empty.png", "bomb.png", "over_cap.png", "trunc.png", "dir.png", "missing.png", "../escape.png",
       "fifo.png", "", None, 5, "bad\x00name.png"]


@pytest.mark.parametrize("name", BAD)
@pytest.mark.parametrize("tool", ["inspect", "decode"])
def test_bad_images_are_tool_errors(images, name, tool, monkeypatch):
    if name == "fifo.png" and not hasattr(os, "mkfifo"):
        pytest.skip("no FIFOs")
    if tool == "decode":
        import sys
        import types
        monkeypatch.setitem(sys.modules, "zxingcpp", types.SimpleNamespace(read_barcodes=lambda *a, **k: []))
    fn = agentkit.t_image_inspect if tool == "inspect" else agentkit.t_image_decode
    t0 = time.monotonic()
    out = run(asyncio.wait_for(fn(images, {"path": name}), 10))
    assert out.startswith("ERROR"), out
    assert time.monotonic() - t0 < 10


def test_one_pixel_and_small_images_still_work(images):
    out = run(agentkit.t_image_inspect(images, {"path": "one.png"}))
    assert out.startswith("one.png: 1x1") and "entropy=0.000" in out


def test_decode_skips_upscale_for_big_images(images, monkeypatch):
    import sys
    import types
    from PIL import Image
    sizes = []
    monkeypatch.setitem(sys.modules, "zxingcpp",
                        types.SimpleNamespace(read_barcodes=lambda img, **k: sizes.append(img.size) or []))
    Image.new("L", (3000, 3000)).save(images.core.runner.workspace / "big.png")
    assert "no symbols" in run(agentkit.t_image_decode(images, {"path": "big.png"}))
    assert max(w * h for w, h in sizes) <= 3000 * 3000


def test_inspect_runs_off_the_event_loop(images, monkeypatch):
    from PIL import Image
    Image.new("RGB", (200, 200)).save(images.core.runner.workspace / "slow.png")
    real = agentkit._inspect
    monkeypatch.setattr(agentkit, "_inspect", lambda p, im: (time.sleep(0.6), real(p, im))[1])

    async def go():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1
        t = asyncio.ensure_future(ticker())
        out = await agentkit.t_image_inspect(images, {"path": "slow.png"})
        t.cancel()
        return out, ticks
    out, ticks = run(go())
    assert out.startswith("slow.png") and ticks >= 5


# ------------------------------------------------------------------ skills
def test_skill_growing_after_stat_is_not_read_whole(tmp_path, monkeypatch):
    f = tmp_path / "s.md"
    f.write_text("x" * 10)
    real_open = Path.open
    done = []

    def grown(self, *a, **k):
        if self == f and not done:
            done.append(1)
            with real_open(f, "wb") as fh:
                fh.write(b"y" * (agentkit.SKILL_MAX_BYTES + 5000))
        return real_open(self, *a, **k)
    monkeypatch.setattr(Path, "open", grown)
    from avrone import safeio  # round 99: _read_skill opens one fd via safeio.open_read
    real_or = safeio.open_read

    def grown_or(path, follow=False):
        fh = real_or(path, follow)
        grown(Path(path), "rb").close()
        return fh
    monkeypatch.setattr(safeio, "open_read", grown_or)
    assert agentkit._read_skill(f) is None


def test_many_skills_are_capped(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    d.mkdir()
    for i in range(agentkit.MAX_SKILLS + 300):
        (d / f"s{i:05d}.md").write_text(f"skill {i}")
    monkeypatch.setenv("AVRONE_SKILLS_DIR", str(d))
    c = _kctx(tmp_path)
    listing = run(agentkit.t_skill_list(c, {}))
    assert listing.count("\n") == agentkit.MAX_SKILLS - 1
    err = run(agentkit.t_skill_load(c, {"name": "nope"}))
    assert err.startswith("ERROR") and "more; skill_list" in err and len(err) < 2000


# ------------------------------------------------------------------ runner
@pytest.mark.parametrize("t,expect", [(10**400, 86400), ("5", 7), (float("inf"), 86400), (float("nan"), 7),
                                      (-5, 7), (0, 7), (True, 7), (None, 7), (2.5, 2.5), (5, 5), (10**6, 86400)])
def test_norm_timeout(t, expect):
    assert sovereign._norm_timeout(t, 7) == expect


@pytest.mark.parametrize("t", [10**400, "5", float("nan"), -5, -float("inf"), True, [3]])
def test_runner_bad_timeouts_run_the_command(tmp_path, t):
    r = LocalRunner(SimpleNamespace(workspace=tmp_path, command_timeout=5))
    res = run(r.run("echo hi", timeout=t))
    assert res.exit_code == 0 and res.stdout == "hi\n" and not res.timed_out


def test_runner_bad_default_timeout(tmp_path):
    r = LocalRunner(SimpleNamespace(workspace=tmp_path, command_timeout="junk"))
    assert run(r.run("echo ok")).stdout == "ok\n"


@pytest.mark.parametrize("lim", [-10, -1, 0])
def test_text_negative_limit_is_cut(lim):
    cr = CommandResult("x", 0, "o" * 10000, "e" * 10000)
    assert len(cr.text(lim)) < 100


@pytest.mark.parametrize("code", [None, "x", 1.5, True])
def test_exit_label_junk_code(code):
    assert exit_label(code).startswith("exit")


# ------------------------------------------------------------------ evolve
def _ectx(tmp: Path):
    return SimpleNamespace(core=SimpleNamespace(settings=SimpleNamespace(data_dir=tmp),
                                                codex=SimpleNamespace(emit=lambda *a, **k: None)), name="A", run_id="r")


def test_hostile_evolve_files_never_break_listings(tmp_path):
    c = _ectx(tmp_path)
    d = tmp_path / "evolve"
    loops = d / "loops"
    loops.mkdir(parents=True)
    (d / "deep.json").write_text("[" * 100_000)
    (d / "m1.json").write_text(json.dumps({"pid": "p1", "passed": True, "goal": 5, "files": [1, None, {"a": 1}],
                                           "by": None}))
    (d / "m2.json").write_text(json.dumps({"pid": "p2", "passed": "yes", "goal": "g", "files": ["a"], "by": "x",
                                           "duration_s": float("inf") if False else 1e308, "loop_id": "L"}))
    (loops / "deep.json").write_text("[" * 100_000)
    (loops / "a.json").write_text(json.dumps({"loop_id": "a", "goal": 7, "iterations": {"x": 1}, "duration_s": "abc",
                                              "time_budget_s": None, "ts": "later"}))
    (loops / "b.json").write_text(json.dumps({"loop_id": "b", "goal": "g", "ts": 5, "status": "running",
                                              "owner_pid": "abc", "accepted": "p9",
                                              "iterations": [1, "x", None, {"outcome": 5, "iteration": 1}]}))
    (loops / "c.json").write_text(json.dumps({"loop_id": ["x"], "status": "running", "owner_pid": 10**30, "ts": 1}))
    (loops / "e.json").write_text(json.dumps({"loop_id": "e", "status": "running", "owner_pid": 10**30, "ts": 2}))
    (loops / "history.jsonl").write_text("[" * 50_000 + "\n{\"loop_id\": 1}\n")
    listing = run(evolve.t_evolve_list(c, {}))
    assert "p1" in listing and "p2" in listing and "loops:" in listing
    for lid in ["", "a", "b", "e"]:
        out = run(evolve.t_evolve_status(c, {"loop_id": lid} if lid else {}))
        assert isinstance(out, str) and not out.startswith("Traceback")
    ids = [lp["loop_id"] for lp in evolve.list_loops(c.core.settings)]
    assert set(ids) == {"a", "b", "e"}
    assert evolve.evolve_status(c, "b")["accepted_count"] == 0


def test_history_write_error_is_swallowed(tmp_path):
    p = tmp_path / "nodir" / "f"
    (tmp_path / "nodir").write_text("a file, so mkdir fails")
    evolve._record_history_at(p, {"loop_id": "x"})  # used to raise UnboundLocalError


@pytest.mark.parametrize("v", [10**400, -10**400, float("inf"), "1e999", True, [1], None])
def test_loop_limits_junk(v):
    n, b = evolve.loop_limits(v, v)
    assert 1 <= n <= evolve.hard_caps()[0] and 1 <= b <= evolve.hard_caps()[1]


@pytest.mark.parametrize("edits", [[{"path": f"f{i}", "content": "x"} for i in range(MAX_EDITS + 1)],
                                   [{"path": "big", "content": "x" * (MAX_EDITS_BYTES + 1)}],
                                   [{"path": "a", "content": {"k": "x" * (MAX_EDITS_BYTES + 1)}}]])
def test_oversized_proposals_refused_before_cloning(tmp_path, edits, monkeypatch):
    c = _ectx(tmp_path)

    async def no_clone(*a, **k):
        raise AssertionError("cloned")
    monkeypatch.setattr(evolve, "_ensure_worktree", no_clone)
    r = run(evolve._propose_once(c, "g", edits))
    assert r["error"] and r["passed"] is False
