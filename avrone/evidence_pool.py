"""Bounded, provenance-aware pooling for Great Scale verification trials.

This is an additive contract for future bridge/fleet consumers. It deliberately
accepts individual, globally identified trials rather than pre-aggregated
counts: duplicate relays must not manufacture confirmations. It does not claim
that distinct IDs make trials statistically independent.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .greatscale import MAX_RUNS, evidence_snapshot

MAX_POOL_TRIALS = 100_000
MAX_TRIAL_ID_CHARS = 256
MAX_SOURCE_CHARS = 128
MAX_SOURCES = 10_000


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

    for record in trials:
        if len(seen) >= MAX_POOL_TRIALS:
            raise ValueError(f"trial pool exceeds hard cap ({MAX_POOL_TRIALS})")
        if not isinstance(record, Mapping):
            raise ValueError("each trial must be a mapping")

        source = record.get("source")
        trial_id = record.get("trial_id")
        failed = record.get("failed")
        if not isinstance(source, str) or not source.strip() or len(source) > MAX_SOURCE_CHARS:
            raise ValueError("source must be a non-empty string of at most 128 characters")
        if not isinstance(trial_id, str) or not trial_id.strip() or len(trial_id) > MAX_TRIAL_ID_CHARS:
            raise ValueError("trial_id must be a non-empty string of at most 256 characters")
        if not isinstance(failed, bool):
            raise ValueError("failed must be a boolean")

        source = source.strip()
        trial_id = trial_id.strip()
        if trial_id in seen:
            raise ValueError(f"duplicate trial_id: {trial_id}")
        if source not in by_source and len(by_source) >= MAX_SOURCES:
            raise ValueError(f"trial pool exceeds hard cap ({MAX_SOURCES} sources)")
        seen.add(trial_id)
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


def event_to_pool_trial(event: Mapping[str, Any]) -> dict[str, Any]:
    """Project exactly one verified execution into the individual-trial pool.

    Aggregate events (runs > 1) are intentionally rejected: expanding an
    aggregate count into synthetic trial IDs would manufacture independence.
    The identity key excludes the outcome/digest so a conflicting re-report
    of the same source/run/check collides instead of becoming a second trial.
    This is an evidence projection only; it grants no capability or authority.
    """
    import hashlib
    import json

    from .integration_contract import verify_evidence_event

    if not isinstance(event, Mapping):
        raise ValueError("evidence event must be a mapping")
    valid, reason = verify_evidence_event(dict(event))
    if not valid:
        raise ValueError(f"invalid evidence event: {reason}")
    evidence = event["evidence"]
    if evidence["runs"] != 1:
        raise ValueError("aggregate evidence cannot be projected as an individual trial")
    identity = json.dumps(
        [event["source"], event["run_id"], event["check_id"]],
        ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")
    trial_id = hashlib.sha256(identity).hexdigest()
    return {
        "source": event["source"],
        "trial_id": trial_id,
        "failed": evidence["failures"] == 1,
    }
