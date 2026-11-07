"""Lesson evidence accumulator — Great Scale companion for distill_lessons.

Additive helper used by bridge.distill_lessons (Descent XV). Re-derived identical
lessons reinforce confirmations and attach evidence_S / evidence_psi so heat can
descend in memory. Does not replace bridge; callers pass store + picks.

Mathematics: pure confirmation path uses evidence_snapshot(0, confirmations);
S = heat(0) = 0 on the raw channel, Jeffreys p_J = 0.5/(N+1) → 0 as N grows.
"""
from __future__ import annotations

from typing import Any

LESSONS_NS = "lessons"


def reinforce_or_create(
    store: Any,
    *,
    key: str,
    text: str,
    have: dict[str, Any],
    run_id: str,
    task: str,
) -> str | None:
    """Return key if newly created, None if reinforced or skipped.

    Mutates `have` in place. Raises nothing for learning failures of evidence math.
    """
    if key in have:
        prev = have[key]
        try:
            conf = int(prev.get("confirmations", 1))
        except (TypeError, ValueError):
            conf = 1
        conf = max(1, conf) + 1
        meta: dict[str, Any] = {
            "run_id": run_id,
            "task": str(task)[:200],
            "confirmations": conf,
            "reinforced": True,
        }
        try:
            from .greatscale import evidence_snapshot
            snap = evidence_snapshot(0, conf)
            meta.update({
                "evidence_p": snap["p"],
                "evidence_S": snap["S"],
                "evidence_psi": snap["psi"],
                "evidence_orientation": snap["orientation"],
                "evidence_runs": snap["runs"],
                "evidence_failures": snap["failures"],
            })
        except Exception:  # noqa: BLE001
            pass
        store.save(LESSONS_NS, key, text, **meta)
        have[key] = {**prev, **meta, "value": text, "key": key, "ns": LESSONS_NS}
        return None
    store.save(
        LESSONS_NS, key, text,
        run_id=run_id, task=str(task)[:200],
        confirmations=1, reinforced=False,
    )
    have[key] = {"key": key, "value": text, "confirmations": 1}
    return key


def rank_score(rec: dict[str, Any], keyword_overlap: int) -> tuple:
    """Sort key: higher overlap, higher confirmations, colder heat, newer ts."""
    try:
        conf = int(rec.get("confirmations", 1))
    except (TypeError, ValueError):
        conf = 1
    conf = max(1, conf)
    try:
        heat_s = float(rec["evidence_S"]) if rec.get("evidence_S") is not None else 0.25
    except (TypeError, ValueError):
        heat_s = 0.25
    return (-keyword_overlap, -conf, heat_s, -float(rec.get("ts", 0)))
