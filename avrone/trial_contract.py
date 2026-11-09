"""Versioned, deterministic trial identity and operational-outcome contracts."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import unicodedata
from typing import Any

SCHEMA_VERSION = "avrone.trial/v1"
OUTCOME_SCHEMA_VERSION = "avrone.trial-outcome/v1"
MAX_COMPONENT_CHARS = 63
MAX_FIELD_CHARS = 256
MAX_CONFIG_BYTES = 16_384
MAX_CONFIG_DEPTH = 32
MAX_CONFIG_NODES = 4096
NON_PASS_OUTCOMES = frozenset({"skipped", "cancelled", "timed_out", "inconclusive", "blocked"})
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_INTEGER = (1 << 53) - 1


def _identity(value: Any, field: str, limit: int = MAX_FIELD_CHARS) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = unicodedata.normalize("NFC", value).strip()
    if not value or len(value) > limit:
        raise ValueError(f"{field} must contain 1..{limit} characters")
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} for ch in value):
        raise ValueError(f"{field} contains a control, formatting, or surrogate character")
    if field in {"system", "node"} and ":" in value:
        raise ValueError(f"{field} must not contain ':'")
    return value


def _normalized_config(value: Any) -> Any:
    """Normalize JSON recursively, reject cycles/key collisions, and bound work before encoding."""
    seen: set[int] = set()
    nodes = 0

    def visit(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_CONFIG_NODES:
            raise ValueError("check_config exceeds the JSON node limit")
        if depth > MAX_CONFIG_DEPTH:
            raise ValueError("check_config exceeds the JSON depth limit")
        if item is None or type(item) is bool:
            return item
        if type(item) is int:
            if abs(item) > _SAFE_INTEGER:
                raise ValueError("check_config integer is outside the safe interoperable range")
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("check_config numbers must be finite")
            return item
        if type(item) is str:
            normalized = unicodedata.normalize("NFC", item)
            if any(unicodedata.category(ch) == "Cs" for ch in normalized):
                raise ValueError("check_config contains a surrogate code point")
            return normalized
        if isinstance(item, list):
            marker = id(item)
            if marker in seen:
                raise ValueError("check_config must not contain cycles")
            seen.add(marker)
            try:
                return [visit(child, depth + 1) for child in item]
            finally:
                seen.remove(marker)
        if isinstance(item, dict):
            marker = id(item)
            if marker in seen:
                raise ValueError("check_config must not contain cycles")
            seen.add(marker)
            try:
                result: dict[str, Any] = {}
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError("check_config object keys must be strings")
                    normalized_key = unicodedata.normalize("NFC", key)
                    if (not normalized_key or
                            any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"}
                                for ch in normalized_key)):
                        raise ValueError("check_config contains an invalid object key")
                    if normalized_key in result:
                        raise ValueError("check_config keys collide after NFC normalization")
                    result[normalized_key] = visit(child, depth + 1)
                return result
            finally:
                seen.remove(marker)
        raise ValueError("check_config must contain only JSON values")

    return visit(value, 0)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise ValueError("value is not canonical finite UTF-8 JSON") from exc


def _identity_payload(*, system: str, node: str, run_id: str, check_id: str,
                      check_version: str, attempt_id: str,
                      check_config: Any | None = None) -> dict[str, str]:
    system = _identity(system, "system", MAX_COMPONENT_CHARS)
    node = _identity(node, "node", MAX_COMPONENT_CHARS)
    run_id = _identity(run_id, "run_id")
    check_id = _identity(check_id, "check_id")
    check_version = _identity(check_version, "check_version")
    attempt_id = _identity(attempt_id, "attempt_id")
    config_bytes = _canonical(_normalized_config({} if check_config is None else check_config))
    if len(config_bytes) > MAX_CONFIG_BYTES:
        raise ValueError(f"check_config exceeds {MAX_CONFIG_BYTES} bytes")
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    return {
        "schema": SCHEMA_VERSION,
        "system": system,
        "node": node,
        "run_id": run_id,
        "check_id": check_id,
        "check_version": check_version,
        "check_config_sha256": config_sha,
        "attempt_id": attempt_id,
    }


def _trial_id(identity: dict[str, str]) -> str:
    return hashlib.sha256(_canonical(identity)).hexdigest()


def make_trial(*, system: str, node: str, run_id: str, check_id: str,
               check_version: str, attempt_id: str, failed: bool,
               check_config: Any | None = None) -> dict[str, Any]:
    """Build a completed pass/fail trial. Outcome is excluded from the stable identity."""
    if type(failed) is not bool:
        raise ValueError("failed must be a boolean")
    identity = _identity_payload(
        system=system, node=node, run_id=run_id, check_id=check_id,
        check_version=check_version, attempt_id=attempt_id,
        check_config=check_config,
    )
    return {
        "schema": SCHEMA_VERSION,
        "trial_id": _trial_id(identity),
        "system": identity["system"],
        "node": identity["node"],
        "source": f'{identity["system"]}:{identity["node"]}',
        "run_id": identity["run_id"],
        "check_id": identity["check_id"],
        "check_version": identity["check_version"],
        "check_config_sha256": identity["check_config_sha256"],
        "attempt_id": identity["attempt_id"],
        "failed": failed,
    }


def make_trial_outcome(*, system: str, node: str, run_id: str, check_id: str,
                       check_version: str, attempt_id: str, outcome: str,
                       check_config: Any | None = None) -> dict[str, Any]:
    """Build a non-poolable operational outcome without inventing a pass or failure."""
    if not isinstance(outcome, str) or outcome not in NON_PASS_OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(sorted(NON_PASS_OUTCOMES))}")
    identity = _identity_payload(
        system=system, node=node, run_id=run_id, check_id=check_id,
        check_version=check_version, attempt_id=attempt_id,
        check_config=check_config,
    )
    return {
        "schema": OUTCOME_SCHEMA_VERSION,
        "trial_id": _trial_id(identity),
        "system": identity["system"],
        "node": identity["node"],
        "source": f'{identity["system"]}:{identity["node"]}',
        "run_id": identity["run_id"],
        "check_id": identity["check_id"],
        "check_version": identity["check_version"],
        "check_config_sha256": identity["check_config_sha256"],
        "attempt_id": identity["attempt_id"],
        "outcome": outcome,
    }


def _record_identity(record: Any, expected_schema: str) -> dict[str, str] | None:
    if not isinstance(record, dict):
        return None
    try:
        system = _identity(record["system"], "system", MAX_COMPONENT_CHARS)
        node = _identity(record["node"], "node", MAX_COMPONENT_CHARS)
        run_id = _identity(record["run_id"], "run_id")
        check_id = _identity(record["check_id"], "check_id")
        check_version = _identity(record["check_version"], "check_version")
        attempt_id = _identity(record["attempt_id"], "attempt_id")
        config_sha = record["check_config_sha256"]
        if any(record[field] != value for field, value in (
            ("system", system), ("node", node), ("run_id", run_id),
            ("check_id", check_id), ("check_version", check_version),
            ("attempt_id", attempt_id),
        )):
            return None
        if not isinstance(config_sha, str) or not _HEX64.fullmatch(config_sha):
            return None
        source = record["source"]
        if not isinstance(source, str) or source != f"{system}:{node}" or len(source) > 127:
            return None
        if record["schema"] != expected_schema:
            return None
        return {
            "schema": SCHEMA_VERSION,
            "system": system,
            "node": node,
            "run_id": run_id,
            "check_id": check_id,
            "check_version": check_version,
            "check_config_sha256": config_sha,
            "attempt_id": attempt_id,
        }
    except (KeyError, TypeError, ValueError):
        return None


def validate_trial_envelope(record: Any) -> bool:
    required = {
        "schema", "trial_id", "system", "node", "source", "run_id", "check_id",
        "check_version", "check_config_sha256", "attempt_id", "failed",
    }
    if not isinstance(record, dict) or set(record) != required or type(record.get("failed")) is not bool:
        return False
    identity = _record_identity(record, SCHEMA_VERSION)
    supplied = record.get("trial_id")
    if identity is None or not isinstance(supplied, str) or not _HEX64.fullmatch(supplied):
        return False
    try:
        return hmac.compare_digest(_trial_id(identity), supplied)
    except (TypeError, ValueError, RecursionError):
        return False


def validate_trial_outcome(record: Any) -> bool:
    required = {
        "schema", "trial_id", "system", "node", "source", "run_id", "check_id",
        "check_version", "check_config_sha256", "attempt_id", "outcome",
    }
    if not isinstance(record, dict) or set(record) != required:
        return False
    if not isinstance(record.get("outcome"), str) or record.get("outcome") not in NON_PASS_OUTCOMES:
        return False
    identity = _record_identity(record, OUTCOME_SCHEMA_VERSION)
    supplied = record.get("trial_id")
    if identity is None or not isinstance(supplied, str) or not _HEX64.fullmatch(supplied):
        return False
    try:
        return hmac.compare_digest(_trial_id(identity), supplied)
    except (TypeError, ValueError, RecursionError):
        return False


def validate_trial_record(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("schema") == SCHEMA_VERSION:
        return validate_trial_envelope(record)
    if record.get("schema") == OUTCOME_SCHEMA_VERSION:
        return validate_trial_outcome(record)
    return False


def trial_record_outcome(record: dict[str, Any]) -> str:
    if not validate_trial_record(record):
        raise ValueError("trial record contract rejected")
    if record["schema"] == SCHEMA_VERSION:
        return "failed" if record["failed"] else "passed"
    return record["outcome"]


def trial_record_poolable(record: dict[str, Any]) -> bool:
    return validate_trial_envelope(record)
