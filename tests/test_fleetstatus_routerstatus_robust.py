"""Hardening round 53: tools_fleetstatus (fleet_status) and tools_routerstatus (router_status)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as S

import pytest

from avrone import fleet as fl
from avrone import gateway
from avrone import tools_fleetstatus as fs
from avrone import tools_routerstatus as rs
from avrone.config import Settings


def run(c):
    return asyncio.run(c)


KEY = "sk-routerSECRETkey1234567890"
URL = "https://secret-host.example/v1"


# ------------------------------------------------------------ router_status
def gw_ctx(upstreams=None, **attrs):
    gw = gateway.Gateway(Settings())
    if upstreams is not None:
        gw.upstreams = upstreams
    for k, v in attrs.items():
        setattr(gw, k, v)
    return S(core=S(llm=gw)), gw


def up(provider="xai", model="grok-4", key=KEY, url=URL, lead=""):
    return gateway.Upstream(provider, url, model, key, lead)


@pytest.mark.parametrize("sep", ["\u200b", "\u200c", "\u200d", "\u2060", "\ufeff", "\u00ad"])
def test_key_with_invisible_char_in_model_label_masked(sep):
    model = "m-" + KEY[:6] + sep + KEY[6:]
    ctx, _ = gw_ctx([up(model=model)])
    out = run(rs.t_router_status(ctx, {}))
    assert "routerSECRET" not in out and "m-???" in out


def test_invisible_char_inside_configured_key_masked():
    k = KEY[:5] + "\u200b" + KEY[5:]
    ctx, _ = gw_ctx([up(model="x-" + KEY, key=k)])
    assert "routerSECRET" not in run(rs.t_router_status(ctx, {}))


def test_bytes_key_masked():
    ctx, _ = gw_ctx([up(model="m-" + KEY, key=KEY.encode())])
    assert "routerSECRET" not in run(rs.t_router_status(ctx, {}))


def test_json_with_many_long_labels_stays_parseable(monkeypatch):
    monkeypatch.setattr(rs, "ROUTER_STATUS_MAX_CHARS", 2500)
    ups = [up(provider="p" * 32, model=f"{i:02d}" + "m" * 94, lead="l" * 96) for i in range(25)]
    ctx, _ = gw_ctx(ups)
    out = run(rs.t_router_status(ctx, {"format": "json"}))
    rep = json.loads(out)
    assert len(out) <= 2500 and rep["more"] >= 5


def test_json_chain_trimmed_when_rows_gone(monkeypatch):
    monkeypatch.setattr(rs, "ROUTER_STATUS_MAX_CHARS", 900)
    ctx, _ = gw_ctx([up(model=f"{i:02d}" + "m" * 94) for i in range(20)])
    out = run(rs.t_router_status(ctx, {"format": "json"}))
    rep = json.loads(out)
    assert len(out) <= 900 and rep.get("chain_truncated") is True and rep["upstreams"] == []


@pytest.mark.parametrize("fmt", [" JSON ", "Json", "json\n"])
def test_json_spellings(fmt):
    ctx, _ = gw_ctx([up()])
    json.loads(run(rs.t_router_status(ctx, {"format": fmt})))


@pytest.mark.parametrize("fmt", [["json"], 5, {"a": 1}])
def test_non_string_format_refused(fmt):
    ctx, _ = gw_ctx([up()])
    assert run(rs.t_router_status(ctx, {"format": fmt})).startswith("ERROR: format")


@pytest.mark.parametrize("bad", [5, object(), 3.5])
def test_non_iterable_upstreams(bad):
    ctx, _ = gw_ctx(bad)
    assert "no upstreams configured" in run(rs.t_router_status(ctx, {}))


def test_string_upstreams_not_split():
    ctx, _ = gw_ctx("abc")
    assert "no upstreams configured" in run(rs.t_router_status(ctx, {}))


def test_string_last_route_not_split_per_char():
    ctx, _ = gw_ctx([up()], last_route="xai:grok")
    out = run(rs.t_router_status(ctx, {}))
    assert "last route: (no calls yet)" in out


def test_last_route_tuple_ok():
    ctx, _ = gw_ctx([up()], last_route=("xai:grok-4", "ollama:x"))
    assert "last route: xai:grok-4 -> ollama:x" in run(rs.t_router_status(ctx, {}))


@pytest.mark.parametrize("url", ["http://[::1", "http://h:99999999/x", 5, None, "http://\u0000/"])
def test_route_of_never_raises(url):
    assert rs.route_of(url) in {"direct", "env-proxy"}


def test_route_of_getproxies_raising(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "getproxies", lambda: 1 / 0)
    assert rs.route_of("https://api.example.com") == "direct"


def test_plain_output_unchanged():
    ctx, _ = gw_ctx([up(), up("ollama", "llama3.1")])
    out = run(rs.t_router_status(ctx, {}))
    assert "primary: xai:grok-4" in out and "fallback chain: ollama:llama3.1" in out
    assert KEY not in out and URL not in out


# ------------------------------------------------------------ fleet_status
class Fleet:
    enabled = True
    node_id = "me"
    parent = ""
    max_hops = 0

    def __init__(self, nodes, boom=None):
        self.nodes, self.boom = nodes, boom or set()

    def offload_mode(self):
        return "off"

    def squad_dispatch(self):
        return "local"

    def status_of(self, n):
        if "status" in self.boom and n["node_id"] == "bad":
            raise RuntimeError("x")
        return "online"

    def node_state(self, n):
        if "state" in self.boom and n["node_id"] == "bad":
            raise KeyError("x")
        return "ok"

    def allowed(self, nid):
        if not isinstance(nid, str):
            raise TypeError
        return nid != "denied"

    def select_nodes(self, k, need=None):
        return []


def fctx(f):
    return S(core=S(fleet=f))


def node(nid, **kw):
    d = {"node_id": nid, "last_seen": 1.0, "load": 0.0}
    d.update(kw)
    return d


@pytest.mark.parametrize("which", ["status", "state"])
def test_one_bad_record_does_not_break_view(which):
    f = Fleet({"good": node("good"), "bad": node("bad")}, boom={which})
    out = run(fs.t_fleet_status(fctx(f), {}))
    assert "- good [" in out and "- bad [" in out and "unknown" in out


def test_health_score_raising(monkeypatch):
    monkeypatch.setattr(fl, "health_score", lambda n: 1 / 0)
    out = run(fs.t_fleet_status(fctx(Fleet({"a": node("a")})), {}))
    assert "- a [online/ok]" in out and "score=" not in out


def test_health_score_nan_is_none(monkeypatch):
    monkeypatch.setattr(fl, "health_score", lambda n: float("nan"))
    rep = json.loads(run(fs.t_fleet_status(fctx(Fleet({"a": node("a")})), {"format": "json"})))
    assert rep["nodes"][0]["score"] is None


def test_sanitize_health_raising(monkeypatch):
    monkeypatch.setattr(fl, "sanitize_health", lambda h: 1 / 0)
    assert "- a [" in run(fs.t_fleet_status(fctx(Fleet({"a": node("a")})), {}))


def test_local_health_raising(monkeypatch):
    monkeypatch.setattr(fl, "local_health", lambda core: 1 / 0)
    assert "unknown (no governor reading)" in run(fs.t_fleet_status(fctx(Fleet({})), {}))


@pytest.mark.parametrize("nodes", [["x"], "abc", 5, None])
def test_non_dict_registry(nodes):
    out = run(fs.t_fleet_status(fctx(Fleet(nodes)), {}))
    assert "known nodes: 0" in out


@pytest.mark.parametrize("inner", [None, 5, "", ["x"]])
def test_missing_inner_id_uses_registry_key(inner):
    out = run(fs.t_fleet_status(fctx(Fleet({"real-id": node(inner)})), {}))
    assert "- real-id [" in out and "None" not in out and "NOT-ALLOWED" not in out


def test_status_values_labelled():
    class F(Fleet):
        def status_of(self, n):
            return "on\nFAKE"
    out = run(fs.t_fleet_status(fctx(F({"a": node("a")})), {}))
    assert not any(l.startswith("FAKE") for l in out.splitlines())


@pytest.mark.parametrize("ls", [1e308, -1e308, 10 ** 400])
def test_odd_last_seen(ls):
    rep = json.loads(run(fs.t_fleet_status(fctx(Fleet({"a": node("a", last_seen=ls)})), {"format": "json"})))
    assert rep["totals"]["known"] == 1


@pytest.mark.parametrize("need", [["docker"], 5, {"a": 1}, True])
def test_non_text_need(need):
    assert run(fs.t_fleet_status(fctx(Fleet({})), {"need": need})) == "ERROR: need must be text"


def test_need_still_validated():
    assert run(fs.t_fleet_status(fctx(Fleet({})), {"need": "a b"})).startswith("ERROR: need must be letters")


@pytest.mark.parametrize("fmt", [" JSON ", "Json"])
def test_fleet_json_spellings(fmt):
    json.loads(run(fs.t_fleet_status(fctx(Fleet({"a": node("a")})), {"format": fmt})))


@pytest.mark.parametrize("fmt", [["json"], 5])
def test_fleet_non_string_format_text(fmt):
    assert run(fs.t_fleet_status(fctx(Fleet({})), {"format": fmt})).startswith("fleet_status")


def test_fleet_json_never_cut(monkeypatch):
    monkeypatch.setattr(fs, "FLEET_STATUS_MAX_CHARS", 300)
    nodes = {f"n{i:03d}": node(f"n{i:03d}") for i in range(40)}
    out = run(fs.t_fleet_status(fctx(Fleet(nodes)), {"format": "json", "limit": 50}))
    json.loads(out)
    assert len(out) <= 300 or "error" in json.loads(out)


def test_denied_node_still_flagged():
    out = run(fs.t_fleet_status(fctx(Fleet({"denied": node("denied")})), {}))
    assert "NOT-ALLOWED" in out
