"""Boundary tests: zone-suffix parsing and scheme-isolated breaker identities."""
import random
import pytest
from avrone.fusion_loopback import is_access_loopback, is_any_loopback, origin_key

@pytest.mark.parametrize("host", [
    "127.0.0.1%eth0", "127.0.0.1%25lo", "localhost%evil",
    "localhost%25eth0", "127.7.8.9%lo", "a.localhost%bogus",
    "::1%lo%trailer", "::ffff:127.0.0.1%lo%attack",
    "[::1%lo%attack]", "::1%", "[::1%]", "::1%lo\nspoof",
])
def test_invalid_scope_suffix_never_gets_loopback_trust(host):
    assert not is_access_loopback(host), repr(host)
    assert not is_any_loopback(host), repr(host)

@pytest.mark.parametrize("host", [
    "::1%lo", "[::1%lo]", "::ffff:127.0.0.1%1",
    "[::ffff:127.0.0.1%25eth0]",
])
def test_single_valid_ipv6_scope_remains_supported(host):
    assert is_access_loopback(host)
    assert is_any_loopback(host)

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]", "::ffff:127.0.0.1"])
def test_unscoped_loopback_remains_supported(host):
    assert is_access_loopback(host)

@pytest.mark.parametrize("port", [None, 1, 80, 443, 8090, 65535])
def test_http_https_breaker_circuits_are_not_shared(port):
    suffix = "" if port is None else f":{port}"
    assert origin_key("http://127.0.0.1"+suffix) != origin_key("https://127.0.0.1"+suffix)
    assert origin_key("https://127.0.0.1"+suffix) == origin_key("https://[::1]"+suffix)

@pytest.mark.parametrize("base", [
    "", "localhost", "file://127.0.0.1", "http://", "https://",
    "://localhost", "ftp://localhost", "mailto:localhost",
])
def test_invalid_origin_is_rejected(base):
    assert origin_key(base) == "invalid:"

def test_fuzz_suffix_hostnames_are_always_remote():
    rng = random.Random(24011)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789_.-"
    for _ in range(3000):
        suffix = "".join(rng.choices(alphabet, k=rng.randint(1, 32)))
        for prefix in ("127.0.0.1", "localhost"):
            host = prefix + "%" + suffix
            assert not is_access_loopback(host)
            assert not is_any_loopback(host)
