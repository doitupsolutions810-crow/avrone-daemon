"""Three real AVRONE servers: root <- router <- edge. Remote spawn routed root -> router -> edge."""
import json, os, socket, subprocess, sys, time
import httpx, pytest

pytestmark = pytest.mark.integration  # spins real uvicorn nodes; excluded from the fast gate
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

TOKEN = "test-fleet-secret"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def start(tmp, nid, port, parent=None):
    env = {**os.environ, "AVRONE_LLM_MODE": "mock", "AVRONE_RUNNER": "local", "AVRONE_FLEET_TOKEN": TOKEN,
           "AVRONE_NODE_ID": nid, "AVRONE_PUBLIC_URL": f"http://127.0.0.1:{port}", "AVRONE_FLEET_HEARTBEAT": "0.5",
           "AVRONE_WORKSPACE": str(tmp / nid / "ws"), "AVRONE_DATA_DIR": str(tmp / nid / "d"),
           "AVRONE_ADMIN_TOKEN": "adm", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", "AVRONE_PLUGINS_DIR": str(tmp / "np"), "AVRONE_MCP_CONFIG": str(tmp / "none.json")}
    if parent:
        env["AVRONE_FLEET_PARENT"] = f"http://127.0.0.1:{parent}"
    return subprocess.Popen([sys.executable, "-m", "uvicorn", "avrone.server:app", "--port", str(port), "--log-level", "warning"],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def wait(url, cond=lambda r: r.status_code < 500, t=20, **kw):
    end = time.time() + t
    while time.time() < end:
        try:
            r = httpx.get(url, timeout=2, **kw)
            if cond(r):
                return r
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting for {url}")


@pytest.fixture()
def fleet(tmp_path):
    pr, pa, pb = free_port(), free_port(), free_port()
    procs = [start(tmp_path, "root", pr)]
    wait(f"http://127.0.0.1:{pr}/api/status")
    procs.append(start(tmp_path, "router", pa, pr)); wait(f"http://127.0.0.1:{pa}/api/status")
    procs.append(start(tmp_path, "edge", pb, pa)); wait(f"http://127.0.0.1:{pb}/api/status")
    yield pr, pa, pb
    for p in reversed(procs):
        try:
            if p.poll() is None:
                try:
                    p.terminate()
                except ProcessLookupError:
                    pass
            # communicate() drains and closes stderr=PIPE instead of leaving
            # BufferedReader handles for Python's finalizer to warn about.
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
            except ProcessLookupError:
                pass
            p.communicate(timeout=5)
        finally:
            if p.stderr is not None and not p.stderr.closed:
                p.stderr.close()


def signed_post(port, path, payload, token=TOKEN):
    from avrone.fleet import sign
    body = json.dumps(payload).encode()
    return httpx.post(f"http://127.0.0.1:{port}{path}", content=body, timeout=60,
                      headers={"content-type": "application/json", **sign(token, "POST", path, body)})


def test_fleet_links_and_routes(fleet):
    pr, pa, pb = fleet
    h = {"x-admin-token": "adm"}
    r = wait(f"http://127.0.0.1:{pr}/api/fleet", lambda r: r.status_code == 200 and
             any(n["node_id"] == "router" and "edge" in n["children"] for n in r.json()["nodes"]), headers=h)
    assert r.json()["node_id"] == "root"
    # root -> router -> edge, chat on the edge
    res = signed_post(pr, "/api/fleet/run", {"route": ["router", "edge"], "mode": "chat", "prompt": "hi", "hops": 0})
    assert res.status_code == 200, res.text
    assert res.json()["path"] == ["root", "router", "edge"] and res.json()["node"] == "edge"
    # system map on root shows the tree
    m = httpx.get(f"http://127.0.0.1:{pr}/api/map").json()
    ids = {n["id"] for n in m["nodes"]}
    assert {"fleet", "node:router", "node:edge"} <= ids
    # agents on root got the fleet tools
    tools = {t["name"] for t in httpx.get(f"http://127.0.0.1:{pr}/api/tools").json()}
    assert {"fleet_nodes", "remote_spawn", "remote_broadcast"} <= tools


def test_fleet_security(fleet):
    pr, pa, pb = fleet
    assert signed_post(pa, "/api/fleet/run", {"mode": "chat", "prompt": "x"}, token="wrong").status_code == 401
    from avrone.fleet import sign
    body = b'{"mode":"chat","prompt":"x","hops":0}'
    hdr = {"content-type": "application/json", **sign(TOKEN, "POST", "/api/fleet/run", body)}
    assert httpx.post(f"http://127.0.0.1:{pb}/api/fleet/run", content=body, headers=hdr, timeout=30).status_code == 200
    assert httpx.post(f"http://127.0.0.1:{pb}/api/fleet/run", content=body, headers=hdr, timeout=30).status_code == 401  # replay
    old = sign(TOKEN, "POST", "/api/fleet/run", body, ts=str(int(time.time()) - 600))
    assert httpx.post(f"http://127.0.0.1:{pb}/api/fleet/run", content=body, headers={**hdr, **old}, timeout=30).status_code == 401
    # hop limit
    assert signed_post(pb, "/api/fleet/run", {"mode": "chat", "prompt": "x", "hops": 9}).status_code == 403
    # unknown route
    assert signed_post(pr, "/api/fleet/run", {"route": ["nope"], "mode": "chat", "prompt": "x", "hops": 0}).status_code == 502


def test_directory_and_tell(fleet):
    pr, pa, pb = fleet
    h = {"x-admin-token": "adm"}
    r = wait(f"http://127.0.0.1:{pr}/api/fleet/directory",
             lambda r: r.status_code == 200 and any(x["route"] == "router/edge" and x["env"] for x in r.json()), headers=h)
    rows = {x["route"]: x for x in r.json()}
    edge = rows["router/edge"]
    assert edge["node_id"] == "edge" and edge["env"]["device"]["hostname"]
    assert edge["env"]["runner"]["kind"] == "local" and edge["env"]["networks"]
    assert edge["env"]["services"]["public_url"].endswith(str(pb))
    # tell an agent on the furthest node to build, routed root -> router -> edge
    res = httpx.post(f"http://127.0.0.1:{pr}/api/fleet/tell", headers=h, timeout=60,
                     json={"agent": "router/edge/Builder-1", "message": "build hello.txt", "wait": True})
    assert res.status_code == 200, res.text
    j = res.json()
    assert j["path"] == ["root", "router", "edge"] and j["delivered"] == "respawned" and "answer" in j
    # ambiguous / unknown names are refused clearly
    assert httpx.post(f"http://127.0.0.1:{pr}/api/fleet/tell", headers=h, timeout=10,
                      json={"agent": "Ghost-99", "message": "x"}).status_code == 404
