"""Round 118: AVRONE_ALLOW_NETWORK=0 is honoured by every builtin network tool.

v0.110: netdiag (dns_lookup, tcp_ping, traceroute, rdap_whois, ip_geo), recon (tls_inspect,
http_fingerprint, port_scan, cert_transparency, dns_enum) and osint_domain never checked
allow_network: with network off they still resolved names, opened TCP connections and made
HTTPS calls (probe /workspace/work/netoff.py patches socket.connect/getaddrinfo).
"""
from __future__ import annotations

import asyncio
import socket

import pytest

from avrone import tools as toolsmod
from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.tools_extra import register_extra_tools
from avrone.trinity import AgentContext, TrinityCore

ARGS = {
    "dns_lookup": {"host": "example.com"}, "tcp_ping": {"host": "example.com", "count": 1},
    "traceroute": {"host": "example.com"}, "rdap_whois": {"query": "example.com"}, "ip_geo": {},
    "tls_inspect": {"host": "example.com"}, "http_fingerprint": {"url": "https://example.com/"},
    "port_scan": {"host": "example.com", "ports": [443], "authorized": True},
    "cert_transparency": {"domain": "example.com"}, "dns_enum": {"host": "example.com"},
    "osint_domain": {"domain": "example.com"},
}


class _LLM:
    async def chat(self, messages, **kw):
        return '{"final": "ok"}'


@pytest.fixture
def netoff(tmp_path, monkeypatch):
    monkeypatch.setenv("AVRONE_RECON", "on")
    monkeypatch.setenv("AVRONE_RECON_ALLOWLIST", "example.com")
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"; s.allow_network = False
    s.workspace.mkdir(parents=True)
    core = TrinityCore(s, _LLM(), LocalRunner(s), Codex(s.data_dir))
    register_extra_tools(core.tools)
    seen: list[str] = []

    def no_dns(host, *a, **k):
        seen.append(f"dns {host}")
        raise OSError("blocked by test")
    real_connect = socket.socket.connect

    def no_connect(self, addr):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            seen.append(f"connect {addr}")
            raise OSError("blocked by test")
        return real_connect(self, addr)

    async def no_exec(*argv, **kw):
        seen.append(f"exec {argv[0]}")
        raise OSError("blocked by test")
    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(socket.socket, "connect", no_connect)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_exec)
    return core, seen


@pytest.mark.parametrize("name", sorted(ARGS))
def test_network_tool_refused_when_network_off(netoff, name):
    core, seen = netoff
    assert core.tools.get(name) is not None, name
    ctx = AgentContext(core=core, run_id="r118", name="P", depth=0)
    out = asyncio.run(asyncio.wait_for(core.tools.call(ctx, name, dict(ARGS[name])), 30))
    assert not seen, f"{name} reached the network with allow_network off: {seen[:3]}"
    assert "network access is disabled" in out, out[:200]


def test_registry_names_exist():
    nt = getattr(toolsmod, "NETWORK_TOOLS", None)
    if nt is None:
        pytest.fail("tools.NETWORK_TOOLS missing")
    assert set(ARGS) <= set(nt)


def test_network_on_still_runs(tmp_path):
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"; s.allow_network = True
    s.workspace.mkdir(parents=True)
    core = TrinityCore(s, _LLM(), LocalRunner(s), Codex(s.data_dir))
    register_extra_tools(core.tools)
    ctx = AgentContext(core=core, run_id="r118", name="P", depth=0)
    out = asyncio.run(core.tools.call(ctx, "tcp_ping", {"host": "-bad"}))
    assert "network access is disabled" not in out
