"""Versioned, bounded trial identity contract for cross-system evidence."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import unicodedata
from typing import Any

SCHEMA_VERSION = "avrone.trial/v2"
OUTCOMES = frozenset({"pass", "fail", "skipped", "cancelled", "timed_out", "inconclusive"})
MAX_SOURCE_COMPONENT_CHARS = 63
MAX_FIELD_CHARS = 256
MAX_CONFIG_BYTES = 16_384
MAX_CONFIG_DEPTH = 32
MAX_CONFIG_NODES = 4_096
MAX_ENVELOPE_BYTES = 24 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _identity(value: Any, field: str, max_chars: int = MAX_FIELD_CHARS) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = unicodedata.normalize("NFC", value).strip()
    if not value or len(value) > max_chars:
        raise ValueError(f"{field} must contain 1..{max_chars} characters")
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError(f"{field} must not contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid Unicode") from exc
    return value


def _config_string(value: Any, field: str) -> str:
    """NFC-normalize JSON strings without trimming meaningful whitespace."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized) > MAX_CONFIG_BYTES:
        raise ValueError(f"{field} exceeds {MAX_CONFIG_BYTES} characters")
    if any(unicodedata.category(ch) == "Cc" for ch in normalized):
        raise ValueError(f"{field} must not contain control characters")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid Unicode") from exc
    return normalized


def _normalize_json(value: Any, depth: int, budget: list[int]) -> Any:
    budget[0] += 1
    if budget[0] > MAX_CONFIG_NODES:
        raise ValueError(f"check_config exceeds {MAX_CONFIG_NODES} JSON values")
    if depth > MAX_CONFIG_DEPTH:
        raise ValueError(f"check_config exceeds maximum depth {MAX_CONFIG_DEPTH}")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value.bit_length() > 4096:
            raise ValueError("check_config integer is too large")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("check_config numbers must be finite")
        return value
    if isinstance(value, str):
        return _config_string(value, "check_config string")
    if isinstance(value, list):
        return [_normalize_json(item, depth + 1, budget) for item in value]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = _config_string(key, "check_config key")
            if normalized_key in normalized:
                raise ValueError("check_config has duplicate keys after Unicode normalization")
            normalized[normalized_key] = _normalize_json(item, depth + 1, budget)
        return normalized
    raise ValueError("check_config must contain only JSON values")


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise ValueError("value cannot be represented as canonical UTF-8 JSON") from exc


def canonical_config_bytes(value: Any | None) -> bytes:
    normalized = _normalize_json({} if value is None else value, 0, [0])
    raw = _canonical_bytes(normalized)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(f"check_config exceeds {MAX_CONFIG_BYTES} bytes")
    return raw


def _identity_document(
    *, system: str, node: str, run_id: str, check_id: str, check_version: str,
    check_config_sha256: str, attempt_id: str,
) -> dict[str, str]:
    return {
        "schema": SCHEMA_VERSION,
        "system": system,
        "node": node,
        "run_id": run_id,
        "check_id": check_id,
        "check_version": check_version,
        "check_config_sha256": check_config_sha256,
        "attempt_id": attempt_id,
    }


def make_trial(
    *, system: str, node: str, run_id: str, check_id: str, check_version: str,
    attempt_id: str, outcome: str, check_config: Any | None = None,
) -> dict[str, Any]:
    """Create an envelope. Outcome is excluded from identity so contradictions collide."""
    system = _identity(system, "system", MAX_SOURCE_COMPONENT_CHARS)
    node = _identity(node, "node", MAX_SOURCE_COMPONENT_CHARS)
    if ":" in system or ":" in node:
        raise ValueError("system and node must not contain the source delimiter ':'")
    run_id = _identity(run_id, "run_id")
    check_id = _identity(check_id, "check_id")
    check_version = _identity(check_version, "check_version")
    attempt_id = _identity(attempt_id, "attempt_id")
    if not isinstance(outcome, str) or outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(sorted(OUTCOMES))}")
    config_sha = hashlib.sha256(canonical_config_bytes(check_config)).hexdigest()
    identity = _identity_document(
        system=system, node=node, run_id=run_id, check_id=check_id,
        check_version=check_version, check_config_sha256=config_sha, attempt_id=attempt_id,
    )
    trial_id = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
    return {
        **identity,
        "trial_id": trial_id,
        "source": f"{system}:{node}",
        "outcome": outcome,
    }


def validate_trial_envelope(record: Any) -> bool:
    required = {
        "schema", "trial_id", "system", "node", "source", "run_id", "check_id",
        "check_version", "check_config_sha256", "attempt_id", "outcome",
    }
    if not isinstance(record, dict) or set(record) != required:
        return False
    if record.get("schema") != SCHEMA_VERSION:
        return False
    if not isinstance(record.get("outcome"), str) or record["outcome"] not in OUTCOMES:
        return False
    if not isinstance(record.get("trial_id"), str) or not _HEX64.fullmatch(record["trial_id"]):
        return False
    if not isinstance(record.get("check_config_sha256"), str) or not _HEX64.fullmatch(
        record["check_config_sha256"]
    ):
        return False
    try:
        system = _identity(record["system"], "system", MAX_SOURCE_COMPONENT_CHARS)
        node = _identity(record["node"], "node", MAX_SOURCE_COMPONENT_CHARS)
        if ":" in system or ":" in node:
            return False
        run_id = _identity(record["run_id"], "run_id")
        check_id = _identity(record["check_id"], "check_id")
        check_version = _identity(record["check_version"], "check_version")
        attempt_id = _identity(record["attempt_id"], "attempt_id")
        if any(record[key] != value for key, value in (
            ("system", system), ("node", node), ("run_id", run_id),
            ("check_id", check_id), ("check_version", check_version), ("attempt_id", attempt_id),
        )):
            return False
        if record["source"] != f"{system}:{node}" or len(record["source"]) > 127:
            return False
        identity = _identity_document(
            system=system, node=node, run_id=run_id, check_id=check_id,
            check_version=check_version, check_config_sha256=record["check_config_sha256"],
            attempt_id=attempt_id,
        )
        expected = hashlib.sha256(_canonical_bytes(identity)).hexdigest()
        return hmac.compare_digest(expected, record["trial_id"])
    except (KeyError, TypeError, ValueError, RecursionError, UnicodeError):
        return False


def canonical_trial_bytes(record: Any) -> bytes:
    if not validate_trial_envelope(record):
        raise ValueError("invalid avrone.trial/v2 envelope")
    raw = _canonical_bytes(record)
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise ValueError(f"trial envelope exceeds {MAX_ENVELOPE_BYTES} bytes")
    return raw


def pool_record(record: dict[str, Any]) -> dict[str, Any]:
    """Project only completed pass/fail outcomes into the legacy evidence pool."""
    if not validate_trial_envelope(record):
        raise ValueError("invalid avrone.trial/v2 envelope")
    if record["outcome"] not in {"pass", "fail"}:
        raise ValueError("non-pass outcome is not eligible for the evidence pool")
    return {
        "source": record["source"],
        "trial_id": record["trial_id"],
        "failed": record["outcome"] == "fail",
    }
