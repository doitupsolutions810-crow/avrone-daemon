import asyncio
from types import SimpleNamespace
import pytest
from avrone import recon

def ctx():
    return SimpleNamespace(core=SimpleNamespace(codex=SimpleNamespace(emit=lambda *a, **k: None)),
                           run_id="r", name="Recon-1", depth=0)

def run(c): return asyncio.run(c)

def test_host_arg_url_stripping(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")  # v0.22 recon is default-deny; allow any public host to exercise stripping
    cases = [
        ({"host": "https://example.com:443/path?x=1"}, "example.com"),
        ({"domain": "http://sub.api.com/"}, "sub.api.com"),
        ({"target": "evil.com:8443"}, "evil.com"),
        ({"host": "  leading.space.com  "}, "leading.space.com"),
    ]
    for arg, expected in cases:
        h, err = recon._host_arg(arg)
        assert h == expected and err is None

def test_allowlist_suffix_logic(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "example.com, .co.uk")
    assert "not in AVRONE_RECON_ALLOWLIST" in run(recon.t_tls_inspect(ctx(), {"host": "evil.com"}))
    async def fake_ok(c, a): return "OK - no network"
    monkeypatch.setattr(recon, "t_dns_enum", fake_ok)
    monkeypatch.setattr(recon, "t_cert_transparency", fake_ok)
    monkeypatch.setattr(recon, "t_http_fingerprint", fake_ok)
    for h in ("api.example.com", "foo.co.uk", "bar.co.uk"):
        for fn in (recon.t_dns_enum, recon.t_cert_transparency, recon.t_http_fingerprint):
            assert "not in AVRONE_RECON_ALLOWLIST" not in run(fn(ctx(), {"host": h}))

def test_port_cap_authorized_gate(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "allow")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "*")  # v0.22 default-deny; cap/gate checks run only on an allowed host
    out = run(recon.t_port_scan(ctx(), {"host": "example.com"}))
    assert "authorized" in out and "refused" in out
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": list(range(50))}))  # >32
    assert "at most 32" in out
    async def mock_open(*args, **kwargs):
        raise OSError("mock offline - no real network")
    monkeypatch.setattr(recon, "_resolve_ips", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(asyncio, "open_connection", mock_open)
    out = run(recon.t_port_scan(ctx(), {"host": "example.com", "authorized": True, "ports": [80, 443]}))
    assert "port_scan" in out and "open: none" in out

def test_deny_switch_per_tool(monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "deny")
    for fn in (recon.t_tls_inspect, recon.t_http_fingerprint, recon.t_cert_transparency, recon.t_dns_enum, recon.t_port_scan):
        args = {"host": "example.com", "authorized": True} if fn is recon.t_port_scan else {"host": "example.com"}
        out = run(fn(ctx(), args))
        assert "reconnaissance is disabled on this node" in out

def test_registration_extra():
    from avrone.tools import ToolRegistry
    reg = ToolRegistry()
    recon.register_recon_tools(reg)
    for name in ("tls_inspect", "http_fingerprint", "port_scan", "cert_transparency", "dns_enum"):
        assert reg.get(name) is not None
