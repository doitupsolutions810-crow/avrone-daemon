import asyncio

import pytest

from avrone import server
from avrone import tools_hostview as hv
from avrone.tools import builtin_registry
from avrone.tools_extra import register_extra_tools


def run(c):
    return asyncio.run(c)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("AVRONE_HOST", "AVRONE_FLEET_TOKEN", "AVRONE_PUBLIC_ACCESS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(server.settings, "admin_token", "")


def test_registered_and_api_surface_shows_local():
    r = builtin_registry()
    register_extra_tools(r)
    assert r.get("exposure_check")
    out = run(hv.t_api_surface(None, {}))
    assert "POST /api/chat [local]" in out and "WS /ws/events [local]" in out
    assert "AVRONE_PUBLIC_ACCESS=loopback" in out


def test_local_default():
    e = hv.exposure()
    assert e["verdict"] == "LOCAL" and not e["risky"] and e["admin_token"] == "unset"
    assert "GET /api/status" in e["remote_open"] and not any("/api/chat" in x for x in e["remote_open"])


def test_exposed_open_is_at_risk(monkeypatch):
    monkeypatch.setenv("AVRONE_HOST", "0.0.0.0")
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "all")
    out = run(hv.t_exposure_check(None, {}))
    assert out.startswith("exposure_check: AT RISK") and "POST /api/chat" in out and "WS /ws/events" in out


def test_exposed_guarded(monkeypatch):
    monkeypatch.setenv("AVRONE_HOST", "[::]")
    monkeypatch.setattr(server.settings, "admin_token", "Xk9mP2vL8qR4wZ7nB3cD5fG6hJ1tY0uE")
    e = hv.exposure()
    assert e["exposed"] and e["verdict"] == "GUARDED" and e["admin_token"] == "ok"


def test_token_issues_never_leak_values(monkeypatch):
    monkeypatch.setattr(server.settings, "admin_token", "changeme")
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "changeme")
    out = run(hv.t_exposure_check(None, {}))
    assert "admin token is placeholder" in out and "same secret" in out and "AT RISK" in out
    monkeypatch.setattr(server.settings, "admin_token", "short-tok")
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "y" * 40)
    out = run(hv.t_exposure_check(None, {}))
    assert "admin token is weak" in out and "short-tok" not in out and "y" * 40 not in out
    assert hv._token_strength(None) == "unset"


def test_no_app(monkeypatch):
    monkeypatch.setattr(hv, "exposure", lambda: (_ for _ in ()).throw(ImportError("x")))
    assert "no HTTP app" in run(hv.t_exposure_check(None, {}))
