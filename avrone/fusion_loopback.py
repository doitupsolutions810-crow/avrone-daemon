"""Loopback spelling shared by access control, the doctor, and fusion delivery.

``ipaddress`` reports ``::ffff:127.0.0.1`` as not a loopback address. Its
``ipv4_mapped`` form is ``127.0.0.1``, which is. A dual-stack listener shows an
IPv4 loopback client as that mapped string, so a three-name set
(``127.0.0.1``, ``::1``, ``localhost``) treats the same machine as remote.

The HTTP access policy stays the single address 127.0.0.1 (plus ``::1`` and
localhost). It does not grow to the rest of 127.0.0.0/8. Proxy bypass and the
doctor already treat that whole block as loopback; this module does not change
that wider check except to unwrap the mapped form first.
"""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789.:-_")
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def host_text(host: str | None) -> str:
    """Normalise a host for comparison; "" for anything that is not a plain host spelling.

    Round 48: ``.strip("[]")`` removed any number of brackets from either end, so
    ``]127.0.0.1[`` and ``[[::1]]`` counted as loopback; ``%`` cut a zone from any name, so
    ``127.0.0.1%evil`` and ``localhost%x`` did too; and control characters, NUL and
    non-ASCII passed through to the comparison. Now only one matched ``[...]`` pair is
    removed, a zone is allowed only on an IPv6 literal, and any other character refuses."""
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii")
        except UnicodeDecodeError:
            return ""
    if not isinstance(host, str):
        return ""
    name = host.strip(" \t").lower()
    if name.startswith("[") and name.endswith("]") and len(name) >= 2:
        name = name[1:-1]
    if "%" in name:
        addr, _, zone = name.partition("%")
        if ":" not in addr or not zone or "%" in zone or not all(c in _HOST_CHARS for c in zone):
            return ""
        name = addr
    if not name or not all(c in _HOST_CHARS for c in name):
        return ""
    return name


def unwrap_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def is_access_loopback(host: str | None) -> bool:
    """True for the access policy's loopback: 127.0.0.1, ::1, localhost, and the
    IPv4-mapped spelling of those two addresses. 127.0.0.2 stays remote."""
    name = host_text(host)
    if not name:
        return False
    if name in {"127.0.0.1", "::1", "localhost"}:
        return True
    ip = unwrap_ip(name)
    if ip is None:
        return False
    return str(ip) in {"127.0.0.1", "::1"}


def is_any_loopback(host: str | None) -> bool:
    """True for localhost, any 127.0.0.0/8 address, ::1, and their mapped forms."""
    name = host_text(host)
    if name.endswith(".") and not name.endswith(".."):
        name = name[:-1]  # one trailing root dot: "localhost." is localhost
    if name == "localhost" or (name.endswith(".localhost") and ".." not in name
                               and not name.startswith(".")):
        return True
    ip = unwrap_ip(name)
    return bool(ip is not None and ip.is_loopback)


def origin_key(base: str) -> str:
    """One breaker bucket for every loopback spelling of the same port.

    ``http://127.0.0.1:8090`` and ``http://[::1]:8090`` are the same peer when
    the access policy would treat both hosts as loopback. Other hosts keep
    their own key, so 10.0.0.1 is not folded into loopback.
    """
    # round 48: ``parts.port`` raised ValueError outside the try (":99999", ":abc"), and the
    # default port was None, so http://localhost and https://localhost shared one bucket while
    # http://localhost and http://localhost:80 did not
    if not isinstance(base, str):
        return "invalid:"
    try:
        parts = urlsplit(base)
        port = parts.port
    except ValueError:
        return "invalid:"
    host = parts.hostname or ""
    if port is None:
        port = _DEFAULT_PORTS.get(parts.scheme.lower())
    if is_access_loopback(host):
        return f"loopback:{port}"
    name = host_text(host) or host
    if ":" in name:
        name = f"[{name}]"  # "fe80::1:8090" read as host fe80::1 port 8090 or host fe80::1:8090
    return f"{name}:{port}"
