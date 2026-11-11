"""Loopback and origin parsing must not normalize control / spacing disguises."""
import random
import pytest
from avrone.fusion_loopback import is_access_loopback, is_any_loopback, origin_key

@pytest.mark.parametrize("host", [
    "127.0.0.1\n", "\tlocalhost", "::1%lo\r", "localhost\x85",
    "\u2003localhost", "127.0.0.1 ", "localhost\u00a0",
    "127.0.0.1\u2007", "::1%lo\u2028", "localhost\x00",
])
def test_host_whitespace_controls_rejected(host):
    assert is_access_loopback(host) is False
    assert is_any_loopback(host) is False

@pytest.mark.parametrize("url", [
    "http://lo\ncalhost:8090", "http://127.0.0.1\n:8090",
    "\nhttp://127.0.0.1", "http://localhost\t:80",
    "https://localhost\r:443", "\t\nhttps://localhost",
    "http://local\x85host:80", "http://localhost:80\u2007",
])
def test_url_with_unencoded_controls_rejected(url):
    assert origin_key(url) == "invalid:"

@pytest.mark.parametrize("host,url", [
    ("127.0.0.1", "http://127.0.0.1:80"),
    ("localhost", "http://localhost:8090"),
    ("::1%lo", "https://[::1%lo]:443"),
    ("[::1]", "https://[::1]:443"),
])
def test_clean_inputs_still_valid(host, url):
    assert is_access_loopback(host)
    assert origin_key(url) != "invalid:"

def test_random_control_suffixes_never_grant_access():
    rng = random.Random(11240)
    whitespace = [" ", "\t", "\n", "\r", "\u00a0", "\u2003", "\x85", "\x00"]
    for _ in range(2000):
        name = rng.choice(["localhost", "127.0.0.1", "::1", "::1%lo"])
        noise = rng.choice(whitespace)
        host = name + noise if rng.random() < .5 else noise + name
        assert not is_access_loopback(host)
        assert not is_any_loopback(host)
