import asyncio
import gzip
import io
import tarfile
import time
from types import SimpleNamespace as S
from unittest.mock import MagicMock

import pytest

from avrone import agentpack, agentfs
from avrone import fleet as fleet_mod
from avrone import tools_packpeer as tp
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools


def run(c):
    return asyncio.run(c)


def test_registered_read_only():
    r = builtin_registry()
    register_extra_tools(r)
    from avrone.tools_statehealth import ACTING
    for n in ("pack_list", "peer_audit"):
        assert r.get(n) and n not in ACTING


def _ctx(tmp_path, fleet=None):
    return S(core=S(settings=S(data_dir=tmp_path / "data", workspace=tmp_path / "ws"), fleet=fleet))


def test_pack_list_empty(tmp_path):
    assert "no agent packs yet" in run(tp.t_pack_list(_ctx(tmp_path), {}))
    agentpack.pack_dir(tmp_path / "data").mkdir(parents=True)
    assert "no agent packs yet" in run(tp.t_pack_list(_ctx(tmp_path), {}))


def test_pack_list_verifies_good_and_bad(tmp_path):
    ws, data = tmp_path / "ws", tmp_path / "data"
    home = agentfs.agent_dir(ws, "a1") if hasattr(agentfs, "agent_dir") else None
    try:
        bundle, man = agentpack.export_pack(ws, data, "a1", "node-1", home="node-1")
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"export unavailable in this layout: {e}")
    d = agentpack.pack_dir(data)
    d.mkdir(parents=True, exist_ok=True)
    (d / "a1-1.tar.gz").write_bytes(bundle)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        ti = tarfile.TarInfo("evil")
        ti.size = 1
        tf.addfile(ti, io.BytesIO(b"x"))
    (d / "a1-2.tar.gz").write_bytes(gzip.compress(raw.getvalue()))
    (d / "a1-3.tar.gz").symlink_to(d / "a1-1.tar.gz")
    (d / ".a1-4.tar.gz.tmp").write_bytes(b"x")
    out = run(tp.t_pack_list(_ctx(tmp_path), {}))
    assert "3 pack(s)" in out and "a1-1.tar.gz" in out and ": OK - agent a1 from node-1" in out
    assert "a1-2.tar.gz" in out and "REFUSED - unexpected member" in out
    assert "a1-3.tar.gz: REFUSED - symlink" in out and "2 pack(s) would be refused" in out and ".tmp" not in out


def test_pack_list_caps_and_unreadable(tmp_path, monkeypatch):
    d = agentpack.pack_dir(tmp_path / "data")
    d.mkdir(parents=True)
    monkeypatch.setattr(tp, "MAX_LIST", 2)
    for i in range(3):
        (d / f"a-{i}.tar.gz").write_bytes(b"junk")
    monkeypatch.setattr(tp.Path, "read_bytes", lambda self: (_ for _ in ()).throw(PermissionError("no")))
    # round 86: packs are read through agentpack.read_pack_file (O_NOFOLLOW, capped)
    monkeypatch.setattr(agentpack, "read_pack_file", lambda p: (_ for _ in ()).throw(PermissionError("no")))
    out = run(tp.t_pack_list(_ctx(tmp_path), {}))
    assert "first 2 shown" in out and "UNREADABLE - PermissionError" in out


@pytest.fixture
def fm(tmp_path, monkeypatch):
    for k in ("AVRONE_FLEET_MAX_HOPS", "AVRONE_FLEET_PARENT", "AVRONE_PUBLIC_URL", "AVRONE_FLEET_ALLOW",
              "AVRONE_FLEET_REQUIRE_ALLOW"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "tok")
    monkeypatch.setenv("AVRONE_NODE_ID", "root")
    monkeypatch.setattr(fleet_mod, "HTTP_TRANSPORT", None)
    c = MagicMock()
    c.active = {}
    return fleet_mod.FleetManager(c, tmp_path)


def test_peer_audit(fm, tmp_path):
    ctx = _ctx(tmp_path, fm)
    assert run(tp.t_peer_audit(ctx, {})) == "peer_audit: no registered peers"
    fm.register({"node_id": "good", "url": "http://10.0.0.2:8710"})
    fm.nodes["old"] = {"node_id": "old", "url": "http://169.254.169.254", "last_seen": 0}
    fm.nodes["weird"] = "junk"
    out = run(tp.t_peer_audit(ctx, {}))
    assert "3 peer(s), 2 with a url" in out and "- good [online]: ok" in out
    assert "- old [offline]: BAD URL - url points at a cloud metadata endpoint" in out
    assert "169.254" not in out.split("BAD URL")[0] and "re-register" in out
    assert "disabled" in run(tp.t_peer_audit(_ctx(tmp_path, None), {}))


def test_peer_audit_status_error(tmp_path):
    f = S(enabled=True, nodes={"n": {"url": "http://a.test"}}, status_of=lambda n: 1 / 0)
    assert "- n [unknown]: ok" in run(tp.t_peer_audit(_ctx(tmp_path, f), {}))


def test_post_refuses_saved_bad_url(fm):
    with pytest.raises(RuntimeError, match="refusing to call peer url"):
        run(fm._post("http://169.254.169.254", "/api/fleet/run", {}))


def test_fleet_forget(fm, tmp_path, monkeypatch):
    from avrone.tools_statehealth import ACTING
    assert "fleet_forget" in ACTING
    ctx = _ctx(tmp_path, fm)
    fm.register({"node_id": "live", "url": "http://10.0.0.2:8710"})
    fm.register({"node_id": "gone", "url": "http://10.0.0.3:8710"})
    fm.nodes["gone"]["last_seen"] = 0
    out = run(tp.t_fleet_forget(ctx, {"node": "live"}))
    assert out.startswith("ERROR:") and "rejoin" in out and "live" in fm.nodes
    assert run(tp.t_fleet_forget(ctx, {"node": "gone"})) == "forgot peer gone (was offline)"
    assert "gone" not in fm.nodes and "gone" not in (tmp_path / "fleet.json").read_text()
    assert "no peer 'nope'" in run(tp.t_fleet_forget(ctx, {"node": "nope"}))
    assert "invalid node_id" in run(tp.t_fleet_forget(ctx, {"node": "Bad ID"}))
    assert "node is required" in run(tp.t_fleet_forget(ctx, {}))
    assert "fleet disabled" in run(tp.t_fleet_forget(_ctx(tmp_path, None), {"node": "x"}))
    fm.allow = {"someone-else"}  # live is no longer allowed: removal sticks even while online
    out = run(tp.t_fleet_forget(ctx, {"node": "live"}))
    assert out == "forgot peer live (was online) (revoked in AVRONE_FLEET_ALLOW, so it cannot rejoin)"
    kinds = [c.args[1] for c in fm.core.codex.emit.call_args_list]
    assert kinds.count("fleet.forget") == 2

