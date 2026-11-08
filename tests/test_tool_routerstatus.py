import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from avrone import gateway
from avrone import tools_extra as tx
from avrone import tools_routerstatus as rs
from avrone.config import Settings
from avrone.llm import LLM
from avrone.tools import builtin_registry

KEY = "sk-routerSECRETkey1234567890"
URL = "https://secret-host.example/v1"


def ups(*specs):
    return [gateway.Upstream(p, URL, m, KEY, lead) for p, m, lead in specs]


@pytest.fixture()
def env(monkeypatch):
    monkeypatch.delenv("AVRONE_LLM_FALLBACKS", raising=False)
    reg = builtin_registry()
    tx.register_extra_tools(reg)
    gw = gateway.Gateway(Settings())
    gw.upstreams = ups(("xai", "grok-4", ""), ("openrouter", "x-ai/grok-4", "big"), ("ollama", "llama3.1", ""))
    core = SimpleNamespace(tools=reg, llm=SimpleNamespace(gateway=gw))
    ctx = SimpleNamespace(core=core, run_id="r", name="w", depth=0)
    return SimpleNamespace(reg=reg, ctx=ctx, gw=gw, core=core)


def call(env, **a):
    return asyncio.run(env.reg.call(env.ctx, "router_status", a))


def test_registered(env):
    assert env.reg.get("router_status").source == "builtin"
    assert env.reg.check("router_status", {}) is None


def test_no_gateway(env):
    env.core.llm = None
    assert "no model gateway configured" in call(env)
    assert json.loads(call(env, format="json"))["gateway"] == "off"


def test_gateway_directly_on_core_and_real_llm(env):
    env.core.llm = env.gw
    assert "primary: xai:grok-4" in call(env)
    env.core.llm = LLM(Settings())
    assert "router_status" in call(env)


def test_chain_and_states(env):
    now = time.monotonic()  # round 112: breaker deadlines are monotonic
    a, b, c = env.gw.upstreams
    b.fails, b.open_until = 5, now + 20  # open
    c.fails, c.open_until = 3, now - 1   # cooldown over -> half-open
    out = call(env)
    assert "fallback chain: openrouter:x-ai/grok-4 -> ollama:llama3.1" in out
    assert "lead=big" in out
    assert "[0] xai:grok-4 primary circuit=closed" in out
    assert "[1] openrouter:x-ai/grok-4 lead=big fallback circuit=open cooldown=" in out
    assert "circuit=half-open" in out
    assert "summary: closed=1, half-open=1, open=1" in out
    rep = json.loads(call(env, format="json"))
    assert [u["state"] for u in rep["upstreams"]] == ["closed", "open", "half-open"]
    assert 0 < rep["upstreams"][1]["cooldown_s"] <= 20


def test_record_resets_to_closed(env):
    up = env.gw.upstreams[0]
    for _ in range(3):
        up.record(False, 3, 30)
    assert rs.circuit_state(up, 3)[0] == "open"
    up.record(True, 3, 30)
    assert rs.circuit_state(up, 3) == ("closed", 0.0)


def test_primary_only_from_env(env, monkeypatch):
    env.gw.upstreams = env.gw.upstreams[:1]
    assert "no fallbacks configured" in call(env)


def test_fallbacks_from_env(monkeypatch, env):
    monkeypatch.setenv("AVRONE_LLM_FALLBACKS", "ollama:llama3.1")
    env.core.llm = gateway.Gateway(Settings())
    assert "-> ollama:llama3.1" in call(env) or "fallback chain: ollama:llama3.1" in call(env)


def test_secrets_never_shown(env):
    env.gw.upstreams[0].model = f"model-{KEY}"
    env.gw.last_route = [f"xai {KEY}", URL]
    for out in (call(env), call(env, format="json")):
        assert KEY not in out and "secret-host" not in out


def test_caps_and_bad_format(env, monkeypatch):
    env.gw.upstreams = ups(*[("ollama", f"m{i}", "") for i in range(30)])
    out = call(env)
    assert "[10 more upstreams]" in out
    monkeypatch.setattr(rs, "ROUTER_STATUS_MAX_CHARS", 1500)
    rep = json.loads(call(env, format="json"))
    assert rep["more"] > 10
    assert len(call(env)) <= 1500 + 20
    assert "ERROR" in call(env, format="xml")


def test_mock_mode_and_empty(env):
    env.gw.s = SimpleNamespace(llm_mode="mock")
    assert "mock mode" in call(env)
    env.gw.upstreams = []
    assert "no upstreams configured" in call(env)


def test_num_rejects_non_finite():
    assert rs._num(True) is None and rs._num(float("nan")) is None and rs._num("1") is None
    assert rs._num(2) == 2.0


def test_route_direct_vs_env_proxy(env, monkeypatch):
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("https_proxy", "http://proxy.corp:3128")
    env.gw.upstreams = [gateway.Upstream("ollama", "http://127.0.0.1:11434/v1", "q", ""),
                        gateway.Upstream("xai", "https://api.x.ai/v1", "grok", KEY)]
    out = call(env)
    assert "[0] ollama:q primary circuit=closed route=direct" in out
    assert "route=env-proxy" in out and "proxy.corp" not in out
    monkeypatch.setenv("NO_PROXY", "api.x.ai")
    monkeypatch.setenv("no_proxy", "api.x.ai")
    assert rs.route_of("https://api.x.ai/v1") == "direct"
    assert rs.route_of("http://[bad") == "direct" and rs.route_of("") == "direct"


def test_route_no_proxy_env(monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    assert rs.route_of("https://api.x.ai/v1") == "direct"
