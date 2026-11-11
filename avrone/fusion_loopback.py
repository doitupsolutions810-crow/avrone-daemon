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
import re
from urllib.parse import urlsplit


def host_text(host: str | None) -> str:
    name = str(host or "").strip().lower()
    if "[" in name or "]" in name:
        # Only one balanced IPv6 literal wrapper is legitimate. strip("[]")
        # silently promoted malformed peer strings to trusted loopback hosts.
        if not (
            name.startswith("[")
            and name.endswith("]")
            and name.count("[") == 1
            and name.count("]") == 1
            and ":" in name[1:-1]
        ):
            return ""
        name = name[1:-1]
    if "%" in name:
        # Zone IDs are meaningful only on IPv6 literals. Never turn
        # 'localhost%untrusted' or '127.0.0.1%untrusted' into a trusted host.
        # Check the full suffix instead of silently truncating repeated %s.
        if name.count("%") != 1:
            return ""
        address, zone = name.split("%", 1)
        if not re.fullmatch(r"[a-z0-9_.-]{1,32}", zone):
            return ""
        try:
            if ipaddress.ip_address(address).version != 6:
                return ""
        except ValueError:
            return ""
        name = address
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
    canonical = host_text(host)
    if parts.scheme not in {"http", "https"} or not canonical or not parts.netloc:
        return "invalid:"
    # HTTP and HTTPS on one host:port do not share a circuit breaker.
    # Retain existing HTTP keys for backward compatibility.
    prefix = "https-" if parts.scheme == "https" else ""
    if is_access_loopback(host):
        return f"{prefix}loopback:{port}"
    return f"{prefix}{canonical}:{port}"
