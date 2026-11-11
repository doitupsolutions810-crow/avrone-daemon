"""Circuit breakers must never alias two distinct origins using the same string key."""
import pytest
from avrone.fusion_loopback import origin_key

@pytest.mark.parametrize("port", [None, 0, 80, 8090, 65535])
def test_http_remote_named_loopback_is_not_trusted_loopback(port):
    p = "" if port is None else f":{port}"
    assert origin_key("http://loopback"+p) != origin_key("http://127.0.0.1"+p)

@pytest.mark.parametrize("port", [None, 0, 80, 8090, 65535])
def test_https_remote_host_cannot_alias_http_prefixed_hostname(port):
    p = "" if port is None else f":{port}"
    assert origin_key("https://10.0.0.1"+p) != origin_key("http://https-10.0.0.1"+p)

@pytest.mark.parametrize("port", [None, 0, 80, 8090, 65535])
def test_https_local_cannot_alias_http_remote_name(port):
    p = "" if port is None else f":{port}"
    assert origin_key("https://127.0.0.1"+p) != origin_key("http://https-loopback"+p)

@pytest.mark.parametrize("port", [None, 80, 8090])
def test_equivalent_loopback_https_still_share_breaker(port):
    p = "" if port is None else f":{port}"
    assert origin_key("https://127.0.0.1"+p) == origin_key("https://[::1]"+p)
    assert origin_key("https://localhost"+p) == origin_key("https://127.0.0.1"+p)
