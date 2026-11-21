import asyncio
import os
import subprocess
import time

import pytest

from avrone import tools_hostview as hv
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools


def run(c):
    return asyncio.run(c)


def test_registered():
    r = builtin_registry()
    register_extra_tools(r)
    assert r.get("proc_status") and r.get("api_surface")
    assert r.check("proc_status", {"all": "yes"})[0] == "bad_args"


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="Linux /proc only")
def test_proc_status_sees_own_children(monkeypatch):
    monkeypatch.setenv("AVRONE_PROC_LONG_S", "0.5")
    p = subprocess.Popen(["sleep", "30"], env={"PATH": os.environ["PATH"], "SECRET_X": "hunter2"})
    try:
        time.sleep(0.8)
        out = run(hv.t_proc_status(None, {}))
        assert f"- {p.pid} [" in out and "sleep 30" in out and "hunter2" not in out
        assert "running >= 0s" in out
        assert f"- {p.pid}" in run(hv.t_proc_status(None, {"all": True}))
    finally:
        p.kill()
        p.wait()


@pytest.mark.skipif(not os.path.isdir("/proc"), reason="Linux /proc only")
def test_proc_status_zombie_and_caps(monkeypatch):
    monkeypatch.setenv("AVRONE_PROC_LONG_S", "bogus")
    assert hv._long_s() == 600.0
    monkeypatch.setenv("AVRONE_PROC_LONG_S", "-3")
    assert hv._long_s() == 600.0
    z = subprocess.Popen(["true"])
    time.sleep(0.3)  # exited, not yet waited: a zombie child
    try:
        out = run(hv.t_proc_status(None, {}))
        assert "1 zombie(s)" in out and f"- {z.pid} [Z]" in out
        monkeypatch.setattr(hv, "MAX_PROCS", 0)
        # Other children of this process (a long suite leaves some) must not
        # change the result: a cap of 0 hides every row, including this zombie.
        capped = run(hv.t_proc_status(None, {}))
        assert f"- {z.pid}" not in capped and " more" in capped
    finally:
        z.wait()


def test_proc_status_without_proc(monkeypatch, tmp_path):
    monkeypatch.setattr(hv, "PROC", tmp_path / "nope")
    assert "Linux only" in run(hv.t_proc_status(None, {}))
    monkeypatch.setattr(hv, "PROC", tmp_path)
    assert hv._children(1) == {} and hv._boot_time() is None


def test_children_tree_from_fake_proc(monkeypatch, tmp_path):
    def mk(pid, ppid, cmd, state="S"):
        d = tmp_path / str(pid)
        d.mkdir()
        rest = [state, str(ppid)] + ["0"] * 17 + ["100", "0", "10"] + ["0"] * 10
        (d / "stat").write_text(f"{pid} (n a) me) " + " ".join(rest))
        (d / "cmdline").write_bytes(cmd)
    (tmp_path / "stat").write_text("cpu 1\nbtime 1000\n")
    mk(10, 1, b"avrone\x00serve")
    mk(11, 10, b"bash\x00-lc\x00x")
    mk(12, 11, b"")
    mk(13, 1, b"other")
    (tmp_path / "14").mkdir()  # raced exit: no stat
    monkeypatch.setattr(hv, "PROC", tmp_path)
    kids = hv._children(10)
    assert set(kids) == {11, 12} and kids[11]["cmd"] == "bash -lc x" and kids[12]["cmd"] == "n a) me"
    assert kids[11]["started"] == 1000 + 100 / os.sysconf("SC_CLK_TCK")


def test_api_surface():
    out = run(hv.t_api_surface(None, {}))
    assert "POST /api/fleet/run [fleet-signed]" in out and "POST /api/fleet/register [fleet-signed]" in out
    assert "POST /api/terminal [admin]" in out and "GET /api/status [public]" in out and "GET /api/runs [local]" in out
    assert "/openapi" not in out and "WS /ws/events" in out
    a = hv.api_surface()
    assert {r["auth"] for r in a["routes"]} <= {"public", "admin", "fleet-signed", "local", "unknown"}
    assert hv._auth_class(len) == "unknown"


def test_api_surface_failure(monkeypatch):
    monkeypatch.setattr(hv, "api_surface", lambda: (_ for _ in ()).throw(ImportError("no fastapi")))
    assert "no HTTP app" in run(hv.t_api_surface(None, {}))
