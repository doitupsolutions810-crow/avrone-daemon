import asyncio
import json
from types import SimpleNamespace

from avrone import tools_secrets as ts
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools


def run(c):
    return asyncio.run(c)


def ctx(tmp_path):
    s = SimpleNamespace(data_dir=str(tmp_path / "data"), workspace=str(tmp_path / "ws"))
    return SimpleNamespace(core=SimpleNamespace(settings=s, runner=SimpleNamespace()))


def test_registered_and_acting():
    r = builtin_registry()
    register_extra_tools(r)
    assert r.get("secret_scan") and r.get("mcp_selftest")
    from avrone.tools_statehealth import ACTING
    assert "secret_scan" in ACTING and "mcp_selftest" not in ACTING
    assert r.check("secret_scan", {"fix": "yes"})[0] == "bad_args"


def test_scan_reports_without_values_and_fixes(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "tok-ABCDEFGH12345")
    runs = tmp_path / "data" / "runs"
    runs.mkdir(parents=True)
    (runs / "old.jsonl").write_text(json.dumps({"data": {"output": "MY_SERVICE_TOKEN=tok-ABCDEFGH12345"}}) + "\n"
                                    + json.dumps({"data": {"output": "fine"}}) + "\n")
    (runs / "clean.jsonl").write_text('{"x": 1}\n')
    c = ctx(tmp_path)
    out = run(ts.t_secret_scan(c, {}))
    assert out.startswith("secret_scan [runs]: FOUND - 1 line(s) in 1 file(s)") and "old.jsonl:1" in out
    assert "ABCDEFGH" not in out and "env-secret-value" in out
    out = run(ts.t_secret_scan(c, {"fix": True}))
    assert "fixed 1 run log(s); 0 line(s) still flagged" in out
    assert "ABCDEFGH" not in (runs / "old.jsonl").read_text()
    assert '"fine"' in (runs / "old.jsonl").read_text()
    assert run(ts.t_secret_scan(c, {})).startswith("secret_scan [runs]: CLEAN")


def test_scope_errors_and_missing(tmp_path, monkeypatch):
    c = ctx(tmp_path)
    assert run(ts.t_secret_scan(c, {"scope": "etc"})).startswith("ERROR")
    assert "only allowed for scope runs or data" in run(ts.t_secret_scan(c, {"scope": "workspace", "fix": True}))  # round 158
    assert "CLEAN" in run(ts.t_secret_scan(c, {"scope": "workspace"}))  # missing dir is clean, not a crash
    assert "no data directory" in run(ts.t_secret_scan(SimpleNamespace(core=None), {"scope": "data"}))


def test_workspace_scope_skips_git_symlinks_binaries(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    (ws / ".git" / "config").write_text("url = https://u:p4ssw0rd@x.test/r")
    (ws / "a.png").write_bytes(b"password=abcdefgh")
    (ws / "deploy.sh").write_text("curl -H 'Authorization: Bearer abcdefghijkl' x\n")
    (ws / "link.sh").symlink_to(ws / "deploy.sh")
    out = run(ts.t_secret_scan(ctx(tmp_path), {"scope": "workspace"}))
    assert "deploy.sh:1" in out and "link.sh" not in out and ".git" not in out and "a.png" not in out


def test_caps(tmp_path, monkeypatch):
    runs = tmp_path / "data" / "runs"
    runs.mkdir(parents=True)
    for i in range(3):
        (runs / f"r{i}.jsonl").write_text("password=abcdefgh1\n" * 20)
    monkeypatch.setattr(ts, "MAX_FILES", 2)
    monkeypatch.setattr(ts, "MAX_SHOWN", 5)
    out = run(ts.t_secret_scan(ctx(tmp_path), {}))
    assert "(file cap reached)" in out and "... 35 more" in out


def test_mcp_selftest_pass_and_fail(monkeypatch):
    out = run(ts.t_mcp_selftest(None, {}))
    assert out.startswith("mcp_selftest: PASS") and "avrone_squad" in out and "FAILED" not in out

    async def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(ts, "mcp_selftest", boom)
    assert "FAIL - server raised RuntimeError" in run(ts.t_mcp_selftest(None, {}))

    async def partial():
        return {"protocol": "x", "server": {}, "tools": [], "checks": {"ping": False}}
    monkeypatch.setattr(ts, "mcp_selftest", partial)
    assert "ping: FAILED" in run(ts.t_mcp_selftest(None, {}))
