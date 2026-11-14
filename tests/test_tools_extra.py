import asyncio
import json
import shutil
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from avrone import tools_extra as tx
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import CommandResult, LocalRunner
from avrone.tools import ToolRegistry


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    for k in ("BRAVE_API_KEY", "TAVILY_API_KEY", "AVRONE_SEARCH_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(workspace=tmp_path / "ws", data_dir=tmp_path / "data", agents_can_shell=True,
                 allow_network=True, command_timeout=60)
    runner = LocalRunner(s)
    run(runner.start())
    codex = Codex(s.data_dir)
    core = SimpleNamespace(settings=s, runner=runner, codex=codex)
    ctx = SimpleNamespace(core=core, run_id="run1", name="Worker-1", depth=0)
    reg = ToolRegistry()
    tx.register_extra_tools(reg)
    monkeypatch.setattr(tx, "HTTP_TRANSPORT", None)
    return SimpleNamespace(s=s, ws=s.workspace, codex=codex, ctx=ctx, reg=reg)


def call(env, _tool, **args):
    return run(env.reg.call(env.ctx, _tool, args))


def events(env):
    return env.codex.replay("run1")


def test_registration():
    reg = ToolRegistry()
    tx.register_extra_tools(reg)
    expected = {"web_search", "python", "list_files", "search_files", "edit_file", "http_request", "git",
                "memory_save", "memory_recall", "artifact_save", "docker_run", "think", "diff_files", "codex_search",
                "self_scan", "capabilities_report"}
    assert expected <= set(reg.names())
    assert all(t["source"] == "builtin" for t in reg.list())


def test_python(env):
    out = call(env, "python", code="import sys\nprint('hi', 6*7)\nprint('err', file=sys.stderr)")
    assert "hi 42" in out and "[exit 0]" in out and "err" in out
    assert list((env.ws / ".avrone" / "tmp").iterdir()) == []  # temp file cleaned up
    env.s.agents_can_shell = False
    assert call(env, "python", code="print(1)").startswith("ERROR")


def test_list_files(env):
    (env.ws / "src" / "pkg").mkdir(parents=True)
    (env.ws / "src" / "pkg" / "a.py").write_text("x")
    (env.ws / "node_modules" / "junk").mkdir(parents=True)
    (env.ws / ".git").mkdir()
    (env.ws / "README.md").write_text("hello")
    out = call(env, "list_files")
    assert "src/" in out and "a.py" in out and "README.md (5b)" in out
    assert "node_modules" not in out and ".git" not in out
    shallow = call(env, "list_files", depth=1)
    assert "src/" in shallow and "a.py" not in shallow
    assert "a.py" in call(env, "list_files", path="src/pkg")
    assert call(env, "list_files", path="../..").startswith("ERROR")


def test_search_files(env):
    (env.ws / "a.py").write_text("def foo():\n    return 1\n")
    (env.ws / "sub").mkdir()
    (env.ws / "sub" / "b.txt").write_text("nothing\nfoo bar\n")
    (env.ws / "bin.dat").write_bytes(b"\0foo\0")
    (env.ws / "__pycache__").mkdir()
    (env.ws / "__pycache__" / "c.py").write_text("foo")
    out = call(env, "search_files", pattern=r"foo")
    assert "a.py:1: def foo():" in out and "sub/b.txt:2: foo bar" in out
    assert "bin.dat" not in out and "__pycache__" not in out
    assert "a.py" not in call(env, "search_files", pattern="foo", path="sub")
    assert call(env, "search_files", pattern="(").startswith("ERROR")
    assert "No matches" in call(env, "search_files", pattern="zzz")


def test_edit_file(env):
    f = env.ws / "f.txt"
    f.write_text("alpha beta beta gamma")
    assert "1 replacement" in call(env, "edit_file", path="f.txt", old="alpha", new="ALPHA")
    assert f.read_text() == "ALPHA beta beta gamma"
    assert "matches 2 times" in call(env, "edit_file", path="f.txt", old="beta", new="x")
    assert "not found" in call(env, "edit_file", path="f.txt", old="delta", new="x")
    assert call(env, "edit_file", path="nope.txt", old="a", new="b").startswith("ERROR")
    assert "escapes" in call(env, "edit_file", path="../../etc/passwd", old="a", new="b")
    assert f.read_text() == "ALPHA beta beta gamma"


@pytest.mark.skipif(not shutil.which("git"), reason="git not installed")
def test_git(env):
    out = call(env, "git", args="init -q")
    assert "[exit 0]" in out
    (env.ws / "x.txt").write_text("x")
    assert "x.txt" in call(env, "git", args="status --porcelain")
    for bad in ("push --force origin main", "push -f", "push origin +main", "git push --force-with-lease"):
        assert "force push is not allowed" in call(env, "git", args=bad)
    # chaining is neutralised by quoting
    out = call(env, "git", args="status; touch pwned")
    assert not (env.ws / "pwned").exists()
    env.s.agents_can_shell = False
    assert call(env, "git", args="status").startswith("ERROR")


def test_memory(env):
    assert call(env, "memory_recall") == "No memories saved yet."
    call(env, "memory_save", key="db_choice", value="Use Postgres for the todo app")
    call(env, "memory_save", key="deploy_target", value="fly.io")
    call(env, "memory_save", key="db_choice", value="Switched to SQLite")
    lines = (env.s.data_dir / "memory.jsonl").read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["agent"] == "Worker-1"
    out = call(env, "memory_recall", query="db")
    assert "SQLite" in out and "Postgres" not in out
    assert out.splitlines()[0].startswith("- db_choice")
    assert "fly.io" in call(env, "memory_recall", query="deploy")
    all_ = call(env, "memory_recall")
    assert "db_choice" in all_ and "deploy_target" in all_
    assert "No memories match" in call(env, "memory_recall", query="kubernetes")
    assert call(env, "memory_save", key="", value="x").startswith("ERROR")


def test_artifact_save(env):
    out = call(env, "artifact_save", name="report/summary.md", content="# Done")
    assert out == "artifacts/run1/report/summary.md"
    assert (env.ws / out).read_text() == "# Done"
    ev = [e for e in events(env) if e["kind"] == "artifact"]
    assert ev and ev[0]["data"] == {"path": out, "bytes": 6} and ev[0]["agent"] == "Worker-1"
    assert "escapes" in call(env, "artifact_save", name="../../../evil.txt", content="x")


def test_think(env):
    assert call(env, "think", thought="plan: do X then Y") == "noted"
    ev = [e for e in events(env) if e["kind"] == "agent.think"]
    assert ev and ev[0]["data"]["thought"] == "plan: do X then Y"


def test_docker_run_unavailable(env, monkeypatch):
    monkeypatch.setattr(tx.shutil, "which", lambda name: None)
    assert "docker is not available" in call(env, "docker_run", image="alpine", cmd="echo hi")


def test_docker_run_builds_command(env, monkeypatch):
    monkeypatch.setattr(tx.shutil, "which", lambda name: "/usr/bin/docker")
    seen = {}

    async def fake_exec(argv, cmd, timeout, cwd=None):
        seen["argv"] = argv
        return CommandResult(cmd, 0, "hi\n", "")

    monkeypatch.setattr(env.ctx.core.runner, "_exec", fake_exec)
    env.s.allow_network = False
    out = call(env, "docker_run", image="python:3.12-slim", cmd="echo hi")
    assert "hi" in out
    a = seen["argv"]
    assert a[:3] == ["docker", "run", "--rm"] and f"{env.ws.resolve()}:/workspace" in a
    assert "--network" in a and a[-3:] == ["sh", "-c", "echo hi"]
    assert "invalid image" in call(env, "docker_run", image="bad image; rm", cmd="x")
    env.s.agents_can_shell = False
    assert call(env, "docker_run", image="alpine", cmd="x").startswith("ERROR")


# --- network tools, mocked -------------------------------------------------

DDG_HTML = """
<div class="result"><h2 class="result__title">
<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">Example <b>Page</b></a></h2>
<a class="result__snippet" href="x">An <b>example</b> snippet &amp; more.</a></div>
<div class="result"><a rel="nofollow" class="result__a" href="https://second.org/">Second</a>
<a class="result__snippet" href="y">Second snippet</a></div>
"""


def test_web_search_duckduckgo(env, monkeypatch):
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = req.content.decode()
        return httpx.Response(200, text=DDG_HTML)

    monkeypatch.setattr(tx, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    out = call(env, "web_search", query="example query", max_results=5)
    assert seen["url"].startswith("https://html.duckduckgo.com/html/") and "q=example" in seen["body"]
    assert "1. Example Page\n   https://example.com/page\n   An example snippet & more." in out
    assert "2. Second" in out and "https://second.org/" in out
    one = call(env, "web_search", query="x", max_results=1)
    assert "2. Second" not in one


def test_web_search_brave_and_tavily(env, monkeypatch):
    def handler(req):
        if req.url.host == "api.search.brave.com":
            assert req.headers["X-Subscription-Token"] == "bk"
            return httpx.Response(200, json={"web": {"results": [
                {"title": "Brave hit", "url": "https://b.com", "description": "from brave"}]}})
        if req.url.host == "api.tavily.com":
            assert json.loads(req.content)["api_key"] == "tk"
            return httpx.Response(200, json={"results": [
                {"title": "Tavily hit", "url": "https://t.com", "content": "from tavily"}]})
        return httpx.Response(500)

    monkeypatch.setattr(tx, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setenv("TAVILY_API_KEY", "tk")
    assert "Tavily hit" in call(env, "web_search", query="q")
    monkeypatch.setenv("BRAVE_API_KEY", "bk")
    assert "Brave hit" in call(env, "web_search", query="q")  # brave preferred in auto
    monkeypatch.setenv("AVRONE_SEARCH_PROVIDER", "tavily")
    assert "Tavily hit" in call(env, "web_search", query="q")


def test_web_search_network_disabled(env):
    env.s.allow_network = False
    assert "disabled" in call(env, "web_search", query="q")


def test_http_request(env, monkeypatch):
    seen = {}

    def handler(req):
        seen["method"] = req.method
        seen["json"] = req.content.decode()
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(201, headers={"Content-Type": "application/json", "X-Other": "hidden"},
                              text="x" * 9000)

    monkeypatch.setattr(tx, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    out = call(env, "http_request", method="post", url="https://api.test/items",
               headers={"Authorization": "Bearer t"}, body={"a": 1})
    assert seen["method"] == "POST" and json.loads(seen["json"]) == {"a": 1} and seen["auth"] == "Bearer t"
    assert out.startswith("HTTP 201") and "content-type: application/json" in out
    assert "x-other" not in out.lower() and "truncated" in out
    assert "url must start" in call(env, "http_request", method="GET", url="file:///etc/passwd")
    assert "unsupported method" in call(env, "http_request", method="TRACE", url="https://a.b")
    env.s.allow_network = False
    assert "disabled" in call(env, "http_request", method="GET", url="https://a.b")

def test_search_files_eof_before_exit_must_not_kill_child(env, monkeypatch):
    """A child that closes stdout just before exit must be reaped, not killed.

    The full suite intermittently saw asyncio's child watcher report returncode
    255 for a successful no-match scan because cleanup killed before wait().
    """
    class FakeStdin:
        def write(self, data):
            assert data
        async def drain(self):
            return None
        def close(self):
            return None

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stderr = None
            self.returncode = None
            self.killed = False
            self.waited = False

            async def no_lines():
                if False:
                    yield b""
            self.stdout = no_lines()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True
            if self.returncode is None:
                self.returncode = 0
            return self.returncode

    child = FakeProcess()

    async def spawn(*args, **kwargs):
        return child

    monkeypatch.setattr(tx.asyncio, "create_subprocess_exec", spawn)
    output = call(env, "search_files", pattern="nothing-can-match")
    assert "No matches" in output
    assert child.waited
    assert not child.killed
    assert child.returncode == 0


def test_search_files_hung_child_still_killed_on_deadline(env, monkeypatch):
    """Normal EOF must be safe without weakening the catastrophic-regex timeout."""
    class FakeStdin:
        def write(self, data):
            return None
        async def drain(self):
            return None
        def close(self):
            return None

    class HungProcess:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stderr = None
            self.returncode = None
            self.killed = False

            async def no_eof():
                await asyncio.sleep(60)
                if False:
                    yield b""
            self.stdout = no_eof()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    child = HungProcess()

    async def spawn(*args, **kwargs):
        return child

    monkeypatch.setattr(tx.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tx, "SEARCH_TIMEOUT_S", 0.01)
    output = call(env, "search_files", pattern="slow")
    assert "search stopped after" in output
    assert child.killed
