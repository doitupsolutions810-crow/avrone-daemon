"""Regression: invalid outbound Fusion URLs cannot crash breaker lookup."""
import pytest
from avrone.fusion_loopback import origin_key, is_access_loopback

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:bad",
    "http://127.0.0.1:65536",
    "http://[::1]:-1",
    "http://[::1]:1.5",
    "http://localhost:999999999999999999999999",
    "http://127.0.0.1:８０",
    "http://127.0.0.1:0x50",
    "http://user:pass@127.0.0.1:notaport",
    "http://[::1",
])
def test_invalid_port_is_rejected_without_exception(url):
    assert origin_key(url) == "invalid:"

@pytest.mark.parametrize("host,allowed", [
    ("127.0.0.1", True), ("::1", True), ("::ffff:127.0.0.1", True),
    ("127.0.0.2", False), ("::ffff:127.0.0.2", False),
    ("::ffff:10.0.0.1", False), ("", False), ("testclient", False),
])
def test_loopback_access_policy_unchanged(host, allowed):
    assert is_access_loopback(host) is allowed

@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:8090", "loopback:8090"),
    ("http://[::1]:8090", "loopback:8090"),
    ("http://[::ffff:127.0.0.1]:8090", "loopback:8090"),
    ("http://127.0.0.2:8090", "127.0.0.2:8090"),
    ("http://10.0.0.5:8090", "10.0.0.5:8090"),
])
def test_loopback_bucket_isolation(url, expected):
    assert origin_key(url) == expected

@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:0", "loopback:0"),
    ("http://[::1]:65535/v1/events", "loopback:65535"),
    ("http://localhost", "loopback:None"),
    ("http://10.0.0.1:65535/health", "10.0.0.1:65535"),
])
def test_valid_port_bounds(url, expected):
    assert origin_key(url) == expected

@pytest.mark.parametrize("host", [
    "[127.0.0.1", "127.0.0.1]", "[[::1]]", "[localhost]",
    "[::1", "::1]", "[127.0.0.1]", "[[::ffff:127.0.0.1]]",
])
def test_malformed_bracketed_peer_cannot_gain_loopback_access(host):
    from avrone.fusion_loopback import is_any_loopback
    assert is_access_loopback(host) is False
    assert is_any_loopback(host) is False

@pytest.mark.parametrize("host", ["[::1]", "[::ffff:127.0.0.1]", "::1%lo"])
def test_valid_bracketed_ipv6_hosts_keep_access(host):
    assert is_access_loopback(host) is True
