"""Heat-weighted trim policy for Bridge rings and fleet relay caps.

Below 80% occupancy: FIFO (unchanged). Above: evict coldest heat*age first.
Fleet relay: density-greedy packing, score = heat / max(1, bytes).
"""
from __future__ import annotations

import json
from typing import Any

BRIDGE_MAX_POSTS = 50
BRIDGE_MAX_BYTES = 64 * 1024
OCCUPANCY_THRESHOLD = 0.8


def trim_by_heat(posts: dict[str, dict[str, Any]], limit: int) -> list[str]:
    """Return post ids to evict (coldest heat*age first), empty if under limit."""
    if len(posts) <= limit:
        return []
    if len(posts) <= OCCUPANCY_THRESHOLD * limit:
        ids = list(posts)
        return ids[: len(posts) - limit]
    scored = sorted(posts.items(),
                    key=lambda kv: (float(kv[1].get("heat", 0.0))
                                    * float(kv[1].get("ts", 0.0))))
    return [pid for pid, _ in scored[: len(posts) - limit]]


def pack_by_density(posts: list[dict[str, Any]], max_posts: int = BRIDGE_MAX_POSTS,
                    max_bytes: int = BRIDGE_MAX_BYTES) -> list[dict[str, Any]]:
    """Density-greedy: score = heat / max(1, serialized bytes), fill until a cap binds."""
    ranked = sorted(posts,
                    key=lambda p: float(p.get("heat", 0.0))
                    / max(1, len(json.dumps(p, default=str))),
                    reverse=True)
    kept: list[dict[str, Any]] = []
    total = 0
    for p in ranked:
        b = len(json.dumps(p, default=str))
        if len(kept) >= max_posts or total + b > max_bytes:
            break
        kept.append(p)
        total += b
    kept.sort(key=lambda p: float(p.get("ts", 0.0)))
    return kept
