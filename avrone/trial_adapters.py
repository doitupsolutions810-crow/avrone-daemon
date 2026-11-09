"""Thin adapters over AVRONE Bridge and the existing authenticated Fleet transport."""
from __future__ import annotations

import json
import math
from typing import Any

from .bridge import bridge_run_id
from .fleet import GatewayRefused
from .trial_contract import validate_trial_record
from .trial_receiver import MAX_BATCH_EVENTS, MAX_BODY_BYTES

TRIALS_PATH = "/api/fleet/trials"


class BridgeTrialAdapter:
    """Attach read-only Bridge correlation metadata; never store authoritative trials in Bridge."""

    @staticmethod
    def wrap(bridge: Any, record: dict[str, Any],
             post_ids: list[str] | tuple[str, ...] | None = None) -> dict[str, Any]:
        if not validate_trial_record(record):
            raise ValueError("trial record contract rejected")
        node_id = str(getattr(bridge, "node_id", "") or "")
        run_id = str(getattr(bridge, "run_id", "") or "")
        if node_id != record["node"] or bridge_run_id(run_id) != record["run_id"]:
            raise ValueError("Bridge node/run context does not match the trial identity")
        if post_ids is None:
            refs = []
        elif isinstance(post_ids, (list, tuple)):
            refs = list(post_ids)
        else:
            raise ValueError("Bridge post references must be a list or tuple")
        if len(refs) > 20:
            raise ValueError("at most 20 Bridge post references may be attached")
        clean: list[str] = []
        for ref in refs:
            if not isinstance(ref, str) or ref in clean:
                raise ValueError("Bridge post references must be unique strings")
            post = bridge.get(ref)
            if (not isinstance(post, dict) or post.get("origin_node") != node_id
                    or post.get("run_id") != run_id or post.get("id") != ref):
                raise ValueError(f"Bridge post {ref!r} is missing or not locally originated")
            clean.append(ref)
        return {
            "record": dict(record),
            "bridge": {
                "schema": "avrone.bridge-trial/v1",
                "run_id": run_id,
                "node_id": node_id,
                "post_ids": clean,
            },
        }


class FleetTrialAdapter:
    """Send bounded trial deliveries through FleetManager._post and its existing HMAC signing."""

    @staticmethod
    async def send(fleet: Any, target: str,
                   events: list[dict[str, Any]], timeout: float = 30.0) -> dict[str, Any]:
        if not isinstance(target, str) or not target:
            raise ValueError("trial relay target must be a non-empty node id")
        if not getattr(fleet, "enabled", False):
            raise GatewayRefused("no_token", "fleet disabled: set AVRONE_FLEET_TOKEN")
        allow = getattr(fleet, "allow", set())
        if not allow or target not in allow:
            raise GatewayRefused(
                "target_not_allowed",
                "trial relay requires an explicit AVRONE_FLEET_ALLOW target",
            )
        if not isinstance(events, list) or not 1 <= len(events) <= MAX_BATCH_EVENTS:
            raise ValueError(f"events must contain 1..{MAX_BATCH_EVENTS} deliveries")
        for item in events:
            if not isinstance(item, dict) or set(item) != {"record", "bridge"}:
                raise ValueError("each event must contain exactly record and bridge")
            if not validate_trial_record(item["record"]):
                raise ValueError("trial record contract rejected")
        payload = {
            "from": fleet.node_id,
            "hops": 1,
            "capability": "trials:ingest",
            "events": events,
        }
        if len(json.dumps(payload).encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError(f"trial batch exceeds {MAX_BODY_BYTES} bytes")
        try:
            requested_timeout = float(timeout)
        except (TypeError, ValueError):
            requested_timeout = 30.0
        if not math.isfinite(requested_timeout):
            requested_timeout = 30.0
        # Use the public Fleet egress contract; adapter code never reaches into _post.
        return await fleet.post_trial_batch(
            target, payload, timeout=max(1.0, min(60.0, requested_timeout)),
        )
