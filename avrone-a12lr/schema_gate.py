#!/usr/bin/env python3
"""Schema gate: the membrane between bytes and typed events."""
from __future__ import annotations
import json
from typing import Any

REQUIRED_FIELDS = {"consumer_id", "state", "ts"}
VALID_STATES = {"ready", "saturated", "retiring", "dead"}


def validate_event(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise TypeError(f"event must be a dict, got {type(raw).__name__}")
    missing = REQUIRED_FIELDS - set(raw)
    if missing:
        raise ValueError(f"missing required fields: {sorted(missing)}")
    if raw["state"] not in VALID_STATES:
        raise ValueError(f"invalid state: {raw['state']!r}")
    if not isinstance(raw["consumer_id"], str) or not raw["consumer_id"].strip():
        raise ValueError("consumer_id must be non-empty")
    if not isinstance(raw["ts"], (int, float)) or raw["ts"] <= 0:
        raise ValueError("ts must be a positive number")
    depth = raw.get("buffer_depth", 0)
    cap = raw.get("capacity", 0)
    if not isinstance(depth, int) or depth < 0:
        raise ValueError("buffer_depth must be a non-negative int")
    if not isinstance(cap, int) or cap < 0:
        raise ValueError("capacity must be a non-negative int")
    if cap and depth > cap:
        raise ValueError(f"buffer_depth {depth} exceeds capacity {cap}")
    return raw
