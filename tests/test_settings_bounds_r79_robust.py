"""Round 79: numeric settings are bounded; LLM retry delays and breakers cannot hang forever."""
from __future__ import annotations

from types import SimpleNamespace as NS

import httpx
import pytest

from avrone import config as c
from avrone import gateway, llm


@pytest.mark.parametrize("var,attr,raw,want", [
    ("AVRONE_MAX_STEPS", "max_steps", "0", 1),
    ("AVRONE_MAX_STEPS", "max_steps", "-3", 1),
    ("AVRONE_MAX_STEPS", "max_steps", "99999", 1000),
    ("AVRONE_LLM_TIMEOUT", "llm_timeout", "0", 180.0),
    ("AVRONE_LLM_TIMEOUT", "llm_timeout", "-1", 180.0),
    ("AVRONE_LLM_TIMEOUT", "llm_timeout", "0.5", 180.0),
    ("AVRONE_LLM_TIMEOUT", "llm_timeout", "1e9", 3600.0),
    ("AVRONE_LLM_TIMEOUT", "llm_timeout", "45", 45.0),
    ("AVRONE_LLM_BACKOFF", "llm_backoff", "1e300", 60.0),
    ("AVRONE_LLM_BACKOFF", "llm_backoff", "-2", 0.0),
    ("AVRONE_LLM_BACKOFF", "llm_backoff", "0", 0.0),
    ("AVRONE_LLM_RETRIES", "llm_retries", "0", 1),
    ("AVRONE_LLM_RETRIES", "llm_retries", "500", 20),
    ("AVRONE_BREAKER_THRESHOLD", "breaker_threshold", "0", 1),
    ("AVRONE_BREAKER_COOLDOWN", "breaker_cooldown", "1e300", 3600.0),
    ("AVRONE_BREAKER_COOLDOWN", "breaker_cooldown", "-5", 0.0),
    ("AVRONE_SQUAD_SIZE", "squad_size", "0", 1),
    ("AVRONE_SQUAD_SIZE", "squad_size", "6", 6),
    ("AVRONE_DEEP_SQUAD_SIZE", "deep_squad_size", "-1", 0),
    ("AVRONE_MAX_PARALLEL", "max_parallel", "0", 1),
    ("AVRONE_MAX_PARALLEL", "max_parallel", "1e12", 4096),
    ("AVRONE_MAX_SPAWN_DEPTH", "max_spawn_depth", "-4", 0),
    ("AVRONE_MAX_SPAWN_DEPTH", "max_spawn_depth", "999", 16),
    ("AVRONE_AGENT_TIMEOUT", "agent_timeout", "-1", 0.0),
    ("AVRONE_MIN_PARALLEL", "min_parallel", "0", 1),
    ("AVRONE_THERMAL_MAX_C", "thermal_max_c", "-40", 85.0),
    ("AVRONE_THERMAL_MAX_C", "thermal_max_c", "500", 150.0),
    ("AVRONE_COMMAND_TIMEOUT", "command_timeout", "0", 120),
    ("AVRONE_COMMAND_TIMEOUT", "command_timeout", "-9", 120),
])
def test_env_settings_bounded(var, attr, raw, want, monkeypatch):
    monkeypatch.setenv(var, raw)
    got = getattr(c.Settings(), attr)
    assert got == want and type(got) is type(want)


def test_defaults_unchanged(monkeypatch):
    for k in ("AVRONE_MAX_STEPS", "AVRONE_LLM_TIMEOUT", "AVRONE_LLM_BACKOFF", "AVRONE_SQUAD_SIZE"):
        monkeypatch.delenv(k, raising=False)
    s = c.Settings()
    assert (s.max_steps, s.llm_timeout, s.llm_backoff, s.squad_size) == (6, 180.0, 1.0, 4)


@pytest.mark.parametrize("v", [float("nan"), float("inf"), None, "5", True, [3]])
def test_bound_setting_odd_values(v):
    out = c.bound_setting("llm_timeout", v)
    assert out == (3600.0 if v == float("inf") else 180.0)


def test_bound_setting_custom_default():
    assert c.bound_setting("breaker_cooldown", float("nan"), 30) == 30.0
    assert c.bound_setting("breaker_threshold", "x", 3) == 3


def test_bound_settings_on_plain_object():
    s = NS(max_steps=0, llm_backoff=1e300, other=-1)
    c.bound_settings(s)
    assert s.max_steps == 1 and s.llm_backoff == 60.0 and s.other == -1


def test_gateway_init_bounds(monkeypatch):
    monkeypatch.setattr(gateway, "parse_upstreams", lambda s: [])
    g = gateway.Gateway(NS(breaker_threshold=-7, breaker_cooldown=float("nan")))
    assert g.threshold == 3 and g.cooldown == 30.0
    g = gateway.Gateway(NS())
    assert g.threshold == 3 and g.cooldown == 30.0


def test_nan_cooldown_breaker_reopens(monkeypatch):
    monkeypatch.setattr(gateway, "parse_upstreams", lambda s: [])
    g = gateway.Gateway(NS(breaker_threshold=1, breaker_cooldown=float("nan")))
    up = gateway.Upstream.__new__(gateway.Upstream)
    up.fails, up.open_until, up.calls, up.errors = 0, 0.0, 0, 0
    up.record(False, g.threshold, g.cooldown)
    assert up.open_until - __import__("time").monotonic() <= 30.0


@pytest.mark.parametrize("base,attempt,want", [
    (1e300, 0, 60.0), (1.0, 200, 60.0), (float("nan"), 1, 0.0), (-3, 2, 0.0), ("x", 1, 0.0),
    (None, 1, 0.0), (1.0, 0, 1.0), (0.5, 2, 2.0), (float("inf"), 0, 60.0), (1.0, -5, 1.0),
])
def test_bounded_delay(base, attempt, want):
    assert gateway._bounded_delay(base, attempt) == want


@pytest.mark.parametrize("fn", [gateway._delay, llm._retry_delay])
def test_delays_never_exceed_cap(fn):
    assert fn(None, 1e300, 5) == 60.0
    assert fn(None, float("nan"), 1) == 0.0


@pytest.mark.parametrize("ra,want", [("-5", 0.0), ("nan", 0.0), ("inf", 30.0), ("7", 7.0), ("abc", 0.0), ("", 0.0)])
def test_llm_retry_after(ra, want):
    r = httpx.Response(429, headers={"retry-after": ra})
    assert llm._retry_delay(r, 0, 0) == want
