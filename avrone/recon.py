"""Advanced reconnaissance (v0.10): deeper, still bounded, authorized-only recon.

Scope and honesty:
  * These tools work on hosts/domains you name, one target per call. No sweeps, no
    ranges, no crawling. port_scan refuses to run without authorized=true and is
    capped to a small port list, because scanning hosts you do not own is abuse.
  * Everything uses public data or a direct connection to the named target: TLS
    certificates, HTTP response headers, public DNS, and certificate-transparency
    logs (crt.sh). Nothing here touches private infrastructure, satellites, towers,
    or any capability AVRONE does not have.
  * Every call is logged to the Codex action log.

Tools: tls_inspect, http_fingerprint, port_scan, cert_transparency, dns_enum.
Env: AVRONE_RECON=allow|deny (default allow), AVRONE_RECON_ALLOWLIST (comma hosts/suffixes; unset/empty = deny all, "*" = any public host).

AUDIT NOTE (v0.13 hardening) -- the guardrails, in the order every tool applies them:
  1. Kill switch. AVRONE_RECON unset/empty or allow|on|true|yes|1 = enabled; deny|off|
     false|no|0|disabled = disabled; ANY other value fails CLOSED (disabled).
  2. Target syntax (_host_arg). Exactly one DNS hostname. Refused before anything else:
     IP literals of any form (dotted, IPv6, bracketed, integer/hex/short-form such as
     "127.1" or "0x7f.1"), CIDR blocks, comma/space separated lists, wildcards, userinfo,
     single-label names, malformed labels, non-http(s) schemes.
  3. Private-target refusal (_reject_private). localhost/loopback, RFC1918/ULA private,
     link-local, CGNAT, reserved/unspecified/multicast, cloud metadata endpoints
     (169.254.169.254, metadata.google.internal, ...), internal-only suffixes (.local,
     .internal, .localdomain, .home.arpa, .lan, ...) and private IPs embedded in a name
     (10.0.0.1.nip.io). Applies with or without an allowlist.
  4. Allowlist. AVRONE_RECON_ALLOWLIST exact host or true subdomain. Default-deny since
     v0.21: unset/blank allows NOTHING; the operator must list their own/authorized domains,
     or set "*" to explicitly allow any public host. Unparseable (", ,") fails CLOSED.
  5. Connect-time re-check. Tools that connect to the target (tls_inspect, port_scan,
     http_fingerprint) resolve the name first and refuse if ANY address is non-public,
     then connect to the vetted address; http_fingerprint re-checks every redirect hop and
     (round 36) pins each hop's connection to its vetted address (_PinnedTransport), so a
     DNS-rebinding name cannot swap in a private address between the check and the connect.
  6. Output caps: CT rows/names, DNS types/answers/answer length, SAN and header lengths.
  7. Audit trail. Every call is written to the Codex action log: the tool's own event
     (recon.tls / recon.http / recon.portscan / recon.ct / recon.dns / osint.domain),
     with an "error" field when the probe failed, and a recon.refused event (tool,
     target, reason) for every refusal by any gate above.
No guardrail may be loosened; sweeps, ranges and multi-target calls are out of scope.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
import ssl
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .tools import Tool

if TYPE_CHECKING:
    from .trinity import AgentContext

# A deliberately small, common-service port set. Not a full scanner.
COMMON_PORTS = [21, 22, 25, 53, 80, 110, 143, 443, 465, 587, 993, 995, 3306, 3389, 5432, 6379, 8080, 8443]
SERVICE = {21: "ftp", 22: "ssh", 25: "smtp", 53: "dns", 80: "http", 110: "pop3", 143: "imap",
           443: "https", 465: "smtps", 587: "submission", 993: "imaps", 995: "pop3s",
           3306: "mysql", 3389: "rdp", 5432: "postgres", 6379: "redis", 8080: "http-alt", 8443: "https-alt"}


# Output caps (kept small on purpose; raise only with review).
CT_MAX_ROWS = 5000        # crt.sh rows processed
CT_MAX_NAMES = 2000       # unique names kept
CT_SHOW = 80              # names printed
DNS_MAX_TYPES = 10        # record types queried per call
DNS_MAX_ANSWERS = 12      # answers printed per type
DNS_MAX_ANSWER_LEN = 255  # characters per answer (long TXT records are truncated)


def _emit(ctx: "AgentContext", kind: str, **data: Any) -> None:
    try:
        ctx.core.codex.emit(ctx.run_id, kind, ctx.name, **data)
    except Exception:  # noqa: BLE001
        pass


_DENY_WORDS = {"deny", "off", "false", "no", "0", "disabled", "disable"}
_ALLOW_WORDS = {"", "allow", "on", "true", "yes", "1", "enabled", "enable"}


def _denied() -> str | None:
    mode = os.getenv("AVRONE_RECON", "allow").strip().lower()
    if mode in _DENY_WORDS:
        return "ERROR: reconnaissance is disabled on this node (AVRONE_RECON=deny)"
    if mode not in _ALLOW_WORDS:  # unknown value: fail closed, never open
        return f"ERROR: reconnaissance is disabled on this node (unrecognised AVRONE_RECON={mode[:40]!r})"
    return None


def _allowlist() -> list[str] | None:
    """Default-deny (v0.21). None = explicit "*" (any public host). [] = unset/blank or
    unparseable -> deny all. Otherwise the exact hosts / true-subdomain suffixes listed."""
    raw = os.getenv("AVRONE_RECON_ALLOWLIST", "")
    if raw.strip() == "*":
        return None
    if not raw.strip():
        return []
    out = []
    for h in raw.split(","):
        a = _norm_allow_entry(h)
        if a and "*" not in a:
            out.append(a)
    return out


def _norm_allow_entry(h: str) -> str:
    """One AVRONE_RECON_ALLOWLIST entry as the bare IDNA host it means. "https://MySite.com/",
    "mysite.com:443", "user@mysite.com" and "bücher.de" used to be kept verbatim and never matched
    anything (silently allowing nothing). Normalisation only narrows to the host the operator wrote."""
    a = h.strip().lower()
    a = re.sub(r"^[a-z][a-z0-9+.-]*://", "", a)
    a = re.split(r"[/?#]", a, maxsplit=1)[0]
    a = a.rsplit("@", 1)[-1]
    if a.count(":") == 1:
        a = a.split(":")[0]
    a = a.strip().strip(".")
    if not a or a.startswith("[") or ":" in a:
        return ""  # an IP literal never matches a hostname target
    try:
        a = a.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return a.strip(".")


def _allowed_host(host: str) -> bool:
    allow = _allowlist()
    if allow is None:
        return True
    h = host.lower().rstrip(".")
    # exact host or a true subdomain only: "evilmysite.com" must NOT match "mysite.com"
    return any(h == a or h.endswith("." + a) for a in allow)


# ------------------------------------------------------------------ private-target refusal
# Names that only ever mean "this machine", "the local network", or a cloud metadata service.
_BLOCKED_NAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "broadcasthost",
    "metadata", "metadata.google.internal", "metadata.goog", "metadata.azure.com",
    "instance-data", "instance-data.ec2.internal", "kubernetes.default", "kubernetes.default.svc",
}
_BLOCKED_SUFFIXES = (
    "localhost", "local", "localdomain", "internal", "intranet", "lan", "home", "corp",
    "private", "home.arpa", "in-addr.arpa", "ip6.arpa", "svc.cluster.local", "cluster.local",
    # public wildcard names that resolve every subdomain to 127.0.0.1 by design
    "localtest.me", "lvh.me", "vcap.me", "lacolhost.com",
)
# public wildcard-DNS services that turn an address written in the name into an A record
_IP_IN_NAME_SERVICES = ("nip.io", "sslip.io", "xip.io", "traefik.me", "nip.direct",
                        # DNS-rebinding test services: the address (hex or dotted) is in the name
                        "rbndr.us", "1u.ms")
# a dotted quad glued to other text inside the name: customer-10.0.0.1.nip.io, make-127.0.0.1-rr.1u.ms
_GLUED_QUAD = re.compile(r"(?:^|[.-])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?=$|[.-])")
_DASH_QUAD = re.compile(r"(?<![0-9])(\d{1,3})-(\d{1,3})-(\d{1,3})-(\d{1,3})(?![0-9])")
_LABEL = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")
_TLD = re.compile(r"^(?:[a-z][a-z0-9-]*[a-z0-9]|[a-z]|xn--[a-z0-9-]+)$")


def _non_public_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (not ip.is_global) or ip.is_multicast or ip.is_private or ip.is_loopback \
        or ip.is_link_local or ip.is_reserved or ip.is_unspecified \
        or bool(getattr(ip, "is_site_local", False))  # fec0::/10: deprecated, still routed internally


def _parse_ip(host: str):
    """Parse anything a socket would treat as an address, including inet_aton short forms."""
    h = host.strip("[]")
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    try:  # "127.1", "0x7f.1", "2130706433", "017700000001" all connect to 127.0.0.1
        return ipaddress.IPv4Address(socket.inet_aton(h))
    except (OSError, ValueError):
        return None


def _reject_private(host: str) -> str | None:
    """Refuse loopback/private/link-local/metadata/internal targets. Pure string check, no I/O."""
    h = (host or "").strip().lower().rstrip(".")
    if not h:
        return "ERROR: empty target"
    ip = _parse_ip(h)
    if ip is not None:
        if _non_public_ip(ip):
            return f"ERROR: {host} is a private/loopback/link-local/metadata address; refused"
        return f"ERROR: {host} is an IP literal; recon takes one public domain name, not addresses"
    if h in _BLOCKED_NAMES or any(h == s or h.endswith("." + s) for s in _BLOCKED_SUFFIXES):
        return f"ERROR: {host} is a local/internal/metadata hostname; refused"
    labels = h.split(".")
    for i in range(len(labels) - 3):  # private IPs embedded in a name: 10.0.0.1.nip.io
        quad = labels[i:i + 4]
        if all(x.isdigit() for x in quad):
            try:
                if _non_public_ip(ipaddress.IPv4Address(".".join(quad))):
                    return f"ERROR: {host} embeds a private address; refused"
            except ValueError:
                pass
    # the dash form (10-0-0-1.sslip.io, app-192-168-1-5.nip.io) and, on the wildcard-DNS services,
    # the 8-hex-digit form (7f000001.nip.io). Before, only the dotted form was caught here; the
    # connecting tools still re-check what the name resolves to, this refuses it up front.
    for label in labels:
        for quad in _DASH_QUAD.findall(label):
            try:
                if _non_public_ip(ipaddress.IPv4Address(".".join(str(int(x)) for x in quad))):
                    return f"ERROR: {host} embeds a private address; refused"
            except ValueError:
                pass
    for quad in _GLUED_QUAD.findall(h):
        try:
            if _non_public_ip(ipaddress.IPv4Address(".".join(str(int(x)) for x in quad))):
                return f"ERROR: {host} embeds a private address; refused"
        except ValueError:
            pass
    if any(h == s or h.endswith("." + s) for s in _IP_IN_NAME_SERVICES):
        # sslip.io/nip.io also read IPv6 with dashes for colons, "::" as "--": 0--1.sslip.io is ::1,
        # fe80--1 link-local, ffff-7f00-1 nothing public. Only the uncompressed 8-group form was caught.
        for label in labels:
            if "-" not in label or not re.fullmatch(r"[0-9a-f-]+", label):
                continue
            try:
                ip6 = ipaddress.IPv6Address(label.replace("-", ":"))
            except ValueError:
                continue
            if _non_public_ip(ip6):
                return f"ERROR: {host} embeds a private address; refused"
        for label in labels:
            for m in re.findall(r"(?<![0-9a-f])([0-9a-f]{8})(?![0-9a-f])", label):
                if _non_public_ip(ipaddress.IPv4Address(int(m, 16))):
                    return f"ERROR: {host} embeds a private address; refused"
    return None


def _reject_resolved(host: str, ips: list[str]) -> str | None:
    if not ips:
        return f"ERROR: {host} did not resolve; refused"
    for a in ips:
        try:
            ip = ipaddress.ip_address(a.split("%")[0])
        except ValueError:
            return f"ERROR: {host} resolved to an unparseable address; refused"
        if _non_public_ip(ip):
            return f"ERROR: {host} resolves to a private/loopback/link-local/metadata address ({a}); refused"
    return None


def _resolve_ips(host: str) -> list[str]:
    """Blocking resolution of one name (tests replace this; it is the only DNS the tools do themselves)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    return sorted({i[4][0] for i in infos})


async def _vet_resolved(host: str) -> tuple[str | None, str | None]:
    """Resolve once, refuse non-public answers, and return a vetted address to connect to."""
    ips = await asyncio.get_running_loop().run_in_executor(None, _resolve_ips, host)
    err = _reject_resolved(host, ips)
    return (None, err) if err else (ips[0], None)


def _syntax_error(raw: str) -> str | None:
    if any(c.isspace() for c in raw) or "," in raw or ";" in raw:
        return "ERROR: one target per call; lists of hosts are refused"
    if any(c in raw for c in "*{}"):
        return "ERROR: wildcards and patterns are refused; name exactly one host"
    if re.fullmatch(r"[^/]+/\d{1,3}", raw):
        return f"ERROR: {raw} looks like a CIDR range; ranges are refused"
    return None


def _host_arg(args: dict[str, Any]) -> tuple[str, str | None]:
    raw = str(args.get("host") or args.get("domain") or args.get("target") or "").strip()
    if not raw:
        return "", 'ERROR: give {"host": "example.com"}'
    if len(raw) > 2048:
        return raw[:64], "ERROR: target too long"
    if (err := _syntax_error(raw)):
        return raw, err
    rest = raw
    m = re.match(r"^([a-z][a-z0-9+.-]*)://", rest, re.I)
    if m:
        if m.group(1).lower() not in ("http", "https"):
            return raw, f"ERROR: scheme {m.group(1)!r} refused; use a bare host or http(s) URL"
        rest = rest[m.end():]
    authority = re.split(r"[/?#]", rest, maxsplit=1)[0]
    if "@" in authority:
        return raw, "ERROR: credentials/userinfo in the target are refused"
    if authority.startswith("[") or authority.count(":") > 1:
        return raw, f"ERROR: {authority} is an IP literal; recon takes one public domain name, not addresses"
    host = authority.split(":")[0].strip().rstrip(".").lower()
    if not host:
        return "", 'ERROR: give {"host": "example.com"}'
    if (err := _reject_private(host)):
        return host, err
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return host, f"ERROR: {host} is not a valid hostname"
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(_LABEL.match(x) for x in labels) \
            or not _TLD.match(labels[-1]):
        return host, f"ERROR: {host} is not a valid public hostname (need e.g. example.com)"
    if (err := _reject_private(host)):  # re-check after IDNA normalisation
        return host, err
    if not _allowed_host(host):
        return host, (f"ERROR: {host} is not in AVRONE_RECON_ALLOWLIST" + ("" if _allowlist() else
                      " (recon is default-deny: list the domains you own or are authorized on, or \"*\" for any public host)"))
    return host, None


def _strict_int(v: Any) -> int:
    """An integer argument: 443, "443", 443.0. Refuses True (int(True) == 1 used to scan port 1),
    443.9 (silently truncated), "443.5", NaN and None."""
    if isinstance(v, bool) or v is None:
        raise ValueError("not an integer")
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError("not an integer")
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, str) and re.fullmatch(r"\s*[+-]?\d{1,6}\s*", v):
        return int(v)
    raise ValueError("not an integer")


def _authorized(v: Any) -> bool:
    """port_scan's authorized flag. Only true / "true" / "yes" / 1 count: bool("false") is True,
    so {"authorized": "false"} (or "no", "0") used to authorize a scan."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and not isinstance(v, bool):
        return v == 1
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return False


def _port_arg(args: dict[str, Any], default: int) -> tuple[int, str | None]:
    try:
        port = _strict_int(args.get("port", default))
    except (TypeError, ValueError):
        return 0, "ERROR: port must be an integer"
    if not 1 <= port <= 65535:
        return port, "ERROR: port must be between 1 and 65535"
    return port, None


REFUSED_KIND = "recon.refused"


def _refuse(ctx: "AgentContext", tool: str, target: Any, err: str) -> str:
    """Audit a refusal (kill switch, syntax, private target, allowlist, authorization) and return it."""
    _emit(ctx, REFUSED_KIND, tool=tool, target=str(target or "")[:200], reason=err[:300])
    return err


def _gate(ctx: "AgentContext", tool: str, args: dict[str, Any]) -> tuple[str, str | None]:
    """The single entry gate every recon/OSINT tool passes: kill switch, target, private, allowlist."""
    raw = args.get("host") or args.get("domain") or args.get("target") or ""
    if (d := _denied()):
        return "", _refuse(ctx, tool, raw, d)
    host, err = _host_arg(args)
    if not err:
        err = _reject_private(host)
    if err:
        return host, _refuse(ctx, tool, host or raw, err)
    return host, None


class _Refused(Exception):
    """A guardrail tripped mid-call (resolution or redirect hop); message is the ERROR text."""


def _request_host(request) -> str:
    raw = getattr(request.url, "raw_host", b"") or b""
    host = raw.decode("ascii", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return host.lower().rstrip(".")


try:
    import httpx as _httpx

    class _PinnedTransport(_httpx.AsyncBaseTransport):
        """Connects each request to the address the guard vetted for its host, never to a fresh DNS
        answer. Before, http_fingerprint vetted the name and then let httpx resolve it again, so a
        DNS-rebinding name (public on the first lookup, 127.0.0.1 / 169.254.169.254 on the second)
        reached a private address. The Host header and TLS SNI/certificate check keep the name."""

        def __init__(self, pins: dict[str, str]):
            self.pins = pins
            self.inner = _httpx.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            host = _request_host(request)
            ip = self.pins.get(host)
            if not ip:
                raise _Refused(f"ERROR: {host or 'request'} was not vetted before connecting; refused")
            logical = request.url
            request.headers.setdefault("host", logical.netloc.decode("ascii"))
            request.url = logical.copy_with(host=ip)
            if logical.scheme == "https":
                request.extensions = {**request.extensions, "sni_hostname": host}
            try:
                return await self.inner.handle_async_request(request)
            finally:
                request.url = logical  # redirects and r.url keep the hostname

        async def aclose(self) -> None:
            await self.inner.aclose()
except ImportError:  # pragma: no cover - httpx is a hard dependency
    _PinnedTransport = None  # type: ignore[assignment,misc]


async def t_tls_inspect(ctx: "AgentContext", args: dict[str, Any]) -> str:
    host, err = _gate(ctx, "tls_inspect", args)
    if err:
        return err
    port, perr = _port_arg(args, 443)
    if perr:
        return _refuse(ctx, "tls_inspect", host, perr)

    def _get():
        # resolve + vet inside the worker thread, then connect to the vetted address only
        ips = _resolve_ips(host)
        if (rerr := _reject_resolved(host, ips)):
            raise _Refused(rerr)
        ctx_ssl = ssl.create_default_context()
        last: Exception | None = None
        for ip in ips[:4]:  # every address is vetted; an unreachable first (often IPv6) used to fail the call
            try:
                sock = socket.create_connection((ip, port), timeout=8)
            except OSError as e:
                last = e
                continue
            with sock:
                with ctx_ssl.wrap_socket(sock, server_hostname=host) as ss:
                    return ss.getpeercert(), ss.version(), ss.cipher()
        raise last or OSError("no address to connect to")

    try:
        cert, ver, cipher = await asyncio.to_thread(_get)
    except _Refused as e:
        return _refuse(ctx, "tls_inspect", host, str(e))
    except (OSError, ssl.SSLError) as e:
        _emit(ctx, "recon.tls", host=host, port=port, error=type(e).__name__)
        return f"ERROR: TLS to {host}:{port}: {type(e).__name__}: {e}"
    _emit(ctx, "recon.tls", host=host, port=port)
    subj = dict(x[0] for x in cert.get("subject", []))
    issuer = dict(x[0] for x in cert.get("issuer", []))
    sans = [str(v)[:253] for t, v in cert.get("subjectAltName", []) if t == "DNS"]
    not_after = cert.get("notAfter", "")
    days = ""
    try:
        exp = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = f" ({(exp - datetime.now(timezone.utc)).days} days left)"
    except ValueError:
        pass
    return (f"{host}:{port}  {ver}  {cipher[0] if cipher else ''}\n"
            f"subject CN: {subj.get('commonName', '?')}\n"
            f"issuer: {issuer.get('organizationName', issuer.get('commonName', '?'))}\n"
            f"valid: {cert.get('notBefore', '?')} -> {not_after}{days}\n"
            f"SANs ({len(sans)}): {', '.join(sans[:40])}")


async def t_http_fingerprint(ctx: "AgentContext", args: dict[str, Any]) -> str:
    host, err = _gate(ctx, "http_fingerprint", args)
    if err:
        return err
    import httpx
    scheme = str(args.get("scheme", "https")).strip().lower()
    if scheme not in ("http", "https"):
        return _refuse(ctx, "http_fingerprint", host, "ERROR: scheme must be http or https")
    _, rerr = await _vet_resolved(host)
    if rerr:
        return _refuse(ctx, "http_fingerprint", host, rerr)
    url = f"{scheme}://{host}/"
    pins: dict[str, str] = {}

    async def _guard_hop(request) -> None:
        # every request, including each redirect hop, must stay on a public, allowed host
        hop = str(request.url.host or "")
        hop_host, herr = _host_arg({"host": hop})
        if herr:
            raise _Refused(f"ERROR: redirect to {hop} refused: {herr.removeprefix('ERROR: ')}")
        addr, herr = await _vet_resolved(hop_host)
        if herr:
            raise _Refused(f"ERROR: redirect to {hop} refused: {herr.removeprefix('ERROR: ')}")
        pins[hop_host] = addr  # the transport connects to exactly this address

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, max_redirects=5,
                                     event_hooks={"request": [_guard_hop]},
                                     transport=_PinnedTransport(pins)) as c:
            r = await c.get(url, headers={"user-agent": "AVRONE-recon/0.10"})
    except _Refused as e:
        return _refuse(ctx, "http_fingerprint", host, str(e))
    except httpx.HTTPError as e:
        _emit(ctx, "recon.http", host=host, error=type(e).__name__)
        return f"ERROR: HTTP to {url}: {type(e).__name__}: {e}"
    _emit(ctx, "recon.http", host=host, status=r.status_code)
    interesting = ["server", "x-powered-by", "via", "x-cache", "cf-ray", "x-amz-cf-id", "content-type",
                   "strict-transport-security", "content-security-policy", "x-frame-options",
                   "set-cookie", "x-served-by", "x-vercel-id", "x-github-request-id"]
    sec = {"strict-transport-security", "content-security-policy", "x-frame-options",
           "x-content-type-options", "referrer-policy"}
    lines = [f"{url} -> {r.status_code}  (final: {r.url})"]
    for h in interesting:
        if h in r.headers:
            v = r.headers[h]
            lines.append(f"  {h}: {v[:160]}")
    missing = sorted(sec - {k.lower() for k in r.headers})
    lines.append(f"missing security headers: {', '.join(missing) if missing else 'none'}")
    return "\n".join(lines)


async def t_port_scan(ctx: "AgentContext", args: dict[str, Any]) -> str:
    raw = args.get("host") or args.get("domain") or args.get("target") or ""
    if (d := _denied()):
        return _refuse(ctx, "port_scan", raw, d)
    if not _authorized(args.get("authorized")):
        return _refuse(ctx, "port_scan", raw,
                       'ERROR: port_scan needs {"authorized": true} to confirm you own or are authorized to scan '
                       "this host. Scanning third-party hosts is abuse and is refused.")
    host, err = _gate(ctx, "port_scan", args)
    if err:
        return err
    ports = args.get("ports") or COMMON_PORTS
    if not isinstance(ports, list) or len(ports) > 32:
        return _refuse(ctx, "port_scan", host, "ERROR: ports must be a list of at most 32 integers")
    try:
        ports = sorted({_strict_int(p) for p in ports})
    except (TypeError, ValueError):
        return _refuse(ctx, "port_scan", host, "ERROR: ports must be integers")
    if any(p < 1 or p > 65535 for p in ports):
        return _refuse(ctx, "port_scan", host, "ERROR: ports must be between 1 and 65535")
    addr, rerr = await _vet_resolved(host)
    if rerr:
        return _refuse(ctx, "port_scan", host, rerr)

    async def probe(p: int) -> tuple[int, bool]:
        try:
            fut = asyncio.open_connection(addr, p)
            r, w = await asyncio.wait_for(fut, timeout=2.0)
            w.close()
            try:
                await w.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return p, True
        except (OSError, asyncio.TimeoutError):
            return p, False

    results = await asyncio.gather(*(probe(p) for p in ports))
    _emit(ctx, "recon.portscan", host=host, ports=len(ports))
    openp = [f"{p}/{SERVICE.get(p, '?')}" for p, ok in sorted(results) if ok]
    return (f"port_scan {host}  ({len(ports)} ports)\n"
            f"open: {', '.join(openp) if openp else 'none'}\n"
            f"closed/filtered: {len(ports) - len(openp)}")


async def t_cert_transparency(ctx: "AgentContext", args: dict[str, Any]) -> str:
    host, err = _gate(ctx, "cert_transparency", args)
    if err:
        return err
    import httpx
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get("https://crt.sh/", params={"q": f"%.{host}", "output": "json"},
                            headers={"user-agent": "AVRONE-recon/0.10"})
            r.raise_for_status()
            rows = r.json()
    except (httpx.HTTPError, ValueError) as e:
        _emit(ctx, "recon.ct", host=host, error=type(e).__name__)
        return f"ERROR: crt.sh lookup for {host}: {type(e).__name__}: {e}"
    if not isinstance(rows, list):
        _emit(ctx, "recon.ct", host=host, error="bad_shape")
        return f"ERROR: crt.sh lookup for {host}: unexpected response shape"
    names: set[str] = set()
    for row in rows[:CT_MAX_ROWS]:
        if not isinstance(row, dict):
            continue
        for n in str(row.get("name_value", ""))[:4096].splitlines():
            n = n.strip().lower().removeprefix("*.").rstrip(".")
            # the domain itself or a true subdomain: "evilexample.com" is NOT under example.com
            if (n == host or n.endswith("." + host)) and len(n) <= 253:
                names.add(n)
                if len(names) >= CT_MAX_NAMES:
                    break
        if len(names) >= CT_MAX_NAMES:
            break
    _emit(ctx, "recon.ct", host=host, found=len(names))
    sub = sorted(names)
    head = "\n".join(sub[:CT_SHOW])
    more = f"\n... (+{len(sub) - CT_SHOW} more)" if len(sub) > CT_SHOW else ""
    return (f"certificate-transparency subdomains of {host}: {len(sub)} unique (public crt.sh data)\n{head}{more}"
            if sub else f"no certificate-transparency records found for {host}")


async def t_dns_enum(ctx: "AgentContext", args: dict[str, Any]) -> str:
    host, err = _gate(ctx, "dns_enum", args)
    if err:
        return err
    import httpx
    types = args.get("types") or ["A", "AAAA", "MX", "NS", "TXT", "CNAME", "SOA"]
    if not isinstance(types, list) or not all(isinstance(t, str) and re.fullmatch(r"[A-Za-z0-9]{1,10}", t)
                                              for t in types):
        return _refuse(ctx, "dns_enum", host, 'ERROR: types must be a list of record type names, e.g. ["A", "MX"]')
    out = [f"DNS for {host}"]
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            for t in types[:DNS_MAX_TYPES]:
                r = await c.get("https://dns.google/resolve", params={"name": host, "type": t},
                                headers={"accept": "application/dns-json"})
                data = r.json()
                answers = data.get("Answer", []) if isinstance(data, dict) else []
                ans = [str(a.get("data"))[:DNS_MAX_ANSWER_LEN] for a in answers[:DNS_MAX_ANSWERS * 4]
                       if isinstance(a, dict) and a.get("data")]
                out.append(f"  {t}: {', '.join(ans[:DNS_MAX_ANSWERS]) if ans else '-'}")
    except (httpx.HTTPError, ValueError) as e:
        _emit(ctx, "recon.dns", host=host, error=type(e).__name__)
        return f"ERROR: DNS lookup for {host}: {type(e).__name__}: {e}"
    _emit(ctx, "recon.dns", host=host)
    return "\n".join(out)


RECON_TOOLS = [
    ("tls_inspect", "inspect one host's TLS certificate: issuer, validity, and all SAN hostnames.",
     '{"host": str, "port"?: int}', t_tls_inspect),
    ("http_fingerprint", "fetch one host's HTTP response headers: server/tech hints and which security headers are missing.",
     '{"host": str, "scheme"?: "https|http"}', t_http_fingerprint),
    ("port_scan", "TCP-connect scan a small port set on ONE host you own/are authorized to scan (needs authorized=true).",
     '{"host": str, "authorized": true, "ports"?: [int]}', t_port_scan),
    ("cert_transparency", "enumerate a domain's subdomains from public certificate-transparency logs (crt.sh).",
     '{"host": str}', t_cert_transparency),
    ("dns_enum", "resolve A/AAAA/MX/NS/TXT/CNAME/SOA records for one host via public DNS-over-HTTPS.",
     '{"host": str, "types"?: [str]}', t_dns_enum),
]


def register_recon_tools(registry) -> None:
    for name, desc, args_doc, fn in RECON_TOOLS:
        registry.register(Tool(name, desc, args_doc, fn, source="builtin"))
