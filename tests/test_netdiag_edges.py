"""Offline edge tests for avrone.netdiag: resolver failures, DoH junk, tcp_ping failures and
boundaries, traceroute binary selection/timeout, RDAP errors and IP-geo branches.
No real network: HTTP goes through httpx.MockTransport; sockets/subprocesses are faked."""
import asyncio
import socket

import httpx
import pytest

from avrone import netdiag, tools_extra


def run(c):
    return asyncio.run(c)


def http(monkeypatch, handler):
    monkeypatch.setattr(tools_extra, "HTTP_TRANSPORT", httpx.MockTransport(handler))


# ---------------------------------------------------------------- _host / _is_ip
@pytest.mark.parametrize("raw,want", [
    ("https://Example.com/path?q=1", "Example.com"),
    ("[2001:db8::1]", "2001:db8::1"),
    ("  host.local  ", "host.local"),
    ("10.0.0.1", "10.0.0.1"),
])
def test_host_normalises(raw, want):
    assert netdiag._host({"host": raw}) == want


@pytest.mark.parametrize("raw", ["", "-oProxy=x", "a b", "evil;rm", "x" * 254, "http://", None])
def test_host_rejects(raw):
    with pytest.raises(ValueError):
        netdiag._host({"host": raw} if raw is not None else {})


def test_is_ip():
    assert netdiag._is_ip("::1") and netdiag._is_ip("8.8.8.8")
    assert not netdiag._is_ip("example.com") and not netdiag._is_ip("999.1.1.1")


# ---------------------------------------------------------------- dns_lookup
def test_dns_bad_type_rejected():
    assert run(netdiag.t_dns_lookup(None, {"host": "ex.com", "type": "AXFR"})).startswith("ERROR: type")


def test_dns_a_record_system_resolver_and_doh(monkeypatch):
    async def fake_gai(self, host, port, family=0, **kw):
        assert family == socket.AF_INET
        return [(socket.AF_INET, 1, 6, "", ("5.6.7.8", 0)), (socket.AF_INET, 1, 6, "", ("1.2.3.4", 0)),
                (socket.AF_INET, 2, 17, "", ("1.2.3.4", 0))]
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_gai)
    http(monkeypatch, lambda r: httpx.Response(200, json={"Status": 3}))
    out = run(netdiag.t_dns_lookup(None, {"host": "ex.com", "type": "a"}))
    assert "system: 1.2.3.4, 5.6.7.8" in out and "doh: NXDOMAIN" in out


def test_dns_aaaa_resolver_failure_and_unknown_status(monkeypatch):
    async def boom(self, host, port, family=0, **kw):
        assert family == socket.AF_INET6
        raise socket.gaierror(-2, "Name or service not known")
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", boom)
    http(monkeypatch, lambda r: httpx.Response(200, json={"Status": 5, "Answer": None}))
    out = run(netdiag.t_dns_lookup(None, {"host": "nx.invalid", "type": "AAAA"}))
    assert "system: [Errno -2] Name or service not known" in out and "doh: 5" in out


def test_dns_doh_junk_body_and_connect_error(monkeypatch):
    http(monkeypatch, lambda r: httpx.Response(200, text="<html>not json</html>"))
    assert "doh: unavailable (JSONDecodeError)" in run(netdiag.t_dns_lookup(None, {"host": "ex.com", "type": "TXT"}))

    def refuse(r):
        raise httpx.ConnectError("refused", request=r)
    http(monkeypatch, refuse)
    assert "doh: unavailable (ConnectError)" in run(netdiag.t_dns_lookup(None, {"host": "ex.com", "type": "NS"}))


def test_dns_doh_answers_capped_at_30(monkeypatch):
    answers = [{"name": "ex.com", "TTL": 1, "data": f"10.0.0.{i}"} for i in range(50)]
    http(monkeypatch, lambda r: httpx.Response(200, json={"Status": 0, "Answer": answers}))
    monkeypatch.setenv("AVRONE_DOH_URL", "https://doh.invalid/q")
    out = run(netdiag.t_dns_lookup(None, {"host": "ex.com", "type": "MX"}))
    assert out.count("ttl=") == 30 and "10.0.0.29" in out and "10.0.0.30" not in out


# ---------------------------------------------------------------- tcp_ping
class _W:
    def __init__(self, peer):
        self.peer = peer
        self.closed = False

    def get_extra_info(self, key):
        return self.peer

    def close(self):
        self.closed = True


@pytest.fixture()
def no_sleep(monkeypatch):
    real = asyncio.sleep

    async def fast(d, *a, **k):
        await real(0)
    monkeypatch.setattr(netdiag.asyncio, "sleep", fast)


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_tcp_ping_port_out_of_range(port):
    assert run(netdiag.t_tcp_ping(None, {"host": "h", "port": port})) == "ERROR: port out of range"


def test_tcp_ping_all_fail_and_count_clamped(monkeypatch, no_sleep):
    calls = []

    async def refuse(host, port):
        calls.append((host, port))
        raise ConnectionRefusedError(111, "Connection refused")
    monkeypatch.setattr(netdiag.asyncio, "open_connection", refuse)
    out = run(netdiag.t_tcp_ping(None, {"host": "h.invalid", "port": 22, "count": 99}))
    assert len(calls) == 10 and out.endswith("0/10 ok") and "ConnectionRefusedError" in out


def test_tcp_ping_count_floor_and_timeout(monkeypatch, no_sleep):
    async def hang(host, port):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(netdiag.asyncio, "open_connection", hang)
    out = run(netdiag.t_tcp_ping(None, {"host": "h", "count": 0}))
    assert "seq=0 failed: TimeoutError" in out and out.endswith("0/1 ok") and ":443" in out


def test_tcp_ping_mixed_and_no_peername(monkeypatch, no_sleep):
    seq = iter([_W(("9.9.9.9", 443)), OSError("unreachable"), _W(None)])

    async def flaky(host, port):
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return None, v
    monkeypatch.setattr(netdiag.asyncio, "open_connection", flaky)
    out = run(netdiag.t_tcp_ping(None, {"host": "box", "count": 3}))
    assert "seq=0 9.9.9.9" in out and "seq=1 failed: OSError unreachable" in out and "seq=2 box" in out
    assert "2/3 ok, min/avg/max" in out


def test_tcp_ping_bad_port_type():
    # round 45: a bad port is a tool error message, not a ValueError out of the tool
    assert run(netdiag.t_tcp_ping(None, {"host": "h", "port": "https"})).startswith("ERROR: port")


# ---------------------------------------------------------------- traceroute
class _Proc:
    def __init__(self, out=b"", hang=False):
        self.out, self.hang, self.killed = out, hang, False

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(3600)
        return self.out, None

    def kill(self):
        self.killed = True


def _which(available):
    return lambda b: f"/usr/bin/{b}" if b in available else None


def test_traceroute_no_binary(monkeypatch):
    monkeypatch.setattr(netdiag.shutil, "which", _which(set()))
    out = run(netdiag.t_traceroute(None, {"host": "ex.com"}))
    assert out.startswith("ERROR: no traceroute binary") and "tcp_ping still works" in out


@pytest.mark.parametrize("avail,binary", [({"mtr", "traceroute"}, "mtr"), ({"traceroute", "tracepath"}, "traceroute"),
                                          ({"tracepath"}, "tracepath")])
def test_traceroute_prefers_binaries_and_clamps_hops(monkeypatch, avail, binary):
    seen = []

    async def fake_exec(*argv, **kw):
        seen.append(argv)
        return _Proc(b"1 10.0.0.1\n" + b"x" * 9000)
    monkeypatch.setattr(netdiag.shutil, "which", _which(avail))
    monkeypatch.setattr(netdiag.asyncio, "create_subprocess_exec", fake_exec)
    out = run(netdiag.t_traceroute(None, {"host": "ex.com", "max_hops": 500}))
    assert out.startswith(f"[{binary}]\n") and len(out) == len(f"[{binary}]\n") + 8000  # output tail capped
    assert seen[0][0] == binary and "30" in seen[0] and seen[0][-1] == "ex.com"


def test_traceroute_min_hops(monkeypatch):
    seen = []

    async def fake_exec(*argv, **kw):
        seen.append(argv)
        return _Proc(b"ok")
    monkeypatch.setattr(netdiag.shutil, "which", _which({"tracepath"}))
    monkeypatch.setattr(netdiag.asyncio, "create_subprocess_exec", fake_exec)
    run(netdiag.t_traceroute(None, {"host": "ex.com", "max_hops": -3}))
    assert seen[0][seen[0].index("-m") + 1] == "1"


def test_traceroute_timeout_kills(monkeypatch):
    proc = _Proc(hang=True)

    async def fake_exec(*argv, **kw):
        return proc

    async def fast_wait_for(aw, timeout):
        aw.close()
        raise asyncio.TimeoutError
    monkeypatch.setattr(netdiag.shutil, "which", _which({"traceroute"}))
    monkeypatch.setattr(netdiag.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(netdiag.asyncio, "wait_for", fast_wait_for)
    assert run(netdiag.t_traceroute(None, {"host": "ex.com"})) == "ERROR: traceroute timed out" and proc.killed


# ---------------------------------------------------------------- rdap_whois
def test_rdap_ip_query_and_full_fields(monkeypatch):
    seen = []

    def handler(r):
        seen.append(str(r.url))
        return httpx.Response(200, json={
            "handle": "NET-1", "startAddress": "8.8.8.0", "endAddress": "8.8.8.255", "country": "US",
            "status": ["active", "x2", "x3", "x4", "x5", "x6", "x7"],
            "entities": [{"roles": ["registrant"], "handle": "GOGL"},
                         {"roles": ["abuse"], "vcardArray": ["vcard", [["version", {}, "text", "4.0"]]]}]})
    http(monkeypatch, handler)
    out = run(netdiag.t_rdap_whois(None, {"query": "8.8.8.8"}))
    assert seen == ["https://rdap.org/ip/8.8.8.8"]
    assert "RDAP ip 8.8.8.8" in out and "startAddress: 8.8.8.0" in out and "x6" in out and "x7" not in out
    assert "entity: registrant GOGL" in out and "entity: abuse" in out


def test_rdap_falls_back_to_host_and_errors(monkeypatch):
    http(monkeypatch, lambda r: httpx.Response(404))
    assert run(netdiag.t_rdap_whois(None, {"host": "nope.example"})) == "ERROR: RDAP 404 for nope.example"
    with pytest.raises(ValueError):
        run(netdiag.t_rdap_whois(None, {"query": "bad host!"}))


# ---------------------------------------------------------------- ip_geo
def test_geo_own_ip_and_failure(monkeypatch):
    seen = []

    def handler(r):
        seen.append(str(r.url))
        return httpx.Response(200, json={"success": False, "message": "rate limited"})
    http(monkeypatch, handler)
    monkeypatch.setenv("AVRONE_GEOIP_URL", "https://geo.invalid/")
    assert run(netdiag.t_ip_geo(None, {})) == "ERROR: rate limited"
    assert seen == ["https://geo.invalid/"]


def test_geo_failure_without_message(monkeypatch):
    http(monkeypatch, lambda r: httpx.Response(200, json={"success": False}))
    assert run(netdiag.t_ip_geo(None, {"host": "1.1.1.1"})) == "ERROR: lookup failed"


def test_geo_resolves_hostname_and_notes_target(monkeypatch):
    async def fake_gai(self, host, port, family=0, **kw):
        return [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 0))]
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_gai)
    http(monkeypatch, lambda r: httpx.Response(200, json={"ip": "93.184.216.34", "city": "X", "connection": {}}))
    out = run(netdiag.t_ip_geo(None, {"host": "example.com"}))
    assert "ip: 93.184.216.34 (example.com)" in out and "asn: ASNone" in out and "isp: None" in out


def test_geo_hostname_resolving_private(monkeypatch):
    async def fake_gai(self, host, port, family=0, **kw):
        return [(socket.AF_INET, 1, 6, "", ("192.168.1.10", 0))]
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", fake_gai)
    assert "private address" in run(netdiag.t_ip_geo(None, {"host": "router.lan"}))


def test_geo_resolver_error_propagates(monkeypatch):
    async def boom(self, host, port, family=0, **kw):
        raise socket.gaierror(-2, "nope")
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", boom)
    with pytest.raises(socket.gaierror):
        run(netdiag.t_ip_geo(None, {"host": "nx.invalid"}))


def test_net_tools_table_shape():
    names = [t[0] for t in netdiag.NET_TOOLS]
    assert names == ["dns_lookup", "tcp_ping", "traceroute", "rdap_whois", "ip_geo"]
    assert all(callable(t[3]) and t[2].startswith("{") for t in netdiag.NET_TOOLS)
