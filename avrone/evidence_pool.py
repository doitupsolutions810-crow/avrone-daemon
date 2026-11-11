"""Bounded, provenance-aware pooling for Great Scale verification trials.

This is an additive contract for future bridge/fleet consumers. It deliberately
accepts individual, globally identified trials rather than pre-aggregated
counts: duplicate relays must not manufacture confirmations. It does not claim
that distinct IDs make trials statistically independent.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .greatscale import MAX_RUNS, evidence_snapshot
from .textsafe import UNSAFE_ONE

MAX_POOL_TRIALS = 100_000
MAX_TRIAL_ID_CHARS = 256
MAX_SOURCE_CHARS = 128
MAX_SOURCES = 10_000

# Hardening round 43: identity fields are compared in one canonical form. Before, a relay that
# re-encoded "trial-\u00e9" as NFD "trial-e\u0301", added a zero-width space or a BOM, or used a
# fullwidth digit produced a "distinct" trial_id, so one outcome counted twice: exactly the
# manufactured confirmation the module exists to stop. "node-a" and "Node-A\u200b" also showed up
# as two silos, which could fake an orientation conflict.
# Preserve the integration branch's strict provenance boundary: controls, bidi markers and
# lone surrogates are refused before normalization. Other invisible format characters are
# removed so a re-encoded relay cannot manufacture an additional confirmation.
_REFUSED_CATEGORIES = frozenset({"Cc", "Zl", "Zp", "Cs"})
_WS = re.compile(r"\s+")


def _canon(value: str) -> str:
    """Reject unsafe provenance, then NFKC and deduplicate safe invisible/space variants.

    Controls, bidi markers and lone surrogates never become accepted identity text.
    Other Unicode format characters are removed; harmless spaces collapse and trim.
    """
    if UNSAFE_ONE.search(value) or any(unicodedata.category(ch) in _REFUSED_CATEGORIES for ch in value):
        raise ValueError("identity fields must not contain control or bidi characters or lone surrogates")
    v = unicodedata.normalize("NFKC", value)
    v = "".join(ch for ch in v if unicodedata.category(ch) != "Cf")
    return _WS.sub(" ", v).strip()


def _shown(value: str, limit: int = 64) -> str:
    """An identifier as an error message may quote it: escaped, and cut to `limit` characters."""
    r = value if len(value) <= limit else value[:limit] + "..."
    return r.encode("unicode_escape").decode("ascii")


def pool_trials(trials: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Pool unique trial records and expose source-level orientation conflicts.

    Each record must have a non-empty ``source``, a globally unique
    ``trial_id``, and a boolean ``failed``. Duplicate IDs are rejected, not
    silently counted or ignored, so callers must resolve relay overlap before
    retrying. Empty input is valid and produces a zero-trial report without a
    Great Scale snapshot (``evidence_snapshot`` intentionally requires runs).

    The report's p/S/Psi describe the observed pooled outcomes only. Distinct
    identifiers are not proof of statistical independence or causal equivalence.
    """
    seen: set[str] = set()
    by_source: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    failures = 0
    source_names: dict[str, str] = {}

    if isinstance(trials, (str, bytes, bytearray, Mapping)) or trials is None:
        # a lone record or a string iterated as keys/characters used to fail with a confusing message
        raise ValueError("trials must be an iterable of trial mappings")
    for record in trials:
        if len(seen) >= MAX_POOL_TRIALS:
            raise ValueError(f"trial pool exceeds hard cap ({MAX_POOL_TRIALS})")
        if not isinstance(record, Mapping):
            raise ValueError("each trial must be a mapping")

        try:
            source = record.get("source")
            trial_id = record.get("trial_id")
            failed = record.get("failed")
        except Exception as e:  # noqa: BLE001  a hostile Mapping: refuse it like any malformed record
            raise ValueError(f"trial record could not be read ({type(e).__name__})") from None
        if not isinstance(failed, bool):
            raise ValueError("failed must be a boolean")
        if not isinstance(source, str) or len(source) > MAX_SOURCE_CHARS * 4:
            raise ValueError("source must be a non-empty string of at most 128 characters")
        if not isinstance(trial_id, str) or len(trial_id) > MAX_TRIAL_ID_CHARS * 4:
            raise ValueError("trial_id must be a non-empty string of at most 256 characters")
        source = _canon(source)
        trial_id = _canon(trial_id)
        # the limits apply to the canonical form, so padding no longer rejects a valid id and
        # NFKC expansion (one ligature -> several letters) cannot sneak past them
        if not source or len(source) > MAX_SOURCE_CHARS:
            raise ValueError("source must be a non-empty string of at most 128 characters")
        if not trial_id or len(trial_id) > MAX_TRIAL_ID_CHARS:
            raise ValueError("trial_id must be a non-empty string of at most 256 characters")
        source_key = source.casefold()  # host and node names are case-insensitive
        if source_key in source_names:
            source = source_names[source_key]  # report the first spelling seen
        if trial_id in seen:
            raise ValueError(f"duplicate trial_id: {_shown(trial_id)}")
        if source not in by_source and len(by_source) >= MAX_SOURCES:
            raise ValueError(f"trial pool exceeds hard cap ({MAX_SOURCES} sources)")
        seen.add(trial_id)
        source_names.setdefault(source_key, source)
        by_source[source][1] += 1
        if failed:
            failures += 1
            by_source[source][0] += 1

    runs = len(seen)
    if runs > MAX_RUNS:  # defensive if the Great Scale ceiling changes independently
        raise ValueError(f"trial pool exceeds Great Scale limit ({MAX_RUNS})")

    rows = []
    orientations = set()
    for source in sorted(by_source):
        source_failures, source_runs = by_source[source]
        orientation = (
            "pass" if source_failures == 0 else
            "fail" if source_failures == source_runs else
            "mixed"
        )
        orientations.add(orientation)
        rows.append({"source": source, "failures": source_failures,
                     "runs": source_runs, "orientation": orientation})

    result: dict[str, Any] = {
        "failures": failures,
        "runs": runs,
        "sources": rows,
        "independence": "not_assessed",
        "orientation_conflict": "pass" in orientations and "fail" in orientations,
    }
    if runs:
        result["evidence"] = evidence_snapshot(failures, runs)
    else:
        result["evidence"] = None
    return result
