"""Hardening round 64: router_status reports the route httpx really takes (ALL_PROXY), and
codex_stats format=json always parses and keeps records split on "\\n" only."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as S

import pytest

from avrone import gateway
from avrone import tools_codexstats as cs
from avrone import tools_routerstatus as rs
from avrone.codex import Codex
from avrone.config import Settings

PROXY_VARS = ["ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
              "NO_PROXY", "no_proxy"]


def run(c):
    return asyncio.run(c)


@pytest.fixture
def clean_env(monkeypatch):
    for k in PROXY_VARS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


# ------------------------------------------------------------ router_status routing
@pytest.mark.parametrize("env,url,want", [
    ({"ALL_PROXY": "http://p:3128"}, "https://api.x.ai/v1", "env-proxy"),
    ({"all_proxy": "http://p:3128"}, "https://api.x.ai/v1", "env-proxy"),
    ({"ALL_PROXY": "http://p:3128"}, "http://api.x.ai/v1", "env-proxy"),
    ({"ALL_PROXY": "http://p:3128", "NO_PROXY": "x.ai"}, "https://api.x.ai/v1", "direct"),
    ({"ALL_PROXY": "http://p:3128", "NO_PROXY": "*"}, "https://api.x.ai/v1", "direct"),
    ({"ALL_PROXY": "http://p:3128"}, "http://127.0.0.1:11434/v1", "direct"),
    ({"ALL_PROXY": "http://p:3128"}, "http://localhost:1234/v1", "direct"),
    ({"HTTPS_PROXY": "http://p:3128"}, "https://api.x.ai/v1", "env-proxy"),
    ({"HTTP_PROXY": "http://p:3128"}, "https://api.x.ai/v1", "direct"),
    ({"HTTPS_PROXY": "http://p:3128", "NO_PROXY": "other.com"}, "https://api.x.ai/v1", "env-proxy"),
    ({"HTTPS_PROXY": "http://p:3128", "NO_PROXY": ".x.ai"}, "https://api.x.ai/v1", "direct"),
    ({}, "https://api.x.ai/v1", "direct"),
    ({"HTTPS_PROXY": "http://p:3128"}, "", "direct"),
    ({"HTTPS_PROXY": "http://p:3128"}, "not a url", "direct"),
])
def test_route_matches_httpx(clean_env, env, url, want):
    for k, v in env.items():
        clean_env.setenv(k, v)
    assert rs.route_of(url) == want


def test_socks_all_proxy_reported_as_proxy(clean_env):
    clean_env.setenv("ALL_PROXY", "socks5://p:1080")
    assert rs.route_of("https://api.x.ai/v1") == "env-proxy"


def test_httpx_failure_falls_back_to_urllib_with_all(clean_env):
    clean_env.setenv("ALL_PROXY", "http://p:3128")
    clean_env.setattr(rs, "_httpx_route", lambda u: None)
    assert rs.route_of("https://api.x.ai/v1") == "env-proxy"


def test_httpx_raising_never_breaks(clean_env):
    import httpx
    clean_env.setattr(httpx, "Client", lambda **k: 1 / 0)
    assert rs._httpx_route("https://api.x.ai/v1") is None
    assert rs.route_of("https://api.x.ai/v1") == "direct"


def test_router_status_shows_all_proxy_route_and_hides_proxy(clean_env):
    clean_env.setenv("ALL_PROXY", "http://proxyuser:proxypass@corp-proxy:3128")
    gw = gateway.Gateway(Settings())
    gw.upstreams = [gateway.Upstream("xai", "https://api.x.ai/v1", "grok-4", "sk-k3y0123456789abcdef", "")]
    out = run(rs.t_router_status(S(core=S(llm=gw)), {}))
    assert "route=env-proxy" in out
    assert "corp-proxy" not in out and "proxypass" not in out


def test_only_shown_rows_get_a_route(clean_env):
    calls = []
    clean_env.setattr(rs, "route_of", lambda u: calls.append(u) or "direct")
    gw = gateway.Gateway(Settings())
    gw.upstreams = [gateway.Upstream("p", f"https://h{i}.example/v1", "m", "", "") for i in range(60)]
    rep = rs.router_status(S(core=S(llm=gw)))
    assert len(calls) == rs.ROUTER_STATUS_MAX_UPSTREAMS and rep["more"] == 40


# ------------------------------------------------------------ codex_stats
@pytest.fixture
def cenv(tmp_path):
    codex = Codex(tmp_path)
    ctx = S(core=S(codex=codex, settings=S()), run_id="run1", name="W-1", depth=0)
    return S(codex=codex, ctx=ctx, tmp=tmp_path)


def stats(cenv, **a):
    return run(cs.t_codex_stats(cenv.ctx, a))


def big_seed(cenv):
    for i in range(70):
        k = f"kind.{'x' * 50}.{i:03d}"
        for _ in range(i % 3 + 1):
            cenv.codex.emit("run1", k, "W-1")
        cenv.codex.emit("run1", "tool.call", "W-1", tool=f"tool_{'y' * 50}_{i:03d}", args={})


def test_json_always_parses_when_large(cenv):
    big_seed(cenv)
    out = stats(cenv, format="json", last=50)
    assert len(out) <= cs.CODEX_STATS_MAX_CHARS
    rep = json.loads(out)
    assert rep["events"] == sum(i % 3 + 1 for i in range(70)) + 70


def test_json_trims_recent_before_counts(cenv):
    big_seed(cenv)
    rep = json.loads(stats(cenv, format="json", last=50))
    if rep.get("recent_trimmed"):
        assert len(rep["recent"]) < 50
    # the most common kind survives every trim
    assert "tool.call" in rep["by_kind"]


def test_json_small_unchanged_indent(cenv):
    cenv.codex.emit("run1", "tool.call", "W-1", tool="shell", args={})
    out = stats(cenv, format="json")
    assert out.startswith("{\n") and json.loads(out)["tools_called"] == {"shell": 1}
    assert "recent_trimmed" not in out


def test_json_capped_direct():
    rep = {"recent": [{"ts": "x", "kind": "k" * 60}] * 50,
           "tools_called": {f"t{i}{'z' * 60}": 1 for i in range(60)},
           "by_kind": {f"k{i}{'z' * 60}": 60 - i for i in range(60)}, "kinds_hidden": 0}
    out = cs._json_capped(rep)
    assert len(out) <= cs.CODEX_STATS_MAX_CHARS
    r = json.loads(out)
    assert r.get("recent_trimmed") and r.get("tools_hidden", 0) >= 0
    assert "k0" + "z" * 60 in r["by_kind"]  # largest count kept


@pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"])
def test_raw_separator_inside_record_still_counted(cenv, sep):
    cenv.codex.emit("run1", "tool.call", "W-1", tool="shell", args={})
    p = cs._log_path(cenv.ctx)
    rec = json.dumps({"run_id": "run1", "kind": "note", "agent": "W-1", "ts": 1.0,
                      "data": {"msg": f"a{sep}b"}}, ensure_ascii=False)
    with open(p, "a", encoding="utf-8") as f:
        f.write(rec + "\n")
    rep = json.loads(stats(cenv, format="json"))
    assert rep["by_kind"].get("note") == 1 and rep["events"] == 2


def test_crlf_lines_still_parse(cenv):
    p = cs._log_path(cenv.ctx)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'{"run_id":"run1","kind":"a","agent":"W-1","ts":1}\r\n'
                  b'{"run_id":"run1","kind":"b","agent":"W-1","ts":2}\r\n')
    rep = json.loads(stats(cenv, format="json"))
    assert rep["by_kind"] == {"a": 1, "b": 1}


def test_text_format_still_capped(cenv):
    big_seed(cenv)
    out = stats(cenv, last=50)
    assert len(out) <= cs.CODEX_STATS_MAX_CHARS + 20 and "by kind:" in out
