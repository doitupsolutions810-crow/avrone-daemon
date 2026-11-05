"""Round 57: MCP HTTP credentials on cross-origin redirects, plus hostile-HTTP-server probes.

  * HTTPTransport followed redirects with every configured header. httpx drops Authorization on a
    cross-origin hop, but an X-Api-Key (or any custom credential header) and the Mcp-Session-Id went
    to whatever host the server's Location named. A request hook now strips the configured headers,
    the session id, cookies and auth headers whenever a hop leaves the configured origin (scheme,
    host, port; an https -> http downgrade on the same host counts as leaving).

Probed against a raw-socket server with no change needed: a 1-byte-per-0.5 s drip, endless SSE
keep-alives, a 2 GB gzip bomb (1.9 MB on the wire; refused at the 32 MB cap, peak ~144 MB traced),
a malformed chunk header, a 60 MB SSE data line with no newline, and a server that never answers.
Each ended within the tool timeout as an MCPError.
"""
from __future__ import annotations

import asyncio

import pytest

from avrone.mcp_client import HTTPTransport, MCPClient, MCPError


async def _recorder(seen):
    async def handle(r, w):
        data = await r.read(65536)
        seen.append(data.split(b"\r\n\r\n")[0].decode(errors="replace"))
        w.write(b"HTTP/1.1 500 x\r\nContent-Length: 0\r\n\r\n")
        await w.drain()
        w.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def _redirector(location_fn):
    async def handle(r, w):
        await r.read(65536)
        w.write(f"HTTP/1.1 307 Temporary Redirect\r\nLocation: {location_fn()}\r\nContent-Length: 0\r\n\r\n".encode())
        await w.drain()
        w.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


HEADERS = {"X-Api-Key": "SECRET123", "Authorization": "Bearer TOPSECRET", "X-Custom": "c"}


def _leaks(seen):
    return [ln for s in seen for ln in s.split("\r\n") if "SECRET" in ln or ln.lower().startswith("x-custom")]


def test_cross_origin_redirect_drops_configured_headers(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    async def go():
        seen = []
        evil, ep = await _recorder(seen)
        good, gp = await _redirector(lambda: f"http://localhost:{ep}/steal")
        c = MCPClient("t", {"url": f"http://127.0.0.1:{gp}/mcp", "headers": HEADERS, "timeout": 3})
        with pytest.raises(MCPError):
            await c.connect()
        await c.close()
        evil.close(); good.close()
        return seen
    seen = asyncio.run(go())
    assert seen and seen[0].startswith("POST /steal")
    assert _leaks(seen) == [], _leaks(seen)


def test_session_id_not_sent_across_origins(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    async def go():
        seen = []
        evil, ep = await _recorder(seen)
        good, gp = await _redirector(lambda: f"http://localhost:{ep}/x")
        t = HTTPTransport(f"http://127.0.0.1:{gp}/mcp", {})
        await t.connect()
        t.session_id = "sess-abc123"
        with pytest.raises(MCPError):
            await t.request("tools/list", {}, timeout=3)
        await t._client.aclose()
        evil.close(); good.close()
        return seen
    seen = asyncio.run(go())
    assert seen and not any("sess-abc123" in s for s in seen)


def test_same_origin_redirect_keeps_headers(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    async def go():
        seen, hops = [], []

        async def handle(r, w):
            data = await r.read(65536)
            head = data.split(b"\r\n\r\n")[0].decode()
            if not hops:
                hops.append(1)
                w.write(b"HTTP/1.1 307 x\r\nLocation: /mcp/\r\nContent-Length: 0\r\n\r\n")
            else:
                seen.append(head)
                w.write(b"HTTP/1.1 500 x\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
            w.close()
        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        c = MCPClient("t", {"url": f"http://127.0.0.1:{port}/mcp", "headers": HEADERS, "timeout": 3})
        with pytest.raises(MCPError):
            await c.connect()
        await c.close()
        srv.close()
        return seen
    seen = asyncio.run(go())
    assert seen and "X-Api-Key: SECRET123" in seen[0] and "x-custom: c" in seen[0].lower()


@pytest.mark.parametrize("a,b,same", [
    ("http://h/mcp", "http://h:80/x", True),
    ("https://h/mcp", "https://h:443/x", True),
    ("https://h/mcp", "http://h/x", False),   # downgrade
    ("http://h:8080/mcp", "http://h:8081/x", False),
    ("http://h/mcp", "http://h.evil/x", False),
])
def test_origin_compare(a, b, same):
    t = HTTPTransport(a, {})
    assert (t._origin(a) == t._origin(b)) is same


# ------------------------------------------------------------------ evolve plain-copy clone
# A node deployed from a release zip (git archive) is not a git checkout, so every proposal clone
# is the copytree fallback. With data_dir inside the repo under any name but "data", the copy
# recursed into its own destination (state/evolve/wt-x/state/evolve/wt-x/...) until ENAMETOOLONG,
# left the half-made tree behind, copied every agent's workspace files, and ran on the event loop.
import os  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402


def _evolve_core(tmp_path, monkeypatch):
    from avrone.codex import Codex
    from avrone.config import Settings
    from avrone.sovereign import LocalRunner
    from avrone.tools_extra import register_extra_tools
    from avrone.trinity import TrinityCore
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "m.py").write_text("def f():\n    return 1\n")
    (repo / "workspace" / "agents" / "a").mkdir(parents=True)
    (repo / "workspace" / "agents" / "a" / "secret.txt").write_text("agent file")
    os.mkfifo(repo / "workspace" / "agents" / "a" / "pipe")
    os.mkfifo(repo / "pkg" / "stray.fifo")
    monkeypatch.setenv("AVRONE_REPO_DIR", str(repo))
    monkeypatch.setenv("AVRONE_EVOLVE_TEST_CMD", "true")
    monkeypatch.delenv("AVRONE_SELF_EVOLVE", raising=False)

    class L:
        async def chat(self, m, **k):
            return '{"final":"ok"}'
    s = Settings(); s.data_dir = repo / "state"; s.workspace = repo / "workspace"
    core = TrinityCore(s, L(), LocalRunner(s), Codex(s.data_dir))
    register_extra_tools(core.tools)
    return core, repo


def test_plain_copy_clone_skips_data_workspace_and_fifos(tmp_path, monkeypatch):
    from avrone.trinity import AgentContext
    core, repo = _evolve_core(tmp_path, monkeypatch)
    ctx = AgentContext(core=core, run_id="e", name="Ev", depth=0)
    out = asyncio.run(asyncio.wait_for(core.tools.call(ctx, "evolve_propose", {
        "goal": "g", "edits": [{"path": "pkg/m.py", "content": "def f():\n    return 2\n"}]}), 60))
    assert "evolve_diff" in out, out[:300]
    clones = list((repo / "state" / "evolve").glob("wt-*"))
    assert len(clones) == 1
    wt = clones[0]
    assert (wt / "pkg" / "m.py").read_text().endswith("return 2\n")
    assert not (wt / "state").exists() and not (wt / "workspace").exists()
    assert not (wt / "pkg" / "stray.fifo").exists()


def test_copy_tree_failure_removes_half_made_clone(tmp_path, monkeypatch):
    from avrone import evolve
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"

    def partial(a, b, **kw):
        Path(b, "half").mkdir(parents=True)
        raise evolve.shutil.Error([("x", "y", "File name too long")])
    monkeypatch.setattr(evolve.shutil, "copytree", partial)
    with pytest.raises(evolve.shutil.Error):
        evolve._copy_tree(src, dst, set())
    assert not dst.exists()


def test_plain_copy_clone_keeps_the_loop_responsive(tmp_path, monkeypatch):
    from avrone import evolve
    from avrone.trinity import AgentContext
    core, repo = _evolve_core(tmp_path, monkeypatch)
    for i in range(3000):
        (repo / "pkg" / f"f{i}.py").write_text("x = 1\n" * 50)
    ctx = AgentContext(core=core, run_id="e", name="Ev", depth=0)

    async def go():
        gaps, last, stop = [], time.monotonic(), False

        async def tick():
            nonlocal last
            while not stop:
                await asyncio.sleep(0.01)
                n = time.monotonic(); gaps.append(n - last); last = n
        t = asyncio.create_task(tick())
        await asyncio.sleep(0.03)
        await evolve._ensure_worktree(ctx, "p1")
        stop = True
        await asyncio.sleep(0.02)
        t.cancel()
        return max(gaps)
    assert asyncio.run(go()) < 0.25
