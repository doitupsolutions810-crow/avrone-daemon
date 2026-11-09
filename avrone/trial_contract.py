"""Versioned, deterministic identity for individual verification trials.

Outcome is deliberately excluded from trial_id: a retry that changes pass/fail
must collide with the first receipt and be quarantined by the receiver.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any

SCHEMA_VERSION = "avrone.trial/v1"
OUTCOMES = frozenset({"passed", "failed", "skipped", "cancelled", "timed_out", "inconclusive"})
POOLED_OUTCOMES = frozenset({"passed", "failed"})
MAX_COMPONENT_CHARS = 63
MAX_ID_CHARS = 256
MAX_SOURCE_CHARS = 127
MAX_CONFIG_BYTES = 16 * 1024
MAX_CONFIG_DEPTH = 32
MAX_CONFIG_NODES = 4096
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _identity(value: Any, field: str, max_chars: int = MAX_ID_CHARS,
              forbid_colon: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized or len(normalized) > max_chars:
        raise ValueError(f"{field} must contain 1..{max_chars} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 or 0xD800 <= ord(ch) <= 0xDFFF for ch in normalized):
        raise ValueError(f"{field} contains a control or invalid Unicode character")
    if forbid_colon and ":" in normalized:
        raise ValueError(f"{field} must not contain ':'")
    return normalized


def _normalize_config(value: Any, depth: int, budget: list[int]) -> Any:
    if depth > MAX_CONFIG_DEPTH:
        raise ValueError("check_config exceeds maximum JSON depth")
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("check_config exceeds maximum JSON node count")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = unicodedata.normalize("NFC", value)
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in normalized):
            raise ValueError("check_config contains invalid Unicode")
        return normalized
    if isinstance(value, int):
        if value.bit_length() > 4096:
            raise ValueError("check_config integer is too large")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("check_config numbers must be finite")
        return value
    if isinstance(value, list):
        return [_normalize_config(item, depth + 1, budget) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("check_config object keys must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            if any(0xD800 <= ord(ch) <= 0xDFFF for ch in normalized_key):
                raise ValueError("check_config contains invalid Unicode key")
            if normalized_key in out:
                raise ValueError("check_config has keys that collide after Unicode normalization")
            out[normalized_key] = _normalize_config(item, depth + 1, budget)
        return out
    raise ValueError(f"check_config contains a non-JSON value: {type(value).__name__}")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("value cannot be represented as canonical JSON") from exc


def _trial_identity(system: str, node: str, run_id: str, check_id: str,
                    check_version: str, check_config_sha256: str, attempt_id: str) -> dict[str, str]:
    return {
        "schema": SCHEMA_VERSION,
        "system": system,
        "node": node,
        "source": f"{system}:{node}",
        "run_id": run_id,
        "check_id": check_id,
        "check_version": check_version,
        "check_config_sha256": check_config_sha256,
        "attempt_id": attempt_id,
    }


def make_trial(*, system: str, node: str, run_id: str, check_id: str,
               check_version: str, attempt_id: str, outcome: str,
               check_config: Any = None) -> dict[str, Any]:
    """Build a v1 envelope. Non-pass outcomes are retained but never pooled as passes."""
    system = _identity(system, "system", MAX_COMPONENT_CHARS, forbid_colon=True)
    node = _identity(node, "node", MAX_COMPONENT_CHARS, forbid_colon=True)
    run_id = _identity(run_id, "run_id")
    check_id = _identity(check_id, "check_id")
    check_version = _identity(check_version, "check_version")
    attempt_id = _identity(attempt_id, "attempt_id")
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(sorted(OUTCOMES))}")

    normalized_config = _normalize_config({} if check_config is None else check_config, 0, [MAX_CONFIG_NODES])
    config_bytes = _canonical_bytes(normalized_config)
    if len(config_bytes) > MAX_CONFIG_BYTES:
        raise ValueError(f"check_config exceeds {MAX_CONFIG_BYTES} bytes")
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    identity = _trial_identity(system, node, run_id, check_id, check_version, config_digest, attempt_id)
    trial_id = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
    return {**identity, "trial_id": trial_id, "outcome": outcome}


def validate_trial_envelope(record: Any) -> bool:
    """Validate exact v1 shape, canonical identity strings, and deterministic trial ID."""
    required = {
        "schema", "trial_id", "system", "node", "source", "run_id", "check_id",
        "check_version", "check_config_sha256", "attempt_id", "outcome",
    }
    if not isinstance(record, dict) or set(record) != required:
        return False
    try:
        if record["schema"] != SCHEMA_VERSION:
            return False
        system = _identity(record["system"], "system", MAX_COMPONENT_CHARS, forbid_colon=True)
        node = _identity(record["node"], "node", MAX_COMPONENT_CHARS, forbid_colon=True)
        run_id = _identity(record["run_id"], "run_id")
        check_id = _identity(record["check_id"], "check_id")
        check_version = _identity(record["check_version"], "check_version")
        attempt_id = _identity(record["attempt_id"], "attempt_id")
        if any(record[field] != value for field, value in (
            ("system", system), ("node", node), ("run_id", run_id), ("check_id", check_id),
            ("check_version", check_version), ("attempt_id", attempt_id),
        )):
            return False
        source = f"{system}:{node}"
        if record["source"] != source or len(source) > MAX_SOURCE_CHARS:
            return False
        digest = record["check_config_sha256"]
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            return False
        if not isinstance(record["outcome"], str) or record["outcome"] not in OUTCOMES:
            return False
        expected = hashlib.sha256(_canonical_bytes(
            _trial_identity(system, node, run_id, check_id, check_version, digest, attempt_id)
        )).hexdigest()
        return (
            isinstance(record["trial_id"], str)
            and _HEX64.fullmatch(record["trial_id"]) is not None
            and record["trial_id"] == expected
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return False


def canonical_trial_json(record: dict[str, Any]) -> str:
    if not validate_trial_envelope(record):
        raise ValueError("invalid avrone.trial/v1 envelope")
    return _canonical_bytes(record).decode("utf-8")


def pool_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Project only completed pass/fail outcomes into the Great Scale pooling contract."""
    if not validate_trial_envelope(record) or record["outcome"] not in POOLED_OUTCOMES:
        return None
    return {
        "source": record["source"],
        "trial_id": record["trial_id"],
        "failed": record["outcome"] == "failed",
    }
