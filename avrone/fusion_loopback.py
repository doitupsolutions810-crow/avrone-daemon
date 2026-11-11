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


def host_text(host: str | None) -> str:
    name = str(host or "").strip().strip("[]").lower()
    if "%" in name:
        name = name.split("%", 1)[0]
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
    if name == "localhost" or name.endswith(".localhost"):
        return True
    ip = unwrap_ip(name)
    return bool(ip is not None and ip.is_loopback)


def origin_key(base: str) -> str:
    """One breaker bucket for every loopback spelling of the same port.

    ``http://127.0.0.1:8090`` and ``http://[::1]:8090`` are the same peer when
    the access policy would treat both hosts as loopback. Other hosts keep
    their own key, so 10.0.0.1 is not folded into loopback.
    """
    try:
        parts = urlsplit(base or "")
        # urlsplit defers malformed or out-of-range port errors until .port
        # is accessed; guard that access just like parsing the URL itself.
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "invalid:"
    if is_access_loopback(host):
        return f"loopback:{port}"
    return f"{host_text(host)}:{port}"
