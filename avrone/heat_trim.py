"""Heat-aware trim and proportional fleet relay budget.

Three functions, all pure:

  heat_trim(posts, limit, age_fn)  -- OrderedDict-compatible trim.
      Below 80% of limit: drop oldest (unchanged FIFO behavior).
      Above 80%: drop the post with the lowest heat * age product,
      so a hot new finding displaces a cold old one. This inverts the
      pathology where FIFO evicted root-cause findings first under max
      residual heat.

  proportional_budget(mean_heat, cap_posts=50, cap_bytes=65536)
      -- netsquad relay allocation. Mean heat 6.25 (max) -> full cap.
      Mean heat 2.25 (cone floor) -> ~12 posts / ~19 KB. Linear in
      the normalized heat: t = (mean_heat - 2.25) / 4.0.

  eviction_stats(before, after)  -- mean heat of evicted vs kept,
      for the verification assertion: kept mean > evicted mean.
"""
from __future__ import annotations

from typing import Any, Callable

from .heat_contract import heat_field, mean_heat

TRIM_THRESHOLD = 0.80  # invert FIFO past this fraction of the ring limit


def heat_trim(
    posts: dict[str, dict[str, Any]],
    limit: int,
    *,
    age_fn: Callable[[dict[str, Any], float], float] | None = None,
    now: float | None = None,
    p: float = 0.5,
) -> list[str]:
    """Return post ids to evict. Does not mutate; caller pops.

    age_fn(post, now) -> age in seconds (default: now - post['ts']).
    """
    import time

    if now is None:
        now = time.time()
    if age_fn is None:
        age_fn = lambda post, t: max(0.0, t - float(post.get("ts") or t))  # type: ignore[misc]

    ids = list(posts.keys())
    if len(ids) <= limit:
        return []

    target = limit
    # Phase 1: below 80%, pure FIFO (preserves historical behavior).
    evicted: list[str] = []
    if len(ids) <= int(limit * TRIM_THRESHOLD) + (len(ids) - limit):
        # still in FIFO regime for the overflow portion
        overflow = len(ids) - limit
        return ids[:overflow]

    # Phase 2: past 80% — heat*age inversion.
    overflow = len(ids) - limit
    scored = []
    for pid in ids:
        post = posts[pid]
        h = heat_field(post, p)
        a = age_fn(post, now)
        scored.append((h * a, pid))
    scored.sort()  # lowest heat*age first
    return [pid for _, pid in scored[:overflow]]


def proportional_budget(
    posts: list[dict[str, Any]],
    *,
    cap_posts: int = 50,
    cap_bytes: int = 64 * 1024,
    p: float = 0.5,
) -> tuple[int, int]:
    """Allocate the netsquad relay budget by mean heat.

    Returns (post_budget, byte_budget). Linear interpolation between
    the cone floor (2.25 -> ~24% of cap) and max (6.25 -> 100%).
    """
    mh = mean_heat(posts, p)
    t = max(0.0, min(1.0, (mh - 2.25) / 4.0))
    # floor at 12 posts / ~19KB so a cold node still exports something
    n = max(12, int(round(12 + t * (cap_posts - 12))))
    b = max(12 * 1024, int(round(12 * 1024 + t * (cap_bytes - 12 * 1024))))
    return n, b


def eviction_stats(
    posts: dict[str, dict[str, Any]],
    evicted_ids: list[str],
    p: float = 0.5,
) -> dict[str, float]:
    """Mean heat of evicted vs retained posts."""
    evicted = [posts[i] for i in evicted_ids if i in posts]
    retained = [posts[i] for i in posts if i not in evicted_ids]
    return {
        "evicted_mean_heat": mean_heat(evicted, p),
        "retained_mean_heat": mean_heat(retained, p),
        "n_evicted": len(evicted),
        "n_retained": len(retained),
    }
