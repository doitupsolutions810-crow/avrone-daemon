"""Round 60: agent home containers (busy homes reaped, image flag injection, bytes inspect, cache)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from avrone import agentcontainer as C


class R:
    def __init__(self, code=0, out="", err=""):
        self.exit_code, self.stdout, self.stderr = code, out, err


class Docker:
    """Fake docker: tracks running containers and every argv."""

    def __init__(self, inspect_bytes=False):
        self.calls: list[list[str]] = []
        self.running: dict[str, str] = {}
        self.inspect_bytes = inspect_bytes

    async def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        op = argv[1]
        if op == "inspect":
            name = argv[-1]
            if name not in self.running:
                return R(1, "", "Error: No such object")
            out = f"true|{self.running[name]}\n"
            return R(0, out.encode() if self.inspect_bytes else out)
        if op == "rm":
            self.running.pop(argv[-1], None)
            return R(0)
        if op == "run":
            name = argv[argv.index("--name") + 1]
            src = next(a for a in argv if a.endswith(":/agent") or "source=" in a)
            self.running[name] = src.rsplit(":/agent", 1)[0] if src.endswith(":/agent") else src
            return R(0, "id")
        return R(0)

    def rms(self, name):
        return sum(1 for c in self.calls if c[1] == "rm" and c[-1] == name)

    def runs(self):
        return sum(1 for c in self.calls if c[1] == "run")


@pytest.fixture
def ws(tmp_path):
    for n in ("a", "b", "c"):
        (tmp_path / "agents" / n).mkdir(parents=True)
    return tmp_path / "agents"


def pool(d, t, idle=10, **kw):
    return C.AgentContainerPool(d, "python:3.12-slim", False, node="n", clock=lambda: t[0], idle=idle, **kw)


# ---- busy homes are never reaped ----------------------------------------------------------------

def test_busy_home_not_reaped_by_other_agent(ws):
    async def go():
        d, t = Docker(), [0.0]
        p = pool(d, t)
        na = await p.ensure(ws / "a")
        p.begin(na)  # a long command starts in a's home
        t[0] = 30.0
        await p.ensure(ws / "b")  # another agent's ensure runs the reaper
        assert d.rms(na) == 1  # only the pre-create cleanup rm, never a reap
        assert na in p.homes and na in d.running
        p.end(na)
        t[0] = 35.0
        await p.ensure(ws / "b")
        assert na in p.homes  # idle counts from end(): 5 s < 10 s
        t[0] = 46.0
        await p.ensure(ws / "b")
        assert na not in p.homes and d.rms(na) == 2
    asyncio.run(go())


def test_nested_begin_end(ws):
    async def go():
        d, t = Docker(), [0.0]
        p = pool(d, t)
        na = await p.ensure(ws / "a")
        p.begin(na)
        p.begin(na)
        p.end(na)
        t[0] = 100.0
        assert await p.reap_idle() == []
        p.end(na)
        t[0] = 111.0
        assert await p.reap_idle() == [na]
    asyncio.run(go())


def test_end_more_than_begin_and_unknown_names(ws):
    async def go():
        d, t = Docker(), [0.0]
        p = pool(d, t)
        na = await p.ensure(ws / "a")
        p.end(na)
        p.end(na)
        assert p.homes[na].active == 0
        p.begin("nope")
        p.end("nope")
        t[0] = 20.0
        assert await p.reap_idle() == [na]
    asyncio.run(go())


def test_state_shows_active_and_no_negative_idle(ws):
    async def go():
        d, t = Docker(), [50.0]
        p = pool(d, t)
        na = await p.ensure(ws / "a")
        p.begin(na)
        st = p.state()["homes"][na]
        assert st["active"] == 1 and st["idle_s"] == 0.0
        p.end(na)
        t[0] = 10.0  # clock went backwards
        assert p.state()["homes"][na]["idle_s"] == 0.0
    asyncio.run(go())


def test_close_removes_busy_homes(ws):
    async def go():
        d, t = Docker(), [0.0]
        p = pool(d, t)
        na = await p.ensure(ws / "a")
        p.begin(na)
        await p.close()
        assert p.homes == {} and na not in d.running
    asyncio.run(go())


def test_sovereign_wraps_exec_in_begin_end():
    src = Path(C.__file__).with_name("sovereign.py").read_text()
    i = src.index("pool.ensure(agent_cwd)")
    block = src[i:i + 1200]
    assert block.count("pool.begin(name)") == 2 and block.count("pool.end(name)") == 2
    assert block.count("finally:") == 2


# ---- the cache never hands a home to another dir -------------------------------------------------

def test_cached_home_other_dir_refused(ws, tmp_path):
    async def go():
        d, t = Docker(), [0.0]
        p = pool(d, t)
        na = await p.ensure(ws / "a")
        other = tmp_path / "elsewhere" / "agents" / "a"
        other.mkdir(parents=True)
        with pytest.raises(C.ForeignContainer):
            await p.ensure(other)
        assert p.homes[na].agent_dir == (ws / "a").resolve()
    asyncio.run(go())


def test_cached_home_same_dir_via_symlink_ok(ws, tmp_path):
    async def go():
        d, t = Docker(), [0.0]
        p = pool(d, t)
        na = await p.ensure(ws / "a")
        link = tmp_path / "lnk"
        link.symlink_to(ws / "a")
        assert await p.ensure(link) == na
        assert p.homes[na].uses == 2
    asyncio.run(go())


# ---- docker inspect output as bytes --------------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    (b"true|/x\n", (True, "/x")),
    (bytearray(b"false|/y"), (False, "/y")),
    (b"TRUE", (True, "")),
    (b"\xff\xfe|z", (False, "z")),
    (b"", (False, "")),
    (None, (False, "")),
    ("true|/a|b", (True, "/a|b")),
])
def test_parse_inspect_bytes(raw, want):
    assert C.parse_inspect(raw) == want


def test_running_home_with_bytes_inspect_is_adopted(ws):
    async def go():
        d, t = Docker(inspect_bytes=True), [0.0]
        p1 = pool(d, t)
        na = await p1.ensure(ws / "a")
        p2 = pool(d, t)  # fresh daemon process: no cache, the container is still running
        assert await p2.ensure(ws / "a") == na
        assert d.runs() == 1  # adopted, not rm -f'd and recreated
    asyncio.run(go())


def test_bytes_inspect_foreign_dir_detected(ws, tmp_path):
    async def go():
        d, t = Docker(inspect_bytes=True), [0.0]
        name = C.container_name("n", "a")
        d.running[name] = str(tmp_path / "other")
        with pytest.raises(C.ForeignContainer):
            await pool(d, t).ensure(ws / "a")
        assert name in d.running
    asyncio.run(go())


# ---- image reference cannot be an option ---------------------------------------------------------

@pytest.mark.parametrize("bad", ["--privileged", "-v=/:/host", "", " img", "img ", "a b", "img\n--privileged",
                                 "img\x00", None, 5, ["img"], "x" * 600, "-", ".img", "/img"])
def test_check_image_rejects(bad):
    with pytest.raises(ValueError):
        C.check_image(bad)


@pytest.mark.parametrize("ok", ["python:3.12-slim", "alpine", "localhost:5000/team/img:v1.2",
                                "ghcr.io/o/r@sha256:" + "a" * 64, "busybox:1.36.1-musl", "img_x.y"])
def test_check_image_accepts(ok):
    assert C.check_image(ok) == ok


def test_persistent_run_argv_refuses_flag_image(tmp_path):
    with pytest.raises(ValueError):
        C.persistent_run_argv("n", tmp_path, "--privileged", False)
    argv = C.persistent_run_argv("n", tmp_path, "alpine", False)
    assert argv[-3:] == ["alpine", "sleep", "infinity"] and "--privileged" not in argv


def test_pool_with_flag_image_never_runs_docker(ws):
    async def go():
        d, t = Docker(), [0.0]
        p = C.AgentContainerPool(d, "--privileged", False, node="n", clock=lambda: t[0], idle=10)
        with pytest.raises(ValueError):
            await p.ensure(ws / "a")
        assert d.runs() == 0 and p.homes == {}
    asyncio.run(go())


def test_sovereign_checks_image_everywhere():
    src = Path(C.__file__).with_name("sovereign.py").read_text()
    assert "[self.s.docker_image" not in src
    assert src.count("check_image(") >= 2


# ---- earlier guarantees --------------------------------------------------------------------------

def test_hardening_and_network_defaults(tmp_path):
    argv = C.persistent_run_argv("n", tmp_path, "alpine", False, mem=float("nan"), cpu=float("inf"))
    s = " ".join(argv)
    assert "--cap-drop ALL" in s and "--network none" in s and "no-new-privileges" in s
    assert "--cpus 64.0" in s and "nan" not in s


@pytest.mark.parametrize("to", [None, 0, -5, float("nan"), float("inf"), 10 ** 400, "x", 0.4, 30])
def test_launcher_never_raises(to):
    out = C.launcher("echo hi", to)
    assert out[-1] == "echo hi"


def test_container_name_safe():
    n = C.container_name("Évil Node!!", "../../Root")
    assert n.startswith("avrone-agent-") and "/" not in n and " " not in n
    assert C.container_name("", "") == "avrone-agent-node-agent"


def test_idle_after_odd_values(ws):
    for v, ok in ((float("nan"), None), (-1, None), (float("inf"), 7 * 86400.0), ("junk", None)):
        p = C.AgentContainerPool(Docker(), "alpine", False, node="n", idle=v)
        if ok is None:
            assert p.idle_after == C.idle_secs()
        else:
            assert p.idle_after == ok
