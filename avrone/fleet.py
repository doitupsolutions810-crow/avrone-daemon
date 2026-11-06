"""Thin heat-aware wrapper over avrone.fleet.

Overrides cap_bridge_posts with density-greedy packing from heat_trim.
Keeps the pushed blob small enough to survive the connector cap.
The real fleet module stays upstream; this wrapper is the hot patch.
"""
from __future__ import annotations

from typing import Any

from avrone.heat_trim import BRIDGE_MAX_POSTS, BRIDGE_MAX_BYTES, pack_by_density

try:
    from avrone import fleet as _fleet
except ImportError:
    _fleet = None


def cap_bridge_posts(posts: list[dict[str, Any]],
                     max_posts: int = BRIDGE_MAX_POSTS,
                     max_bytes: int = BRIDGE_MAX_BYTES) -> list[dict[str, Any]]:
    """Density-greedy relay cap: score = heat / max(1, serialized bytes)."""
    return pack_by_density(posts, max_posts=max_posts, max_bytes=max_bytes)


def __getattr__(name: str):
    if _fleet is not None and hasattr(_fleet, name):
        return getattr(_fleet, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
