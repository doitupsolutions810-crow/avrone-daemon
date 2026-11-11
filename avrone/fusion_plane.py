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
    # round 48: only exact str values count. A list status raised TypeError (unhashable, in
    # the set test); a str subclass or any object with an always-true __eq__ could pose as
    # "SUCCEEDED"; a dict subclass's own .get() could answer anything; and the error reason was
    # str() of whatever the body carried (unbounded, multi-line, or a hostile __str__)
    if not isinstance(body, dict):
        return {"executed": False, "reason": "malformed", "runner": "none"}
    status = _exact_str(dict.get(body, "execution_status"))
    outer = _exact_str(dict.get(body, "status"))
    result = dict.get(body, "result")
    error = dict.get(body, "error")
    has_error = not (error is None or error is False or (type(error) is str and error == ""))
    if status == "SUCCEEDED" and outer == "executed":
        if has_error or _exact_str(result) == "not_executed":
            return {"executed": False, "reason": "contradictory", "runner": "none"}
        return {"executed": True, "reason": "plane_succeeded", "runner": "plane"}
    if status == "NOT_IMPLEMENTED":
        return {"executed": False, "reason": "not_implemented", "runner": "none"}
    if (dict.get(body, "execution_status") is None and _exact_str(result) == "executed"
            and outer in ("ok", "executed")):
        return {"executed": False, "reason": "unscoped_stub", "runner": "none"}
    if outer == "error":
        return {"executed": False, "reason": "error", "runner": "none",
                "detail": _detail(dict.get(body, "error"))}
    return {"executed": False, "reason": "not_executed", "runner": "none"}


_DETAIL_MAX = 200


def _exact_str(v: Any) -> str | None:
    return v if type(v) is str else None


def _detail(v: Any) -> str:
    """The plane's error text as one short, printable line. Never a reason code: an error
    spelled "plane_succeeded" used to come back as reason "plane_succeeded"."""
    if v is None or v is False or (type(v) is str and v == ""):
        return "error"
    if type(v) is not str:
        return type(v).__name__  # never str() a foreign object
    import unicodedata
    out = []
    for ch in v[: _DETAIL_MAX * 4]:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Zl", "Zp", "Cs"):
            ch = " "
        out.append(ch)
    text = " ".join("".join(out).split())
    return (text[:_DETAIL_MAX] + "...") if len(text) > _DETAIL_MAX else (text or "error")


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
