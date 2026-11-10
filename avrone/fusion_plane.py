"""How the daemon reads a capability-authority execute response.

Inspected, not assumed:

- ``capability-authority`` ``main`` (``012dc7d5``, 2026-10-30) ``Executor.execute``
  returns ``{"status": "ok", "result": "executed"}`` with no runner and no
  ``execution_status``. Its HTTP default port is ``PLANE_PORT`` **8090**, the
  same port CycleKernal's CognitiveBridge binds.
- PR #17 (``ee13c32d``, open, not merged) defaults to port **8095**. ``execute``
  returns ``execution_status: NOT_IMPLEMENTED`` for every action except the
  reviewed ``Execute:artifact.write_demo:POST`` dispatcher, which returns
  ``SUCCEEDED``.

This module does not call the plane, sign a capability, or run an action.
Fusion delivery does not go through ``/v1/execute``. A ``SUCCEEDED`` result is
the only shape counted as execution, and only because that one dispatcher
exists on the unmerged branch. The main stub is not execution.
"""
from __future__ import annotations

from typing import Any

# Pins from the read-only inspection. The evidence script re-reads the remotes.
PLANE_MAIN_SHA = "012dc7d5ed254c2ece5c7c1782fb6d3eba3e08ce"
PLANE_PR17_SHA = "ee13c32d4e8e90ad1f952fb8bdda7e5a8a0e459e"
PLANE_MAIN_PORT = 8090
PLANE_PR17_PORT = 8095
NARROW_ACTION = "Execute:artifact.write_demo:POST"


def classify_execute(body: Any) -> dict[str, Any]:
    """Classify a ``/v1/execute`` JSON body. ``executed`` is true only for SUCCEEDED.

    The main-branch stub (``result: executed`` and no ``execution_status``) is
    ``unscoped_stub``. ``NOT_IMPLEMENTED`` stays not executed. Nothing here
    performs the action locally.
    """
    if not isinstance(body, dict):
        return {"executed": False, "reason": "malformed", "runner": "none"}
    status = body.get("execution_status")
    if status == "SUCCEEDED" and body.get("status") == "executed":
        return {"executed": True, "reason": "plane_succeeded", "runner": "plane"}
    if status == "NOT_IMPLEMENTED":
        return {"executed": False, "reason": "not_implemented", "runner": "none"}
    if status is None and body.get("result") == "executed" and body.get("status") in {"ok", "executed"}:
        return {"executed": False, "reason": "unscoped_stub", "runner": "none"}
    if body.get("status") == "error":
        return {"executed": False, "reason": str(body.get("error") or "error"), "runner": "none"}
    return {"executed": False, "reason": "not_executed", "runner": "none"}


def plane_availability() -> dict[str, Any]:
    """Static record of what was actually on the remotes. No network call."""
    return {
        "main_sha": PLANE_MAIN_SHA,
        "main_port": PLANE_MAIN_PORT,
        "main_execute": "unscoped_stub",
        "pr17": 17,
        "pr17_sha": PLANE_PR17_SHA,
        "pr17_port": PLANE_PR17_PORT,
        "pr17_merged": False,
        "pr17_execute": "NOT_IMPLEMENTED",
        "narrow_action": NARROW_ACTION,
        "daemon_calls_execute": False,
    }
