"""Offline edge tests for avrone.agentkit: image tools (with a fake zxingcpp), skill discovery
boundaries, env_install error branches, resource ledger and task board edge cases."""
import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from avrone import agentkit
from avrone.sovereign import CommandResult

Image = pytest.importorskip("PIL.Image")


class Runner:
    def __init__(self, ws, codes=(0, 0)):
        self.workspace, self.kind, self.cmds = ws, "local", []
        self.codes = list(codes)

    async def run(self, cmd, timeout=None):
        self.cmds.append(cmd)
        code = self.codes.pop(0) if self.codes else 0
        return CommandResult(cmd, code, "out" if code == 0 else "", "" if code == 0 else "E: boom")


def make_ctx(tmp, codes=(0, 0), emit=None, run_id="r1"):
    ws = tmp / "ws"
    ws.mkdir(exist_ok=True)
    events = []
    core = SimpleNamespace(runner=Runner(ws, codes), settings=SimpleNamespace(data_dir=tmp / "d"),
                           codex=SimpleNamespace(emit=emit or (lambda *a, **k: events.append((a, k)))))
    return SimpleNamespace(core=core, run_id=run_id, name="Agent-1", depth=0), events


def run(c):
    return asyncio.run(c)


# ---------------------------------------------------------------- _emit / paths
def test_emit_swallows_codex_errors(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("codex down")
    c, _ = make_ctx(tmp_path, emit=boom)
    agentkit._emit(c, "x", a=1)  # must not raise
    out = run(agentkit.t_task_board(c, {"op": "add", "title": "t"}))
    assert "#1 [todo] t" in out


def test_img_path_absolute_allowed_and_disabled(tmp_path, monkeypatch):
    c, _ = make_ctx(tmp_path)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    monkeypatch.setenv("AVRONE_ALLOW_ABS_READ", "1")
    assert agentkit._img_path(c, str(outside)) == outside
    monkeypatch.setenv("AVRONE_ALLOW_ABS_READ", "0")
    # absolute paths are re-rooted inside the workspace when abs reads are off
    assert agentkit._img_path(c, str(outside)).is_relative_to(c.core.runner.workspace.resolve())
    with pytest.raises(ValueError):
        agentkit._img_path(c, "../../etc/passwd")


# ---------------------------------------------------------------- image_inspect
def _checker(path, size=64, cell=8):
    im = Image.new("RGB", (size, size), "white")
    px = im.load()
    for y in range(size):
        for x in range(size):
            if (x // cell + y // cell) % 2:
                px[x, y] = (0, 0, 0)
    im.save(path)


def test_image_inspect_checkerboard_grid(tmp_path):
    c, _ = make_ctx(tmp_path)
    _checker(c.core.runner.workspace / "c.png")
    out = run(agentkit.t_image_inspect(c, {"path": "c.png"}))
    assert "c.png: 64x64 RGB format=PNG" in out
    assert "~8x8 modules (unit 8px)" in out and "dark_ratio=0.500" in out and "entropy=1.000" in out


def test_image_inspect_flat_image_no_grid(tmp_path):
    c, _ = make_ctx(tmp_path)
    Image.new("L", (10, 10), 200).save(c.core.runner.workspace / "flat.png")
    out = run(agentkit.t_image_inspect(c, {"path": "flat.png"}))
    assert "estimated grid: n/a" in out and "dark_ratio=0.000" in out
    assert ("entropy=0.000" in out) or ("entropy=-0.000" in out)  # single colour: zero entropy


def test_image_inspect_missing_and_not_an_image(tmp_path):
    c, _ = make_ctx(tmp_path)
    # v0.42: errors come back as tool text instead of raw exceptions
    assert run(agentkit.t_image_inspect(c, {"path": "nope.png"})).startswith("ERROR")
    (c.core.runner.workspace / "junk.png").write_bytes(b"not an image at all")
    assert "not a readable image" in run(agentkit.t_image_inspect(c, {"path": "junk.png"}))


# ---------------------------------------------------------------- image_decode
def _fake_zxing(results_by_call):
    calls = []
    mod = types.ModuleType("zxingcpp")

    def read_barcodes(img, try_rotate=True, try_downscale=True):
        calls.append(img.size)
        return results_by_call(len(calls))
    mod.read_barcodes = read_barcodes
    mod.calls = calls
    return mod


def _result(text, fmt="QRCode", extra=None, ec="M"):
    pt = lambda x, y: SimpleNamespace(x=x, y=y)  # noqa: E731
    return SimpleNamespace(format=SimpleNamespace(name=fmt), text=text, valid=True, ec_level=ec,
                           position=SimpleNamespace(top_left=pt(1, 2), bottom_right=pt(30, 40)), extra=extra)


def test_image_decode_without_zxing(tmp_path, monkeypatch):
    c, _ = make_ctx(tmp_path)
    monkeypatch.setitem(sys.modules, "zxingcpp", None)  # forces ImportError
    out = run(agentkit.t_image_decode(c, {"path": "x.png"}))
    assert out.startswith("ERROR: zxing-cpp not installed") and "env_install" in out


def test_image_decode_dedupes_across_scales(tmp_path, monkeypatch):
    c, events = make_ctx(tmp_path)
    Image.new("L", (40, 40), 255).save(c.core.runner.workspace / "q.png")
    big = "Z" * 5000
    mod = _fake_zxing(lambda n: [_result("hello", extra={"Version": 3}), _result(big, fmt="Aztec", extra="x")]
                      if n <= 2 else [_result("hello", extra={"Version": 3})])
    monkeypatch.setitem(sys.modules, "zxingcpp", mod)
    out = run(agentkit.t_image_decode(c, {"path": "q.png"}))
    assert out.startswith("2 symbol(s) in q.png")
    assert out.count("text[5]: hello") == 1 and "QRCode version=3" in out and "scale=1.0" in out
    assert "text[5000]: " + "Z" * 4000 in out  # full length reported, text capped at 4000
    assert "Z" * 4001 not in out and "at (1,2)-(30,40)" in out
    assert mod.calls == [(40, 40), (20, 20), (80, 80), (10, 10)]
    assert any(a[1] == "image.decode" and k["found"] == 2 for a, k in events)


def test_image_decode_nothing_found_tiny_image(tmp_path, monkeypatch):
    c, events = make_ctx(tmp_path)
    Image.new("L", (1, 1), 0).save(c.core.runner.workspace / "dot.png")
    mod = _fake_zxing(lambda n: [])
    monkeypatch.setitem(sys.modules, "zxingcpp", mod)
    assert run(agentkit.t_image_decode(c, {"path": "dot.png"})) == "no symbols decoded in dot.png"
    assert all(w >= 1 and h >= 1 for w, h in mod.calls)  # scaled sizes never hit 0
    assert any(k["found"] == 0 for a, k in events if a[1] == "image.decode")


# ---------------------------------------------------------------- skills
@pytest.fixture()
def skills(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    d.mkdir()
    monkeypatch.setenv("AVRONE_SKILLS_DIR", str(d))
    return d


def test_skill_dir_missing(tmp_path, monkeypatch):
    c, _ = make_ctx(tmp_path)
    monkeypatch.setenv("AVRONE_SKILLS_DIR", str(tmp_path / "absent"))
    assert run(agentkit.t_skill_list(c, {})) == "no skills installed"
    assert run(agentkit.t_skill_load(c, {"name": "x"})) == "ERROR: no skill 'x'. Available: none"


def test_skill_discovery_rules(tmp_path, skills):
    c, _ = make_ctx(tmp_path)
    (skills / "alpha").mkdir()
    (skills / "alpha" / "SKILL.md").write_text("---\ndescription: dir wins\n---\nbody")
    (skills / "alpha.md").write_text("# flat loses")
    (skills / "beta.md").write_text("\n\n## Beta heading   with   spaces\nrest")
    (skills / "empty.md").write_text("")
    (skills / "huge.md").write_text("x" * (agentkit.SKILL_MAX_BYTES + 1))
    (skills / "-bad.md").write_text("bad name")
    outside = tmp_path / "secret.md"
    outside.write_text("# secret")
    (skills / "link.md").symlink_to(outside)
    (skills / "dirlink").mkdir()
    (skills / "dirlink" / "SKILL.md").mkdir()  # a directory, not a file -> unreadable
    out = run(agentkit.t_skill_list(c, {}))
    assert "- alpha: dir wins" in out and "- beta: Beta heading with spaces" in out
    assert "- empty: (no description)" in out
    for gone in ("huge", "-bad", "link", "secret", "dirlink", "flat loses"):
        assert gone not in out
    assert out.splitlines()[0].startswith("- alpha")  # sorted


def test_skill_load_errors_and_truncation(tmp_path, skills):
    c, events = make_ctx(tmp_path)
    (skills / "big.md").write_text("A" * (agentkit.SKILL_LOAD_CHARS + 500))
    (skills / "blank.md").write_text("   \n\t\n")
    assert run(agentkit.t_skill_load(c, {})).startswith("ERROR: give")
    assert run(agentkit.t_skill_load(c, None)).startswith("ERROR: give")
    assert "invalid skill name" in run(agentkit.t_skill_load(c, {"name": "../etc"}))
    assert "invalid skill name" in run(agentkit.t_skill_load(c, {"name": "a/b"}))
    assert run(agentkit.t_skill_load(c, {"name": "blank"})) == "ERROR: skill 'blank' is empty."
    assert len(run(agentkit.t_skill_load(c, {"name": "big"}))) == agentkit.SKILL_LOAD_CHARS
    assert any(a[1] == "skill.load" for a, _ in events)


def test_skill_load_file_vanishes(tmp_path, skills, monkeypatch):
    c, _ = make_ctx(tmp_path)
    (skills / "gone.md").write_text("# soon gone")
    real = agentkit._read_skill
    seen = {"n": 0}

    def flaky(f):
        seen["n"] += 1
        return real(f) if seen["n"] == 1 else None
    monkeypatch.setattr(agentkit, "_read_skill", flaky)
    assert run(agentkit.t_skill_load(c, {"name": "gone"})) == "ERROR: skill 'gone' could not be read."


def test_read_skill_oserror(tmp_path, monkeypatch):
    f = tmp_path / "s.md"
    f.write_text("x")

    def boom(self):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "read_bytes", boom)
    monkeypatch.setattr(Path, "open", lambda self, *a, **k: boom(self))
    assert agentkit._read_skill(f) is None


def test_skills_resolve_oserror_skipped(tmp_path, skills, monkeypatch):
    c, _ = make_ctx(tmp_path)
    (skills / "ok.md").write_text("# ok")
    (skills / "loop.md").write_text("# loop")
    real = Path.resolve

    def resolve(self, strict=False):
        if self.name == "loop.md":
            raise OSError("symlink loop")
        return real(self, strict)
    monkeypatch.setattr(Path, "resolve", resolve)
    out = run(agentkit.t_skill_list(c, {}))
    assert "- ok: ok" in out and "loop" not in out


@pytest.mark.parametrize("text,fm,body", [
    ("", {}, ""),
    ("\ufeff---\nname: x\n---\nB", {"name": "x"}, "B"),
    ("---\nk: v\nno-colon\n# c: skip\nK: dup\nq: 'quoted'\nd: \"\"\n---\n", {"k": "v", "q": "quoted", "d": ""}, ""),
    ("---\nunterminated: yes\nbody", {}, "---\nunterminated: yes\nbody"),
])
def test_parse_skill_edges(text, fm, body):
    assert agentkit.parse_skill(text) == (fm, body)


def test_skill_desc_truncated_and_fallback():
    assert agentkit._skill_desc({"description": "w " * 300}, "") == ("w " * 300)[:200]
    assert agentkit._skill_desc({}, "---\n#\n  # Title\n") == "Title"
    assert agentkit._skill_desc({"description": "  "}, "") == "(no description)"


# ---------------------------------------------------------------- env_install
@pytest.fixture()
def allow(monkeypatch):
    monkeypatch.setenv("AVRONE_SELF_CHANGES", "allow")


@pytest.mark.parametrize("pkg", ["", "-e evil", "a" * 90, "pkg==1.0;rm", "pkg>=1", "x y"])
def test_env_install_rejects_bad_names(tmp_path, allow, pkg):
    c, _ = make_ctx(tmp_path)
    assert run(agentkit.t_env_install(c, {"package": pkg})) == "ERROR: invalid package name"
    assert not c.core.runner.cmds


def test_env_install_unknown_manager(tmp_path, allow):
    c, _ = make_ctx(tmp_path)
    assert run(agentkit.t_env_install(c, {"package": "jq", "manager": "brew"})) == "ERROR: manager must be pip or apt"
    assert not c.core.runner.cmds


def test_env_install_apt_success_and_pinned_pip_check(tmp_path, allow):
    c, _ = make_ctx(tmp_path, codes=(0, 0, 0, 0))
    out = run(agentkit.t_env_install(c, {"package": "jq", "manager": "APT"}))
    assert out.startswith("installed jq via apt") and "sudo -n apt-get" in c.core.runner.cmds[0]
    assert c.core.runner.cmds[1] == "dpkg -s jq >/dev/null 2>&1"
    run(agentkit.t_env_install(c, {"package": "rich==13.0"}))
    assert c.core.runner.cmds[-1] == "python3 -m pip show rich >/dev/null 2>&1"


def test_env_install_failure_is_logged_and_retried(tmp_path, allow):
    c, _ = make_ctx(tmp_path, codes=(1, 1, 0, 0))
    out = run(agentkit.t_env_install(c, {"package": "nopkg"}))
    assert out.startswith("FAILED nopkg via pip") and "[exit 1]" in out
    assert "pip install nopkg FAIL" in run(agentkit.t_resources(c, {}))
    # a failed install is not "already installed": the next call tries again
    assert run(agentkit.t_env_install(c, {"package": "nopkg"})).startswith("installed nopkg")


def test_env_install_log_policy_executes(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_SELF_CHANGES", "LOG")
    c, _ = make_ctx(tmp_path)
    assert run(agentkit.t_env_install(c, {"package": "tqdm"})).startswith("installed tqdm")


def test_env_install_race_second_check_under_lock(tmp_path, allow, monkeypatch):
    c, _ = make_ctx(tmp_path)
    answers = iter([None, "Agent-9"])
    monkeypatch.setattr(agentkit, "_installed_before", lambda ctx, mgr, pkg: next(answers))
    out = run(agentkit.t_env_install(c, {"package": "numpy"}))
    assert out == "numpy already installed via pip by Agent-9 (resource ledger); no change made"
    assert not c.core.runner.cmds


def test_installed_before_skips_corrupt_lines(tmp_path):
    c, _ = make_ctx(tmp_path)
    led = agentkit._ledger(c)
    led.write_text("not json\n" + json.dumps({"ok": True, "action": "pip install", "target": "x"}) + "\n{broken\n")
    assert agentkit._installed_before(c, "pip", "x") == "another agent"
    assert agentkit._installed_before(c, "apt", "x") is None


# ---------------------------------------------------------------- resources / task board
def test_resources_empty_and_limit(tmp_path):
    c, _ = make_ctx(tmp_path)
    assert run(agentkit.t_resources(c, {})) == "resource ledger empty"
    for i in range(5):
        agentkit.record_change(c, "pip install", f"p{i}", i % 2 == 0, "d" * 900)
    out = run(agentkit.t_resources(c, {"limit": 2})).splitlines()
    assert len(out) == 2 and out[0].endswith("p3 FAIL") and out[1].endswith("p4 ok")
    rows = [json.loads(x) for x in agentkit._ledger(c).read_text().splitlines()]
    assert all(len(r["detail"]) == 500 for r in rows)


def test_task_board_edges(tmp_path):
    c, events = make_ctx(tmp_path, run_id="../../weird id!")
    assert agentkit._board(c).name.startswith("weirdid-") and agentkit._board(c).name.endswith(".json")  # round 97: hash suffix
    assert run(agentkit.t_task_board(c, {})) == "board empty"
    assert run(agentkit.t_task_board(c, {"op": "delete"})) == "ERROR: op must be add, update or list"
    assert run(agentkit.t_task_board(c, {"op": "update", "id": 7})) == "ERROR: no such task"
    run(agentkit.t_task_board(c, {"op": "add", "title": "T" * 300, "owner": "Bob"}))
    out = run(agentkit.t_task_board(c, {"op": "update", "id": "1", "owner": "Eve", "note": "n" * 2000}))
    item = json.loads(agentkit._board(c).read_text())[0]
    assert len(item["title"]) == 200 and item["owner"] == "Eve" and len(item["note"]) == 1000
    assert "(owner Eve) - nnn" in out
    n_events = len([e for e in events if e[0][1] == "task.board"])
    run(agentkit.t_task_board(c, {"op": "list"}))
    assert len([e for e in events if e[0][1] == "task.board"]) == n_events  # list doesn't emit
    assert run(agentkit.t_task_board(c, {"op": "add"})) == "ERROR: op=add needs a title"


def test_task_board_empty_run_id(tmp_path):
    c, _ = make_ctx(tmp_path, run_id="///")
    assert agentkit._board(c).name.startswith("run-")  # round 97: hash suffix, no shared "run" board


def test_register_kit_tools():
    from avrone.tools import ToolRegistry
    reg = ToolRegistry()
    agentkit.register_kit_tools(reg)
    assert {"image_inspect", "image_decode", "skill_list", "skill_load", "env_install", "resources",
            "task_board"} <= set(reg.names())
