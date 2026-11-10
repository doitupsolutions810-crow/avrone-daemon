"""IPv4 and IPv6 loopback spellings.

Empirical, from ipaddress on this interpreter: ``::ffff:127.0.0.1``.is_loopback
is False and its ipv4_mapped address is 127.0.0.1, whose is_loopback is True.
Before this seam, caller_allowed compared the raw host to {127.0.0.1, ::1,
localhost}, so a dual-stack accept of IPv4 loopback was refused.

127.0.0.2 stays remote for the HTTP access policy. The doctor and the proxy
bypass still treat every 127.0.0.0/8 address as loopback. That split is
intentional: widening /api/runs to 127.0.0.2 is a different policy.
"""
import pytest
from fastapi.testclient import TestClient

from avrone import doctor, gateway, server
from avrone.fusion_loopback import is_access_loopback, is_any_loopback, origin_key


@pytest.mark.parametrize("host", [
    "127.0.0.1", "::1", "localhost", "[::1]", "::1%lo",
    "::ffff:127.0.0.1", "[::ffff:127.0.0.1]", "::FFFF:127.0.0.1",
])
def test_access_loopback_spellings(host):
    assert is_access_loopback(host) is True


@pytest.mark.parametrize("host", ["127.0.0.2", "::ffff:10.0.0.1", "::ffff:127.0.0.2", "10.0.0.1", "", "testclient"])
def test_access_policy_does_not_widen(host):
    assert is_access_loopback(host) is False


def test_doctor_and_proxy_unwrap_mapped_and_keep_127_block():
    assert doctor._is_loopback_host("::ffff:127.0.0.1") is True
    assert doctor._is_loopback_host("127.0.0.2") is True
    assert doctor._is_loopback_host("::ffff:10.0.0.1") is False
    assert doctor._is_loopback_host("") is True  # doctor treats an empty bind as local
    assert is_any_loopback("127.8.9.1") is True
    assert gateway.is_loopback_url("http://[::ffff:127.0.0.1]:11434/v1") is True
    assert gateway.is_loopback_url("http://127.8.9.1/v1") is True
    assert gateway.is_loopback_url("http://[::ffff:10.0.0.1]/v1") is False
    assert gateway.is_loopback_url("http://10.0.0.5:11434/v1") is False


def test_loopback_origins_share_one_breaker_bucket():
    assert origin_key("http://127.0.0.1:8090") == origin_key("http://[::1]:8090")
    assert origin_key("http://127.0.0.1:8090") == origin_key("http://[::ffff:127.0.0.1]:8090")
    assert origin_key("http://localhost:8090") == "loopback:8090"
    assert origin_key("http://10.0.0.5:8090") == "10.0.0.5:8090"
    assert origin_key("http://127.0.0.2:8090") == "127.0.0.2:8090"


def test_mapped_client_reaches_a_loopback_route(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_PUBLIC_ACCESS", "loopback")
    monkeypatch.setattr(server.settings, "workspace", tmp_path / "ws")
    monkeypatch.setattr(server.settings, "data_dir", tmp_path / "data")
    with TestClient(server.app, client=("::ffff:127.0.0.1", 9)) as mapped:
        assert mapped.get("/api/runs").status_code == 200
    with TestClient(server.app, client=("::ffff:10.0.0.1", 9)) as remote:
        assert remote.get("/api/runs").status_code == 403
    with TestClient(server.app, client=("127.0.0.2", 9)) as other:
        assert other.get("/api/runs").status_code == 403
