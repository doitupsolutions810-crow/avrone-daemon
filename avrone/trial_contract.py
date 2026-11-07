"""Versioned identity contract for evidence trials crossing system boundaries.

A trial ID is derived from execution identity, not outcome. Replaying the same
attempt produces the same ID; a new attempt must supply a new attempt_id.
"""
from __future__ import annotations
import hashlib, json, re, unicodedata
from typing import Any

SCHEMA_VERSION = "avrone.trial/v1"
MAX_FIELD_CHARS = 256
MAX_CONFIG_BYTES = 16_384
_HEX64 = re.compile(r"^[0-9a-f]{64}$")

def _identity(value: Any, field: str) -> str:
    if not isinstance(value, str): raise ValueError(f"{field} must be a string")
    value = unicodedata.normalize("NFC", value).strip()
    if not value or len(value) > MAX_FIELD_CHARS: raise ValueError(f"{field} must contain 1..{MAX_FIELD_CHARS} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value): raise ValueError(f"{field} must not contain control characters")
    return value

def _canonical(value: Any) -> bytes:
    try: raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc: raise ValueError("check_config must be finite JSON data") from exc
    if len(raw) > MAX_CONFIG_BYTES: raise ValueError(f"check_config exceeds {MAX_CONFIG_BYTES} bytes")
    return raw

def make_trial(*, system: str, node: str, run_id: str, check_id: str, check_version: str, attempt_id: str, failed: bool, check_config: Any | None = None) -> dict[str, Any]:
    """Build a bounded envelope compatible with evidence_pool.pool_trials."""
    if not isinstance(failed, bool): raise ValueError("failed must be a boolean")
    system, node = _identity(system, "system"), _identity(node, "node")
    run_id, check_id = _identity(run_id, "run_id"), _identity(check_id, "check_id")
    check_version, attempt_id = _identity(check_version, "check_version"), _identity(attempt_id, "attempt_id")
    config = _canonical({} if check_config is None else check_config)
    config_sha = hashlib.sha256(config).hexdigest()
    identity = {"schema": SCHEMA_VERSION, "system": system, "node": node, "run_id": run_id, "check_id": check_id, "check_version": check_version, "check_config_sha256": config_sha, "attempt_id": attempt_id}
    trial_id = hashlib.sha256(_canonical(identity)).hexdigest()
    return {"schema": SCHEMA_VERSION, "trial_id": trial_id, "source": f"{system}:{node}", "run_id": run_id, "check_id": check_id, "check_version": check_version, "check_config_sha256": config_sha, "attempt_id": attempt_id, "failed": failed}

def validate_trial_envelope(record: Any) -> bool:
    required = {"schema", "trial_id", "source", "run_id", "check_id", "check_version", "check_config_sha256", "attempt_id", "failed"}
    if not isinstance(record, dict) or set(record) != required: return False
    if record.get("schema") != SCHEMA_VERSION or not isinstance(record.get("failed"), bool): return False
    if not isinstance(record.get("trial_id"), str) or not _HEX64.fullmatch(record["trial_id"]): return False
    if not isinstance(record.get("check_config_sha256"), str) or not _HEX64.fullmatch(record["check_config_sha256"]): return False
    try:
        for field in ("source", "run_id", "check_id", "check_version", "attempt_id"): _identity(record[field], field)
    except ValueError: return False
    return True
