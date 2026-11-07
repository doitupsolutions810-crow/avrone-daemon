"""Bounded, provenance-aware pooling for Great Scale verification trials.

Additive contract for bridge/fleet consumers. Accepts individual, globally
identified trials rather than pre-aggregated counts: duplicate relays must not
manufacture confirmations. Distinct IDs are not proof of statistical
independence — that wall is named explicitly in the companion channel.

Great Scale companion discipline (Psi = H - sigma form, dual channels):
  - evidence: raw pooled Bernoulli snapshot (sigma / observable)
  - evidence_jeffreys: finite-N honest channel (H-side smoothing)
  - ambiguity: independence / shared-cause wall (named non-identifiability)
  - distances: to promote / demote / conflict thresholds
Never promote on the pooled scalar alone.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from .greatscale import CONE, MAX_RUNS, evidence_snapshot, heat, psi

MAX_POOL_TRIALS = 100_000
MAX_TRIAL_ID_CHARS = 256
MAX_SOURCE_CHARS = 128
MAX_SOURCES = 10_000

# Decision walls (success-criteria thresholds)
PROMOTE_PSI_CEILING = CONE + 0.02          # near floor after confirmations
DEMOTE_PSI_FLOOR = CONE + 0.20             # residual heat demands attention
CONFLICT_HEAT_ALARM = 0.10                 # pooled S above this with orientation conflict


def _jeffreys_snapshot(failures: int, runs: int) -> dict[str, Any]:
    """Jeffreys-smoothed channel: p_J = (f + 1/2)/(r + 1). Keeps small samples honest."""
    if runs < 1:
        return {
            "failures": failures, "runs": runs, "p_J": None, "S_J": None,
            "psi_J": None, "orientation": "empty",
        }
    p_j = (failures + 0.5) / (runs + 1.0)
    s_j = heat(p_j)
    return {
        "failures": failures,
        "runs": runs,
        "p_J": p_j,
        "S_J": s_j,
        "psi_J": psi(s_j),
        "orientation": (
            "pass" if failures == 0 else
            "fail" if failures == runs else
            "mixed"
        ),
        "cone": CONE,
        "onConeFloor": s_j < 1e-12,
    }


def pool_trials(trials: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Pool unique trial records and emit dual-channel Great Scale report.

    Each record must have non-empty ``source``, globally unique ``trial_id``,
    and boolean ``failed``. Duplicate IDs raise. Empty input yields a valid
    zero-trial report with evidence=None and ambiguity named.

    Companion contract:
      evidence          — raw (sigma) channel from observed f/r
      evidence_jeffreys — H-side finite-N smoothing
      ambiguity         — independence wall + shared-cause candidates
      distances         — to promote / demote / conflict thresholds
      orientation_conflict — True when any source is pure-pass and another pure-fail
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
    if runs > MAX_RUNS:
        raise ValueError(f"trial pool exceeds Great Scale limit ({MAX_RUNS})")

    rows = []
    orientations: set[str] = set()
    pure_pass: list[str] = []
    pure_fail: list[str] = []
    for source in sorted(by_source):
        source_failures, source_runs = by_source[source]
        orientation = (
            "pass" if source_failures == 0 else
            "fail" if source_failures == source_runs else
            "mixed"
        )
        orientations.add(orientation)
        if orientation == "pass":
            pure_pass.append(source)
        elif orientation == "fail":
            pure_fail.append(source)
        rows.append({
            "source": source,
            "failures": source_failures,
            "runs": source_runs,
            "orientation": orientation,
        })

    conflict = bool(pure_pass and pure_fail)
    evidence = evidence_snapshot(failures, runs) if runs else None
    evidence_j = _jeffreys_snapshot(failures, runs)

    # Ambiguity set: the independence wall (Descent XIII)
    ambiguity: dict[str, Any] = {
        "independence": "not_assessed",
        "wall": (
            "Distinct trial_ids do not prove statistical independence. "
            "Shared latent causes (same CI image, same billing epoch, same host "
            "clock, same network partition) can induce positive dependence; "
            "pooling under independence then understates residual heat and "
            "manufactures a false descent of Psi toward CONE."
        ),
        "shared_cause_candidates": [],
        "action": "name the wall; do not promote pooled scalar alone",
    }
    if conflict:
        ambiguity["shared_cause_candidates"] = [
            f"orientation conflict between pure-pass sources {pure_pass} "
            f"and pure-fail sources {pure_fail}: hidden context variable exists "
            "with certainty under the Descent-III alarm rule"
        ]
        ambiguity["independence"] = "contradicted_by_orientation_conflict"

    # Distances to decision thresholds (companion channel contract)
    distances: dict[str, Any] = {
        "promote_psi_ceiling": PROMOTE_PSI_CEILING,
        "demote_psi_floor": DEMOTE_PSI_FLOOR,
        "conflict_heat_alarm": CONFLICT_HEAT_ALARM,
    }
    if evidence is not None:
        s = evidence["S"]
        p_psi = evidence["psi"]
        distances["psi_to_promote"] = p_psi - PROMOTE_PSI_CEILING
        distances["psi_to_demote"] = DEMOTE_PSI_FLOOR - p_psi
        distances["S_to_conflict_alarm"] = CONFLICT_HEAT_ALARM - s
        distances["near_floor"] = p_psi <= PROMOTE_PSI_CEILING
        distances["demote_zone"] = p_psi >= DEMOTE_PSI_FLOOR
        distances["conflict_alarm"] = conflict and s >= CONFLICT_HEAT_ALARM
    else:
        distances["psi_to_promote"] = None
        distances["psi_to_demote"] = None
        distances["S_to_conflict_alarm"] = None
        distances["near_floor"] = False
        distances["demote_zone"] = False
        distances["conflict_alarm"] = False

    result: dict[str, Any] = {
        "failures": failures,
        "runs": runs,
        "sources": rows,
        "independence": ambiguity["independence"],
        "orientation_conflict": conflict,
        "evidence": evidence,                 # sigma channel
        "evidence_jeffreys": evidence_j,      # H-side channel
        "ambiguity": ambiguity,               # named wall / non-identifiability
        "distances": distances,               # threshold distances
        # Psi form reminder: never act on one channel alone
        "psi_form": "Psi = CONE + S ; companion requires both channels + wall",
    }
    return result
