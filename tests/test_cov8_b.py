"""Squad round 8 coverage lane, part B: tools_extra, agentpack and server edge branches.

Offline only: outbound HTTP goes through httpx.MockTransport, fleet methods are faked on the
live server's fleet object, and nothing reaches a real host.
"""
import asyncio
import gzip
import io
import json
import os
import runpy
import socket
import tarfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from avrone import agentpack as ap
from avrone import tools_extra as tx
from avrone.codex import Codex, Event
from avrone.config import Settings
from avrone.fleet import sign
from avrone.sovereign import LocalRunner
from avrone.tools import ToolRegistry


def run(coro):
    return asyncio.run(coro)


# ============================================================================ tools_extra
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
    return SimpleNamespace(s=s, ws=Path(s.workspace).resolve(), codex=codex, ctx=ctx, reg=reg, tmp=tmp_path)


def call(env, _tool, **args):
    return run(env.reg.call(env.ctx, _tool, args))


def test_rel_outside_workspace_returns_raw_path(env, tmp_path):
    other = tmp_path / "elsewhere.txt"
    assert tx._rel(env.ctx, other) == str(other)
    assert tx._rel(env.ctx, env.ws / "a" / "b") == "a/b"


def test_emit_swallows_codex_failures(env):
    class Bad:
        def emit(self, *a, **k):
            raise RuntimeError("disk full")
    env.ctx.core.codex = Bad()
    tx._emit(env.ctx, "x", a=1)  # must not raise
    assert call(env, "think", thought="still fine") == "noted"


def test_parse_ddg_skips_hrefless_links_and_ads():
    html = ('<a class="result__a">no href</a>'
            '<a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a>'
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Freal.example%2F">Real</a>'
            '<a class="result__snippet">snip</a>')
    res = tx.parse_ddg_html(html, 5)
    assert [r["url"] for r in res] == ["https://real.example/"]
    assert res[0]["title"] == "Real"


def test_web_search_argument_and_key_errors(env, monkeypatch):
    assert call(env, "web_search", query="   ") == "ERROR: query is required"
    monkeypatch.setenv("AVRONE_SEARCH_PROVIDER", "brave")
    assert "BRAVE_API_KEY is not set" in call(env, "web_search", query="x")
    monkeypatch.setenv("AVRONE_SEARCH_PROVIDER", "tavily")
    assert "TAVILY_API_KEY is not set" in call(env, "web_search", query="x")


def test_web_search_no_results_message(env, monkeypatch):
    monkeypatch.setenv("AVRONE_SEARCH_PROVIDER", "somethingelse")  # falls back to duckduckgo
    monkeypatch.setattr(tx, "HTTP_TRANSPORT",
                        httpx.MockTransport(lambda req: httpx.Response(200, text="<html>nothing</html>")))
    assert call(env, "web_search", query="obscure") == "No results for 'obscure' (duckduckgo)."


def test_python_requires_code(env):
    assert call(env, "python", code="   ") == "ERROR: code is required"


def test_list_files_iterdir_oserror_dangling_symlink_and_cap(env, monkeypatch):
    root = env.ws / "tree"
    (root / "locked").mkdir(parents=True)
    (root / "dangling").symlink_to(env.ws / "missing-target")
    for i in range(405):
        (root / f"f{i:03d}").write_text("x")
    out = call(env, "list_files", path="tree", depth=1)
    assert "...[truncated at 400 entries]" in out
    assert out.count("\n") >= 400

    real_iterdir = Path.iterdir

    def iterdir(self):
        if self.name == "locked":
            raise PermissionError("denied")
        return real_iterdir(self)
    monkeypatch.setattr(Path, "iterdir", iterdir)
    import os as _os
    real_scandir = _os.scandir

    def scandir(p="."):  # round 89: list_files reads directories with os.scandir
        if Path(p).name == "locked":
            raise PermissionError("denied")
        return real_scandir(p)
    monkeypatch.setattr(_os, "scandir", scandir)
    small = env.ws / "small"
    (small / "locked").mkdir(parents=True)
    (small / "dangling").symlink_to(env.ws / "nowhere")
    out = call(env, "list_files", path="small", depth=3)
    # round 89: a link is listed as "(symlink)"; its target's size is never shown
    assert "locked/" in out and "dangling (symlink)" in out


def test_search_files_required_pattern_and_skips(env):
    assert call(env, "search_files", pattern="") == "ERROR: pattern is required"
    d = env.ws / "s"
    d.mkdir(parents=True)
    (d / "big.txt").write_bytes(b"needle\n" * 200_000)  # > 1 MB: skipped
    (d / "dangling.txt").symlink_to(env.ws / "gone")     # stat fails: skipped
    (d / "ok.txt").write_text("a needle here\n")
    out = call(env, "search_files", pattern="needle", path="s")
    assert out.strip().endswith(": a needle here") and "big.txt" not in out


def test_search_files_caps_within_one_file_and_across_files(env):
    one = env.ws / "one"
    one.mkdir(parents=True)
    (one / "many.txt").write_text("hit\n" * 250)
    out = call(env, "search_files", pattern="hit", path="one")
    assert out.endswith("...[capped at 200 matches]")
    assert out.count("many.txt:") == 200
    many = env.ws / "many"
    many.mkdir()
    for i in range(205):
        (many / f"f{i:03d}.txt").write_text("hit\n")
    out = call(env, "search_files", pattern="hit", path="many")
    assert out.endswith("...[capped at 200 matches]") and "f204.txt" not in out


def test_edit_file_requires_old(env):
    (env.ws / "e.txt").write_text("abc")
    assert call(env, "edit_file", path="e.txt", old="", new="x") == "ERROR: 'old' must be a non-empty string"


def test_http_request_header_validation_and_string_body(env, monkeypatch):
    assert call(env, "http_request", url="https://x.example/", headers=["a"]) == "ERROR: headers must be an object"
    seen = {}

    def handler(req):
        seen["body"] = req.content
        seen["ua"] = req.headers.get("user-agent")
        return httpx.Response(201, text="made", headers={"location": "/new"})
    monkeypatch.setattr(tx, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    out = call(env, "http_request", method="post", url="https://x.example/items", body=12345)
    assert out.startswith("HTTP 201") and "location: /new" in out and out.endswith("made")
    assert seen["body"] == b"12345" and seen["ua"] == "AVRONE/0.2"


def test_force_push_detection_variants():
    assert tx._is_force_push(["push", "-uf", "origin", "main"]) is True
    assert tx._is_force_push(["push", "origin", "main"]) is False
    assert tx._is_force_push(["push", "-u", "origin", "main"]) is False
    assert tx._is_force_push(["status"]) is False


def test_git_parse_errors_and_empty(env):
    assert call(env, "git", args='log "unterminated').startswith("ERROR: could not parse args")
    assert call(env, "git", args="   ").startswith("ERROR: args is required")
    assert call(env, "git", args="git push -fu origin").startswith("ERROR: force push")


def test_load_memory_across_namespaces_newest_wins(tmp_path):
    from avrone import memstore
    p = tmp_path / "memory.jsonl"
    st = memstore.MemoryStore(p)
    st.save("alpha", "k", "old", run_id="r", agent="a")
    st.save("beta", "k", "new", run_id="r", agent="b")
    st.save("beta", "only", "v", run_id="r", agent="b")
    allm = tx.load_memory(p)
    assert allm["k"]["value"] == "new" and allm["only"]["value"] == "v"
    assert set(tx.load_memory(p, "alpha")) == {"k"}


def test_memory_recall_substring_in_value_and_empty_list(env):
    assert call(env, "memory_list") .endswith("no memories saved yet.")
    call(env, "memory_save", key="plan", value="xxqqzyy")
    call(env, "memory_save", key="other", value="nothing")
    out = call(env, "memory_recall", query="qqz")
    assert out == "- plan: xxqqzyy"


def test_artifact_save_name_errors(env):
    assert call(env, "artifact_save", name="  ", content="x") == "ERROR: name is required"
    assert call(env, "artifact_save", name=".", content="x") == "ERROR: bad artifact name"


def test_docker_run_requires_cmd(env, monkeypatch):
    monkeypatch.setattr(tx.shutil, "which", lambda name: "/usr/bin/docker")
    assert call(env, "docker_run", image="alpine", cmd="  ") == "ERROR: cmd is required"
    assert call(env, "docker_run", image="-bad", cmd="ls") == "ERROR: invalid image name"


def test_codex_search_missing_dir_symlink_run_and_unreadable_file(env, tmp_path):
    env.ctx.core.codex = SimpleNamespace(log_dir=tmp_path / "no-such-dir")
    assert call(env, "codex_search", kind="x") == "No CodeX runs logged yet."
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    env.ctx.core.codex = SimpleNamespace(log_dir=log_dir)
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"kind": "x"}) + "\n")
    (log_dir / "linked.jsonl").symlink_to(outside)
    assert call(env, "codex_search", run_id="linked") == "ERROR: invalid run_id"
    (log_dir / "linked.jsonl").unlink()
    (log_dir / "adir.jsonl").mkdir()  # not a log file: skipped (round 73 no longer counts it as a run)
    (log_dir / "good.jsonl").write_text(json.dumps({"kind": "x", "agent": "A", "ts": 5, "data": {}}) + "\n")
    out = call(env, "codex_search", kind="x")
    assert out.startswith("1 matching event(s) in 1 run(s)") and "[good]" in out


def test_writable_and_disk_errors(tmp_path, monkeypatch):
    assert tx._disk(tmp_path / "missing" / "deeper") is None
    assert tx._disk(tmp_path) is not None

    def boom(*a, **k):
        raise OSError("weird fs")
    monkeypatch.setattr(tx.os, "access", boom)
    assert tx._writable(tmp_path) is False


def test_self_inventory_tolerates_broken_tools_and_hostname(env, monkeypatch):
    class BadTools:
        def names(self):
            raise RuntimeError("registry broken")
    env.ctx.core.tools = BadTools()

    def no_host():
        raise OSError("no hostname")
    monkeypatch.setattr(socket, "gethostname", no_host)
    inv = tx.self_inventory(env.ctx)
    assert inv["tools"]["registered"] is None
    assert inv["system"]["hostname"] == "unknown"
    out = call(env, "self_scan")
    assert "tools registered: unknown" in out and "host=unknown" in out


def test_priv_shell_empty_argv_after_parse_is_refused(monkeypatch):
    monkeypatch.setattr(tx.shlex, "split", lambda s: [])
    assert tx.check_priv_shell_command("ls", {"ls"}) == (None, "empty", "cmd is required")


def test_priv_admin_reports_missing_fleet_and_codex(env):
    env.ctx.core.fleet_privileged = tx.PRIV_ADMIN_CAP
    env.ctx.core.fleet = None
    env.ctx.core.codex = None
    with tx.priv_grant(tx.PRIV_ADMIN_CAP):
        assert call(env, "priv_admin", action="fleet_info") == "ERROR: fleet is not available on this node"
        assert call(env, "priv_admin", action="codex_runs") == "ERROR: the Eternal CodeX log is not available"


# ============================================================================ agentpack
def _bundle(manifest: dict, memory=b"[]", home: dict | None = None, extra=()) -> bytes:
    raw = io.BytesIO()
    man = manifest if isinstance(manifest, (bytes, bytearray)) else json.dumps(manifest).encode()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        members = [(ap.MANIFEST, man), (ap.MEMORY, memory)] + [(ap.HOME_PREFIX + k, v) for k, v in (home or {}).items()]
        members += list(extra)
        for name, data in members:
            if data is None:
                continue
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def _manifest(files=None, memory=b"[]", **over):
    m = {"format": ap.FORMAT, "agent_id": "bob", "origin": "nodea", "home": "nodea", "created": 100,
         "total_bytes": 0, "files": files or [], "memory": {"sha256": ap._sha(memory)}}
    m.update(over)
    return m


def _entry(rel, data, **over):
    e = {"path": rel, "size": len(data), "sha256": ap._sha(data), "mtime": 50}
    e.update(over)
    return e


def test_read_pack_valid_baseline():
    data = b"hello"
    man, mem, blobs = ap.read_pack(_bundle(_manifest([_entry("a.txt", data)]), home={"a.txt": data}))
    assert man["agent_id"] == "bob" and mem == [] and blobs == {"a.txt": data}


@pytest.mark.parametrize("mutate,msg", [
    ({"format": "other/1"}, "unknown pack format"),
    ({"agent_id": "Bad/Id"}, "bad agent_id"),
    ({"origin": "UPPER"}, "bad origin"),
    ({"home": 5}, "bad home"),
    ({"files": "nope"}, "bad files list"),
    ({"files": ["x"]}, "bad file entry"),
    ({"files": [{"path": 3}]}, "bad file entry"),
])
def test_read_pack_manifest_refusals(mutate, msg):
    with pytest.raises(ap.PackError, match=msg):
        ap.read_pack(_bundle(_manifest(**mutate)))


@pytest.mark.parametrize("mtime", [True, "5", 2**41])
def test_read_pack_bad_mtime(mtime):
    data = b"x"
    with pytest.raises(ap.PackError, match="bad mtime"):
        ap.read_pack(_bundle(_manifest([_entry("a", data, mtime=mtime)]), home={"a": data}))


def test_read_pack_missing_manifest_or_memory():
    with pytest.raises(ap.PackError, match="no manifest/memory"):
        ap.read_pack(_bundle(_manifest(), memory=None))


def test_read_pack_duplicate_home_member():
    data = b"x"
    b = _bundle(_manifest([_entry("a", data)]), home={"a": data}, extra=[(ap.HOME_PREFIX + "a", data)])
    with pytest.raises(ap.PackError, match="duplicate member 'a'"):
        ap.read_pack(b)


def test_read_pack_memory_must_be_list():
    mem = b'{"k": 1}'
    with pytest.raises(ap.PackError, match="memory must be a list"):
        ap.read_pack(_bundle(_manifest(memory=mem), memory=mem))


def test_read_pack_bundle_and_total_over_quota(monkeypatch):
    monkeypatch.setattr(ap, "quota_bytes", lambda: 0)
    with pytest.raises(ap.PackError, match="over the quota"):
        ap.read_pack(b"\0" * (1024 * 1024 + 1))
    monkeypatch.setattr(ap, "quota_bytes", lambda: 3)
    data = b"0123456789"
    with pytest.raises(ap.PackError, match="over the agent-fs quota"):
        ap.read_pack(_bundle(_manifest([_entry("a", data)]), home={"a": data}))


def test_content_stamp_skips_unstattable_files(tmp_path, monkeypatch):
    from avrone import agentfs
    d = agentfs.agent_dir(tmp_path / "ws", "bob")
    (d / "good").write_text("g")
    (d / "bad").write_text("b")
    os.utime(d / "good", (1000, 1000))
    os.utime(d / "bad", (999999, 999999))
    real = os.lstat

    def lstat(p, *a, **k):
        if str(p).endswith(os.sep + "bad"):
            raise OSError("vanished")
        return real(p, *a, **k)
    monkeypatch.setattr(ap.os, "lstat", lstat)
    assert ap.content_stamp(tmp_path / "ws", tmp_path / "data", "bob") == 1000


def test_export_refuses_symlinked_dir_and_changed_file(tmp_path, monkeypatch):
    from avrone import agentfs
    ws = tmp_path / "ws"
    root = agentfs.agents_root(ws)
    root.mkdir(parents=True)
    (tmp_path / "real").mkdir()
    (root / "linky").symlink_to(tmp_path / "real")
    with pytest.raises(ap.PackError, match="must not be a symlink"):
        ap.export_pack(ws, tmp_path / "data", "linky", "nodea")
    d = agentfs.agent_dir(ws, "bob")
    (d / "f.txt").write_text("abc")
    real_walk = ap._walk

    def walk(r):
        return [(rel, p, SimpleNamespace(st_size=st.st_size + 1, st_mtime=st.st_mtime)) for rel, p, st in real_walk(r)]
    monkeypatch.setattr(ap, "_walk", walk)
    with pytest.raises(ap.PackError, match="changed while packing"):
        ap.export_pack(ws, tmp_path / "data", "bob", "nodea")


def test_install_refuses_symlinked_root_and_target(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "elsewhere").mkdir()
    root = ws / "agents"
    root.symlink_to(tmp_path / "elsewhere")
    man = _manifest()
    with pytest.raises(ap.PackError, match="agents root must not be a symlink"):
        ap._install(root, ws, tmp_path / "data", man, {}, "bob", "nodea")
    root.unlink()
    root.mkdir()
    (root / "bob").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ap.PackError, match="agent dir must not be a symlink"):
        ap._install(root, ws, tmp_path / "data", man, {}, "bob", "nodea")


def test_install_refuses_escaping_blob_and_cleans_temp(tmp_path):
    ws = tmp_path / "ws"
    root = ws / "agents"
    man = _manifest([{"path": "../evil", "size": 1, "mtime": 1}])
    with pytest.raises(ap.PackError, match="escapes the agent dir"):
        ap._install(root, ws, tmp_path / "data", man, {"../evil": b"x"}, "bob", "nodea")
    assert not (root / "evil").exists()
    assert [p.name for p in root.iterdir()] == []  # temp dir removed


def test_install_rolls_back_old_home_when_swap_fails(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    data_dir = tmp_path / "data"
    root = ws / "agents"
    (root / "bob").mkdir(parents=True)
    (root / "bob" / "keep.txt").write_text("original")
    os.utime(root / "bob" / "keep.txt", (10, 10))
    payload = b"new"
    man = _manifest([_entry("n.txt", payload)], created=1000)
    real_replace = os.replace
    calls = {"n": 0}

    def replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # the tmp -> target swap
            raise OSError("swap failed")
        return real_replace(src, dst)
    monkeypatch.setattr(ap.os, "replace", replace)
    with pytest.raises(OSError, match="swap failed"):
        ap._install(root, ws, data_dir, man, {"n.txt": payload}, "bob", "nodea")
    monkeypatch.setattr(ap.os, "replace", real_replace)
    assert (root / "bob" / "keep.txt").read_text() == "original"
    assert sorted(p.name for p in root.iterdir()) == ["bob"]


# ============================================================================ server
@pytest.fixture()
def client(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


@pytest.fixture()
def fclient(tmp_path, monkeypatch):
    from avrone import server
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "fleet-secret")
    monkeypatch.setenv("AVRONE_NODE_ID", "hub")
    for k in ("AVRONE_FLEET_PARENT", "AVRONE_FLEET_ALLOW", "AVRONE_PUBLIC_URL", "AVRONE_FLEET_MAX_HOPS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app) as c:
        yield c


def _post(c, path, payload):
    body = json.dumps(payload).encode()
    h = {"content-type": "application/json", **sign("fleet-secret", "POST", path, body)}
    return c.post(path, content=body, headers=h)


def _raiser(exc, is_async=False):
    if is_async:
        async def f(*a, **k):
            raise exc
    else:
        def f(*a, **k):
            raise exc
    return f


@pytest.fixture()
def fleet(fclient, monkeypatch):
    from avrone import server
    f = server._fleet()
    monkeypatch.setattr(f, "dispatch_gateway", lambda kind, *a, **k: {"kind": kind})
    return f


def test_fleet_message_permission_error_is_403(fclient, fleet, monkeypatch):
    monkeypatch.setattr(fleet, "handle_message", _raiser(PermissionError("no"), True))
    r = _post(fclient, "/api/fleet/message", {})
    assert r.status_code == 403 and r.json()["detail"] == "no"


def test_fleet_memory_permission_error_is_403(fclient, fleet, monkeypatch):
    monkeypatch.setattr(fleet, "receive_memory", _raiser(PermissionError("cap missing")))
    assert _post(fclient, "/api/fleet/memory", {}).status_code == 403


@pytest.mark.parametrize("exc,code", [(PermissionError("p"), 403), (ValueError("v"), 400),
                                      (RuntimeError("r"), 400)])
def test_fleet_agentpack_error_mapping(fclient, fleet, monkeypatch, exc, code):
    monkeypatch.setattr(fleet, "receive_agentpack", _raiser(exc))
    r = _post(fclient, "/api/fleet/agentpack", {})
    assert r.status_code == code and r.json()["detail"] == str(exc)


def test_fleet_agentpack_success_passes_payload(fclient, fleet, monkeypatch):
    seen = {}

    def recv(payload):
        seen.update(payload)
        return {"agent_id": "bob", "files": 0}
    monkeypatch.setattr(fleet, "receive_agentpack", recv)
    r = _post(fclient, "/api/fleet/agentpack", {})
    assert r.status_code == 200 and r.json() == {"agent_id": "bob", "files": 0}
    assert seen == {"kind": "agentpack"}


def test_fleet_run_permission_error_is_403(fclient, fleet, monkeypatch):
    monkeypatch.setattr(fleet, "handle_run", _raiser(PermissionError("not privileged"), True))
    assert _post(fclient, "/api/fleet/run", {}).status_code == 403


def test_chat_job_construction_failure_releases_checkpoint(client, monkeypatch):
    from avrone import server
    c = server.core()
    real = c.squad
    monkeypatch.setattr(c, "squad", _raiser(RuntimeError("cannot build job")))
    with pytest.raises(RuntimeError, match="cannot build job"):
        client.post("/api/chat", json={"message": "task", "mode": "squad", "squad_size": 2,
                                       "checkpoint_id": "rel-1"})
    monkeypatch.setattr(c, "squad", real)
    r = client.post("/api/chat", json={"message": "task", "mode": "squad", "squad_size": 2,
                                       "checkpoint_id": "rel-1"})
    assert r.status_code == 200, r.text  # not 409: the failed attempt released its claim


def test_events_websocket_disconnect_unsubscribes(monkeypatch):
    from fastapi import WebSocketDisconnect

    from avrone import server
    state = {}

    class FakeCodex:
        def subscribe(self):
            q = asyncio.Queue()
            q.put_nowait(Event(ts=1.0, run_id="r", kind="k", agent="a", data={}))
            state["q"] = q
            return q

        def unsubscribe(self, q):
            state["unsub"] = q

    class FakeWS:
        headers, query_params, client = {}, {}, SimpleNamespace(host="127.0.0.1")

        async def accept(self):
            state["accepted"] = True

        async def send_json(self, data):
            raise WebSocketDisconnect(1000)

        send_text = send_json  # round 34: the stream sends strict JSON text

        async def receive(self):  # v0.30: the handler also watches for the client leaving
            await asyncio.sleep(3600)
    fake_core = SimpleNamespace(codex=FakeCodex())
    monkeypatch.setattr(server, "core", lambda: fake_core)
    run(server.events(FakeWS()))
    assert state["accepted"] and state["unsub"] is state["q"]


def test_server_main_entrypoint_uses_env(monkeypatch):
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: seen.update(app=app, host=host, port=port))
    monkeypatch.setenv("AVRONE_HOST", "0.0.0.0")
    monkeypatch.setenv("AVRONE_PORT", "9999")
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # runpy: module already imported
        runpy.run_module("avrone.server", run_name="__main__")
    assert seen == {"app": "avrone.server:app", "host": "0.0.0.0", "port": 9999}


# ============================================================================ fleet (cheap env / advisory edges)
def test_fleet_migrate_interval_garbage_and_nan(monkeypatch):
    from avrone import fleet
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE_INTERVAL", "soon")
    assert fleet.migrate_interval() == max(fleet.MIN_MIGRATE_INTERVAL_S, fleet.DEFAULT_MIGRATE_INTERVAL_S)
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE_INTERVAL", "nan")
    assert fleet.migrate_interval() == max(fleet.MIN_MIGRATE_INTERVAL_S, fleet.DEFAULT_MIGRATE_INTERVAL_S)
    monkeypatch.setenv("AVRONE_AGENT_MIGRATE_INTERVAL", "0.001")
    assert fleet.migrate_interval() == fleet.MIN_MIGRATE_INTERVAL_S


def test_fleet_offload_timeout_and_env_int_garbage(monkeypatch):
    from avrone import fleet
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD_TIMEOUT", "forever")
    assert fleet.offload_timeout() == 300.0
    monkeypatch.setenv("AVRONE_FLEET_OFFLOAD_TIMEOUT", "1")
    assert fleet.offload_timeout() == 5.0
    monkeypatch.setenv("AVRONE_COV8_INT", "x")
    assert fleet._env_int("AVRONE_COV8_INT", 7, 1, 10) == 7
    monkeypatch.setenv("AVRONE_COV8_INT", "99")
    assert fleet._env_int("AVRONE_COV8_INT", 7, 1, 10) == 10


def test_fleet_bridge_of_falls_back_when_run_bridge_fails(monkeypatch):
    from avrone import bridge, fleet
    monkeypatch.setattr(bridge, "for_core", _raiser(RuntimeError("no bridge")))
    assert fleet.bridge_of(SimpleNamespace(), "run1") is None


def test_fleet_health_score_non_numeric_busy_counts_as_busy():
    from avrone import fleet
    bad = fleet.health_score({"load": "lots", "health": {"headroom": 1.0, "load": 0.0}})
    idle = fleet.health_score({"load": 0, "health": {"headroom": 1.0, "load": 0.0}})
    assert bad == 80.0 and idle == 100.0


def test_fleet_privileged_max_hops_garbage_and_audit_failure(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from avrone.fleet import FleetManager
    monkeypatch.setenv("AVRONE_FLEET_PRIVILEGED_MAX_HOPS", "many")
    core = MagicMock()
    core.codex.emit.side_effect = RuntimeError("log down")
    fm = FleetManager(core, tmp_path)
    assert fm.privileged_max_hops == 1
    fm._audit("x.event", run_id="r", kind="k", agent="a")  # must not raise
    _, kwargs = core.codex.emit.call_args
    assert kwargs == {"req_run_id": "r", "req_kind": "k", "req_agent": "a"}
