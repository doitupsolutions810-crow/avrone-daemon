"""Versioned, deterministic identity contract for completed verification attempts."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import unicodedata
from typing import Any

SCHEMA_VERSION = "avrone.trial/v1"
OUTCOMES = frozenset({"pass", "fail", "skipped", "cancelled", "timed_out", "inconclusive"})
MAX_SYSTEM_CHARS = 64
MAX_NODE_CHARS = 63
MAX_ID_CHARS = 256
MAX_CONFIG_BYTES = 16_384
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 4_096
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED = {
    "schema", "trial_id", "system", "node", "source", "run_id", "check_id",
    "check_version", "check_config_sha256", "attempt_id", "outcome", "failed",
}


class TrialContractError(ValueError):
    """A trial envelope is malformed, non-canonical, or internally inconsistent."""


class TrialNotPoolable(TrialContractError):
    """A valid operational outcome that is not a completed pass/fail observation."""

    def __init__(self, outcome: str):
        super().__init__(f"outcome {outcome!r} is stored operationally but is not poolable")
        self.outcome = outcome


def _identity(value: Any, field: str, limit: int, *, require_canonical: bool = False) -> str:
    if not isinstance(value, str):
        raise TrialContractError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > limit:
        raise TrialContractError(f"{field} must contain 1..{limit} characters")
    if require_canonical and value != normalized:
        raise TrialContractError(f"{field} is not NFC/trim canonical")
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in normalized):
        raise TrialContractError(f"{field} contains a control, format, or surrogate character")
    return normalized


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise TrialContractError("value is not finite canonical JSON") from exc


def _normalize_config(value: Any, depth: int = 0, budget: list[int] | None = None) -> Any:
    if budget is None:
        budget = [0]
    budget[0] += 1
    if budget[0] > MAX_JSON_NODES:
        raise TrialContractError(f"check_config exceeds {MAX_JSON_NODES} JSON nodes")
    if depth > MAX_JSON_DEPTH:
        raise TrialContractError(f"check_config exceeds JSON depth {MAX_JSON_DEPTH}")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise TrialContractError("check_config must not contain NaN or Infinity")
        return value
    if isinstance(value, str):
        if any(unicodedata.category(ch) == "Cs" for ch in value):
            raise TrialContractError("check_config contains a surrogate code point")
        return unicodedata.normalize("NFC", value)
    if type(value) is list:
        return [_normalize_config(item, depth + 1, budget) for item in value]
    if type(value) is dict:
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TrialContractError("check_config object keys must be strings")
            if any(unicodedata.category(ch) == "Cs" for ch in key):
                raise TrialContractError("check_config key contains a surrogate code point")
            canonical_key = unicodedata.normalize("NFC", key)
            if canonical_key in normalized:
                raise TrialContractError("check_config has duplicate keys after NFC normalization")
            normalized[canonical_key] = _normalize_config(item, depth + 1, budget)
        return normalized
    raise TrialContractError(f"check_config contains non-JSON type {type(value).__name__}")


def canonical_config(value: Any | None = None) -> bytes:
    normalized = _normalize_config({} if value is None else value)
    raw = _canonical_json(normalized)
    if len(raw) > MAX_CONFIG_BYTES:
        raise TrialContractError(f"check_config exceeds {MAX_CONFIG_BYTES} bytes")
    return raw


def _identity_payload(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "system": record["system"],
        "node": record["node"],
        "run_id": record["run_id"],
        "check_id": record["check_id"],
        "check_version": record["check_version"],
        "check_config_sha256": record["check_config_sha256"],
        "attempt_id": record["attempt_id"],
    }


def make_trial(
    *,
    system: str,
    node: str,
    run_id: str,
    check_id: str,
    check_version: str,
    attempt_id: str,
    outcome: str,
    check_config: Any | None = None,
) -> dict[str, Any]:
    """Build a bounded trial envelope; only pass/fail outcomes enter evidence_pool."""
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise TrialContractError(f"outcome must be one of {', '.join(sorted(OUTCOMES))}")
    system = _identity(system, "system", MAX_SYSTEM_CHARS)
    node = _identity(node, "node", MAX_NODE_CHARS)
    run_id = _identity(run_id, "run_id", MAX_ID_CHARS)
    check_id = _identity(check_id, "check_id", MAX_ID_CHARS)
    check_version = _identity(check_version, "check_version", MAX_ID_CHARS)
    attempt_id = _identity(attempt_id, "attempt_id", MAX_ID_CHARS)
    source = f"{system}:{node}"
    if len(source) > 128:
        raise TrialContractError("composite source exceeds evidence_pool's 128-character limit")
    config_sha = hashlib.sha256(canonical_config(check_config)).hexdigest()
    identity = {
        "schema": SCHEMA_VERSION, "system": system, "node": node, "run_id": run_id,
        "check_id": check_id, "check_version": check_version,
        "check_config_sha256": config_sha, "attempt_id": attempt_id,
    }
    trial_id = hashlib.sha256(_canonical_json(identity)).hexdigest()
    failed = False if outcome == "pass" else True if outcome == "fail" else None
    return {
        "schema": SCHEMA_VERSION, "trial_id": trial_id, "system": system, "node": node,
        "source": source, "run_id": run_id, "check_id": check_id, "check_version": check_version,
        "check_config_sha256": config_sha, "attempt_id": attempt_id, "outcome": outcome, "failed": failed,
    }


def validate_trial_envelope(record: Any) -> bool:
    """Validate exact fields, canonical identifiers, outcome semantics, and deterministic ID."""
    if not isinstance(record, dict) or set(record) != _REQUIRED:
        return False
    try:
        if record["schema"] != SCHEMA_VERSION or record["outcome"] not in OUTCOMES:
            return False
        expected_failed = False if record["outcome"] == "pass" else True if record["outcome"] == "fail" else None
        if record["failed"] is not expected_failed:
            return False
        fields = (
            ("system", MAX_SYSTEM_CHARS), ("node", MAX_NODE_CHARS),
            ("run_id", MAX_ID_CHARS), ("check_id", MAX_ID_CHARS),
            ("check_version", MAX_ID_CHARS), ("attempt_id", MAX_ID_CHARS),
        )
        for field, limit in fields:
            _identity(record[field], field, limit, require_canonical=True)
        source = record["source"]
        if not isinstance(source, str) or len(source) > 128:
            return False
        if source != record["system"] + ":" + record["node"]:
            return False
        config_sha, trial_id = record["check_config_sha256"], record["trial_id"]
        if not isinstance(config_sha, str) or not _HEX64.fullmatch(config_sha):
            return False
        if not isinstance(trial_id, str) or not _HEX64.fullmatch(trial_id):
            return False
        expected = hashlib.sha256(_canonical_json(_identity_payload(record))).hexdigest()
        return hmac.compare_digest(expected, trial_id)
    except (KeyError, TypeError, ValueError, RecursionError, UnicodeEncodeError):
        return False


def canonical_trial(record: dict[str, Any]) -> bytes:
    if not validate_trial_envelope(record):
        raise TrialContractError("trial envelope rejected")
    raw = _canonical_json(record)
    if len(raw) > 4_096:
        raise TrialContractError("trial envelope exceeds 4096 bytes")
    return raw


def project_pool_trial(record: dict[str, Any]) -> dict[str, Any]:
    if not validate_trial_envelope(record):
        raise TrialContractError("trial envelope rejected")
    if record["outcome"] not in {"pass", "fail"}:
        raise TrialNotPoolable(record["outcome"])
    return {"source": record["source"], "trial_id": record["trial_id"], "failed": record["failed"]}
