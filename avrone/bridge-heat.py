"""Thin heat-aware wrapper over avrone.bridge.

Stamps rec["heat"] in post and merge_remote, swaps _trim for the
80%-threshold inverted trim from heat_trim. Keeps the pushed blob small
enough to survive the connector cap. The real bridge module stays
upstream; this wrapper is the hot patch.
"""
from __future__ import annotations

from typing import Any

from avrone.heat_contract import heat, kind_weight, post_heat, soft_contract
from avrone.heat_trim import TRIM_THRESHOLD, trim_by_heat

try:
    from avrone import bridge as _bridge
except ImportError:
    _bridge = None


def _stamp_heat(rec: dict[str, Any]) -> dict[str, Any]:
    """Attach heat to a post record if absent."""
    if "heat" not in rec:
        kind = rec.get("kind", "finding")
        text = rec.get("text") or rec.get("content") or ""
        rec = dict(rec)
        rec["heat"] = post_heat(kind, text)
    return rec


if _bridge is not None and hasattr(_bridge, "post"):
    _orig_post = _bridge.post

    def post(self, rec: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        rec = _stamp_heat(rec)
        return _orig_post(self, rec, *args, **kwargs)

    post.__name__ = "post"
    post.__doc__ = "Heat-stamping wrapper around bridge.post."
else:
    def post(self, rec: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        return _stamp_heat(rec)


if _bridge is not None and hasattr(_bridge, "merge_remote"):
    _orig_merge = _bridge.merge_remote

    def merge_remote(self, remote_posts: list[dict[str, Any]], *args: Any, **kwargs: Any) -> Any:
        stamped = [_stamp_heat(r) for r in remote_posts]
        return _orig_merge(self, stamped, *args, **kwargs)

    merge_remote.__name__ = "merge_remote"
    merge_remote.__doc__ = "Heat-stamping wrapper around bridge.merge_remote."
else:
    def merge_remote(self, remote_posts: list[dict[str, Any]], *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return [_stamp_heat(r) for r in remote_posts]


if _bridge is not None and hasattr(_bridge, "_trim"):
    def _trim(self) -> None:
        """Inverted trim: FIFO below 80% occupancy, coldest-first above."""
        posts = self._posts
        limit = getattr(self, "limit", 500)
        if len(posts) <= TRIM_THRESHOLD * limit:
            while len(posts) > limit:
                pid, _ = posts.popitem(last=False)
                if hasattr(self, "_tomb"):
                    self._tomb(pid)
        else:
            trim_by_heat(posts, limit)

    _trim.__name__ = "_trim"
    _trim.__doc__ = "Heat-weighted inverted trim for the bridge ring."
else:
    def _trim(self) -> None:
        return None


def __getattr__(name: str):
    if _bridge is not None and hasattr(_bridge, name):
        return getattr(_bridge, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
