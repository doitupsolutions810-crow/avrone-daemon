"""Portable, deterministic evidence envelope for AVRONE ↔ Great Scale integrations.

This module transports *measured test outcomes*; it does not grant capabilities,
attest human identity, or claim that a model-derived result is independently verified.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from typing import Any

from .greatscale import evidence_snapshot

SCHEMA = "avrone.greatscale.evidence/v1"
MAX_RUN_ID = 128
MAX_SOURCE = 80
MAX_REFS = 32
MAX_REF = 160
MAX_PROVENANCE_DEPTH = 4096


class EvidenceContractError(ValueError):
    """An evidence envelope is incomplete, malformed, or inconsistent."""


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise EvidenceContractError(f"{field} must be a non-empty string of at most {limit} chars")
    if value != value.strip():
        raise EvidenceContractError(f"{field} must not have leading or trailing whitespace")
    # Machine identifiers must not contain C0/C1 controls, bidi controls, zero-width
    # format characters, or surrogate code points. These can spoof logs and identities.
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in value):
        raise EvidenceContractError(f"{field} contains a control or formatting character")
    return value


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _refs(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > MAX_REFS:
        raise EvidenceContractError(f"refs must be a list of at most {MAX_REFS} strings")
    out: list[str] = []
    for item in value:
        ref = _text(item, "ref", MAX_REF)
        if ref not in out:
            out.append(ref)
    return out


def build_evidence_event(*, source: str, run_id: str, failures: int, runs: int,
                         refs: Any = None, capability_id: str | None = None,
                         human_root: str | None = None, provenance_depth: int = 0,
                         check_id: str = "default") -> dict[str, Any]:
    """Create a content-addressed event using observed integer outcomes only."""
    source = _text(source, "source", MAX_SOURCE)
    run_id = _text(run_id, "run_id", MAX_RUN_ID)
    check_id = _text(check_id, "check_id", MAX_REF)
    if (isinstance(provenance_depth, bool) or not isinstance(provenance_depth, int)
            or not 0 <= provenance_depth <= MAX_PROVENANCE_DEPTH):
        raise EvidenceContractError(f"provenance_depth must be an integer in [0, {MAX_PROVENANCE_DEPTH}]")
    if capability_id is not None:
        capability_id = _text(capability_id, "capability_id", MAX_REF)
    if human_root is not None:
        human_root = _text(human_root, "human_root", MAX_REF)
    if provenance_depth > 0 and human_root is None:
        raise EvidenceContractError("positive provenance_depth requires an explicit human_root")
    if isinstance(failures, bool) or not isinstance(failures, int):
        raise EvidenceContractError("failures must be an integer")
    if isinstance(runs, bool) or not isinstance(runs, int):
        raise EvidenceContractError("runs must be an integer")
    try:
        snapshot = evidence_snapshot(failures, runs)
    except (ValueError, TypeError, OverflowError) as exc:
        raise EvidenceContractError(str(exc)) from exc
    body = {
        "schema": SCHEMA,
        "source": source,
        "kind": "greatscale_evidence",
        "run_id": run_id,
        "check_id": check_id,
        "evidence": snapshot,
        "refs": _refs(refs),
        "capability_id": capability_id,
        "human_root": human_root,
        "provenance_depth": provenance_depth,
    }
    digest = hashlib.sha256(_canonical(body)).hexdigest()
    return {**body, "digest": digest}


def verify_evidence_event(event: Any) -> tuple[bool, str]:
    """Validate structure, recompute Great Scale values, and verify the content digest."""
    if not isinstance(event, dict):
        return False, "event must be an object"
    required = {"schema", "source", "kind", "run_id", "check_id", "evidence",
                "refs", "capability_id", "human_root", "provenance_depth", "digest"}
    if set(event) != required:
        return False, "event fields do not match schema v1"
    try:
        if event["schema"] != SCHEMA or event["kind"] != "greatscale_evidence":
            raise EvidenceContractError("unsupported schema or kind")
        _text(event["source"], "source", MAX_SOURCE)
        _text(event["run_id"], "run_id", MAX_RUN_ID)
        _text(event["check_id"], "check_id", MAX_REF)
        refs = _refs(event["refs"])
        if refs != event["refs"]:
            raise EvidenceContractError("refs are not canonical")
        depth = event["provenance_depth"]
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= MAX_PROVENANCE_DEPTH:
            raise EvidenceContractError("invalid provenance_depth")
        if depth > 0 and not event["human_root"]:
            raise EvidenceContractError("positive provenance_depth requires human_root")
        if event["capability_id"] is not None:
            _text(event["capability_id"], "capability_id", MAX_REF)
        if event["human_root"] is not None:
            _text(event["human_root"], "human_root", MAX_REF)
        evidence = event["evidence"]
        if not isinstance(evidence, dict):
            raise EvidenceContractError("evidence must be an object")
        expected = evidence_snapshot(evidence.get("failures"), evidence.get("runs"))
        if _canonical(expected) != _canonical(evidence):
            raise EvidenceContractError("Great Scale evidence does not match measured counts")
        body = {key: event[key] for key in required if key != "digest"}
        actual = hashlib.sha256(_canonical(body)).hexdigest()
        supplied = event["digest"]
        if not isinstance(supplied, str) or re.fullmatch(r"[0-9a-f]{64}", supplied) is None:
            raise EvidenceContractError("digest must be 64 lowercase hexadecimal characters")
        if not hmac.compare_digest(actual, supplied):
            raise EvidenceContractError("digest mismatch")
    except (EvidenceContractError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        return False, str(exc)
    return True, ""
