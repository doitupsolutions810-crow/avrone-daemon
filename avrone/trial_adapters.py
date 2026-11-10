"""Versioned fleet and bridge adapters for the AVRONE trial receiver.

The receive methods are intentionally called only after FleetManager.dispatch_gateway
has authenticated the exact request bytes and applied the sender allowlist/capability policy.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from .trial_contract import canonical_trial_bytes, validate_trial_envelope
from .trial_receiver import MAX_BATCH, TrialStore

TRIAL_CAPABILITY = "evidence:trial:ingest"
FLEET_TRIAL_SCHEMA = "avrone.trial-ingest/v1"
BRIDGE_TRIAL_SCHEMA = "avrone.bridge-trial-ingest/v1"
FLEET_TRIAL_PATH = "/api/fleet/trials"
BRIDGE_TRIAL_PATH = "/api/fleet/bridge/trials"
SENDER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value} is not allowed")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number is outside the finite float range")
    return parsed


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key}")
        out[key] = value
    return out


def _check_json_bounds(value: Any) -> None:
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        if depth > 32:
            raise ValueError("request JSON exceeds maximum depth 32")
        if isinstance(item, dict):
            nodes += len(item)
            for key, child in item.items():
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ValueError("request JSON object key contains invalid Unicode") from exc
                stack.append((child, depth + 1))
        elif isinstance(item, list):
            nodes += len(item)
            stack.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("request JSON string contains invalid Unicode") from exc
        if nodes > 100_000:
            raise ValueError("request JSON exceeds 100000 values")


def strict_json_loads(raw: bytes) -> Any:
    """Parse UTF-8 JSON while rejecting duplicate keys, non-finite values and deep payloads."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("request body must be bytes")
    try:
        parsed = json.loads(
            bytes(raw).decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
        _check_json_bounds(parsed)
        return parsed
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise ValueError("request body must be unambiguous finite bounded UTF-8 JSON") from exc


def _validate_batch(
    payload: Any, *, schema: str, sender: str, max_hops: int, bridge: bool,
) -> tuple[list[dict[str, Any]], str | None]:
    expected = {"schema", "from", "hops", "capability", "trials"}
    if bridge:
        expected.add("run_id")
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("trial relay payload has missing or unexpected fields")
    if payload.get("schema") != schema:
        raise ValueError(f"expected {schema}")
    if not isinstance(sender, str) or not SENDER_RE.fullmatch(sender):
        raise ValueError("authenticated sender is not a valid fleet node id")
    if payload.get("from") != sender:
        raise ValueError("signed sender does not match the fleet gateway identity")
    hops = payload.get("hops")
    if isinstance(hops, bool) or not isinstance(hops, int) or not 1 <= hops <= max_hops:
        raise ValueError("trial relay hops are outside the permitted bound")
    if payload.get("capability") != TRIAL_CAPABILITY:
        raise ValueError("trial relay requires evidence:trial:ingest capability")
    trials = payload.get("trials")
    if not isinstance(trials, list) or not 1 <= len(trials) <= MAX_BATCH:
        raise ValueError(f"trials must contain 1..{MAX_BATCH} envelopes")
    for index, trial in enumerate(trials):
        if not validate_trial_envelope(trial):
            raise ValueError(f"trial at index {index} is not a valid avrone.trial/v2 envelope")
        canonical_trial_bytes(trial)
    run_id = None
    if bridge:
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 128:
            raise ValueError("bridge run_id must contain 1..128 characters")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in run_id):
            raise ValueError("bridge run_id must not contain control characters")
        try:
            run_id.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("bridge run_id must be valid Unicode") from exc
        # Match Bridge's existing deep-mode behavior: <run>-wave1 shares <run>'s bridge.
        from .bridge import bridge_run_id
        run_id = bridge_run_id(run_id)
        if not run_id.strip():
            raise ValueError("canonical bridge run_id must not be empty")
    return trials, run_id


class FleetTrialAdapter:
    """Fleet HTTP adapter; the server invokes receive only after HMAC verification."""

    def __init__(self, store: TrialStore):
        self.store = store

    def receive(self, payload: Any, sender: str, max_hops: int) -> dict[str, Any]:
        trials, _ = _validate_batch(
            payload, schema=FLEET_TRIAL_SCHEMA, sender=sender, max_hops=max_hops, bridge=False,
        )
        return self.store.ingest_many(trials, sender=sender, transport="fleet")

    @staticmethod
    async def send(manager: Any, node_id: str, trials: list[dict[str, Any]]) -> dict[str, Any]:
        manager.check_outbound("trials", TRIAL_CAPABILITY, 0, node_id)
        node = manager.pick(node_id)
        payload = {
            "schema": FLEET_TRIAL_SCHEMA,
            "from": manager.node_id,
            "hops": 1,
            "capability": TRIAL_CAPABILITY,
            "trials": trials,
        }
        _validate_batch(
            payload, schema=FLEET_TRIAL_SCHEMA, sender=manager.node_id,
            max_hops=manager.max_hops, bridge=False,
        )
        return await manager._post(node["url"], FLEET_TRIAL_PATH, payload, timeout=15)


class BridgeTrialAdapter:
    """Bridge relay contract for trial envelopes; it does not alter Bridge JSONL posts."""

    def __init__(self, store: TrialStore):
        self.store = store

    def receive(self, payload: Any, sender: str, max_hops: int) -> dict[str, Any]:
        trials, run_id = _validate_batch(
            payload, schema=BRIDGE_TRIAL_SCHEMA, sender=sender, max_hops=max_hops, bridge=True,
        )
        return self.store.ingest_many(
            trials, sender=sender, transport="bridge", bridge_run_id=run_id,
        )

    @staticmethod
    async def send(
        manager: Any, node_id: str, run_id: str, trials: list[dict[str, Any]],
    ) -> dict[str, Any]:
        manager.check_outbound("trials", TRIAL_CAPABILITY, 0, node_id)
        node = manager.pick(node_id)
        payload = {
            "schema": BRIDGE_TRIAL_SCHEMA,
            "from": manager.node_id,
            "hops": 1,
            "capability": TRIAL_CAPABILITY,
            "run_id": run_id,
            "trials": trials,
        }
        _, canonical_run_id = _validate_batch(
            payload, schema=BRIDGE_TRIAL_SCHEMA, sender=manager.node_id,
            max_hops=manager.max_hops, bridge=True,
        )
        payload["run_id"] = canonical_run_id
        return await manager._post(node["url"], BRIDGE_TRIAL_PATH, payload, timeout=15)
