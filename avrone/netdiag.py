"""Network diagnostics for AVRONE agents: real, bounded, from AVRONE's own host.

Tools
    dns_lookup   A/AAAA/MX/TXT/NS/CNAME records (system resolver + DNS-over-HTTPS)
    tcp_ping     TCP connect latency to one host:port (works without ICMP/root)
    traceroute   hop-by-hop route (system traceroute/tracepath/mtr when installed)
    rdap_whois   domain or IP registration data via RDAP (the modern WHOIS)
    ip_geo       public IP / hostname geolocation, ASN and network owner

Deliberately bounded: one host and one port per call, a capped number of probes,
no port sweeping. Targets are whatever AVRONE's host can reach.
Env: AVRONE_DOH_URL (default Cloudflare), AVRONE_GEOIP_URL (default ipwho.is).
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import shutil
import socket
import time
from typing import TYPE_CHECKING, Any

from .tools import Tool
from .tools_extra import _client

if TYPE_CHECKING:
    from .trinity import AgentContext

HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]_]{1,253}\Z")
RTYPES = {"A", "AAAA", "MX", "TXT", "NS", "CNAME", "SOA", "PTR", "CAA", "SRV"}


def _host(args: dict[str, Any], key: str = "host") -> str:
    h = str(args.get(key, "")).strip()
    h = re.sub(r"^[a-z]+://", "", h).split("/")[0]
    if not h or not HOST_RE.match(h) or h.startswith("-"):
        raise ValueError(f"invalid host: {h!r}")
    return h.strip("[]")


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


async def t_dns_lookup(ctx: "AgentContext", args: dict[str, Any]) -> str:
    host = _host(args)
    rtype = str(args.get("type", "A")).upper()
    if rtype not in RTYPES:
        return f"ERROR: type must be one of {sorted(RTYPES)}"
    out = [f"{host} {rtype}"]
    if rtype in ("A", "AAAA"):
        fam = socket.AF_INET if rtype == "A" else socket.AF_INET6
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, family=fam)
            out.append("system: " + ", ".join(sorted({i[4][0] for i in infos})))
        except socket.gaierror as e:
            out.append(f"system: {e}")
    doh = os.environ.get("AVRONE_DOH_URL", "https://cloudflare-dns.com/dns-query")
    try:
        async with _client(15) as c:
            r = await c.get(doh, params={"name": host, "type": rtype}, headers={"accept": "application/dns-json"})
            d = r.json()
        answers = d.get("Answer") or []
        status = {0: "NOERROR", 2: "SERVFAIL", 3: "NXDOMAIN"}.get(d.get("Status"), str(d.get("Status")))
        out.append(f"doh: {status}")
        out += [f"  {a.get('name')} ttl={a.get('TTL')} {a.get('data')}" for a in answers[:30]]
    except Exception as e:  # noqa: BLE001
        out.append(f"doh: unavailable ({type(e).__name__})")
    return "\n".join(out)


async def t_tcp_ping(ctx: "AgentContext", args: dict[str, Any]) -> str:
    host = _host(args)
    from .argnum import tool_int  # round 45: "abc", 1e999 and NaN raised out of the tool
    port = tool_int(args.get("port"), 443, bad=None)
    if port is None or isinstance(args.get("port"), float) and not float(args.get("port")).is_integer():
        return "ERROR: port must be an integer 1..65535"
    count = tool_int(args.get("count"), 4, 1, 10)
    if not 1 <= port <= 65535:
        return "ERROR: port out of range"
    times: list[float] = []
    lines = [f"tcp_ping {host}:{port}"]
    for i in range(count):
        t0 = time.perf_counter()
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
            ms = (time.perf_counter() - t0) * 1000
            peer = w.get_extra_info("peername")
            w.close()
            times.append(ms)
            lines.append(f"  seq={i} {peer[0] if peer else host} {ms:.1f} ms")
        except (OSError, asyncio.TimeoutError) as e:
            lines.append(f"  seq={i} failed: {type(e).__name__} {e}".rstrip())
        if i < count - 1:
            await asyncio.sleep(0.2)
    if times:
        lines.append(f"{len(times)}/{count} ok, min/avg/max = {min(times):.1f}/{sum(times)/len(times):.1f}/{max(times):.1f} ms")
    else:
        lines.append(f"0/{count} ok")
    return "\n".join(lines)


async def t_traceroute(ctx: "AgentContext", args: dict[str, Any]) -> str:
    host = _host(args)
    from .argnum import tool_int
    hops = tool_int(args.get("max_hops"), 20, 1, 30)
    for binary, argv in (
        ("mtr", ["mtr", "-r", "-n", "-c", "1", "-m", str(hops), host]),
        ("traceroute", ["traceroute", "-n", "-q", "1", "-w", "2", "-m", str(hops), host]),
        ("tracepath", ["tracepath", "-n", "-m", str(hops), host]),
    ):
        if shutil.which(binary):
            proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
            except asyncio.TimeoutError:
                proc.kill()
                return f"ERROR: {binary} timed out"
            return f"[{binary}]\n" + out.decode(errors="replace")[-8000:]
    return ("ERROR: no traceroute binary on this host. Install one (apt install traceroute | mtr-tiny, "
            "or brew install mtr), or run it via docker_run with an image that has it. tcp_ping still works.")


async def t_rdap_whois(ctx: "AgentContext", args: dict[str, Any]) -> str:
    q = _host(args, "query") if args.get("query") else _host(args)
    kind = "ip" if _is_ip(q) else "domain"
    async with _client(20) as c:
        r = await c.get(f"https://rdap.org/{kind}/{q}", headers={"accept": "application/rdap+json"})
    if r.status_code != 200:
        return f"ERROR: RDAP {r.status_code} for {q}"
    d = r.json()
    lines = [f"RDAP {kind} {q}"]
    for k in ("ldhName", "name", "handle", "startAddress", "endAddress", "country", "type"):
        if d.get(k):
            lines.append(f"  {k}: {d[k]}")
    for ev in d.get("events", [])[:6]:
        lines.append(f"  {ev.get('eventAction')}: {ev.get('eventDate')}")
    if d.get("status"):
        lines.append(f"  status: {', '.join(d['status'][:6])}")
    for ns in d.get("nameservers", [])[:6]:
        lines.append(f"  nameserver: {ns.get('ldhName')}")
    for ent in d.get("entities", [])[:6]:
        vcard = ent.get("vcardArray", [None, []])[1]
        fn = next((v[3] for v in vcard if v and v[0] == "fn"), None)
        lines.append(f"  entity: {', '.join(ent.get('roles', []))} {fn or ent.get('handle', '')}".rstrip())
    return "\n".join(lines)


async def t_ip_geo(ctx: "AgentContext", args: dict[str, Any]) -> str:
    target = str(args.get("host", "")).strip()
    ip = ""
    if target:
        h = _host(args)
        if _is_ip(h):
            ip = h
        else:
            infos = await asyncio.get_running_loop().getaddrinfo(h, None, family=socket.AF_INET)
            ip = infos[0][4][0]
        if ipaddress.ip_address(ip).is_private:
            return f"{h} -> {ip}: private address, no public geolocation"
    base = os.environ.get("AVRONE_GEOIP_URL", "https://ipwho.is/")
    async with _client(15) as c:
        r = await c.get(base + ip)
    d = r.json()
    if d.get("success") is False:
        return f"ERROR: {d.get('message', 'lookup failed')}"
    conn = d.get("connection") or {}
    parts = [
        f"ip: {d.get('ip')}" + (f" ({target})" if target and target != d.get("ip") else ""),
        f"location: {d.get('city')}, {d.get('region')}, {d.get('country')} ({d.get('latitude')}, {d.get('longitude')})",
        f"asn: AS{conn.get('asn')} {conn.get('org') or ''}".rstrip(),
        f"isp: {conn.get('isp')}",
        "note: IP geolocation is approximate (usually city-level at best).",
    ]
    return "\n".join(parts)


NET_TOOLS = [
    ("dns_lookup", "resolve DNS records for a host (system resolver + DNS-over-HTTPS).",
     '{"host": str, "type"?: "A"|"AAAA"|"MX"|"TXT"|"NS"|"CNAME"|"SOA"|"PTR"|"CAA"|"SRV"}', t_dns_lookup),
    ("tcp_ping", "measure TCP connect latency to one host:port (default 443, up to 10 probes).",
     '{"host": str, "port"?: int, "count"?: int}', t_tcp_ping),
    ("traceroute", "trace the hop-by-hop route to a host (needs mtr/traceroute/tracepath on the host).",
     '{"host": str, "max_hops"?: int}', t_traceroute),
    ("rdap_whois", "registration data for a domain or IP via RDAP (registrar, dates, owner network).",
     '{"query": str}', t_rdap_whois),
    ("ip_geo", "approximate geolocation, ASN and ISP of a public IP or hostname (blank = AVRONE's own public IP).",
     '{"host"?: str}', t_ip_geo),
]


def register_net_tools(registry) -> None:
    for name, desc, args_doc, fn in NET_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
