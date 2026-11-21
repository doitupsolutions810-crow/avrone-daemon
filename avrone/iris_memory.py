"""Optional Redis Iris Agent Memory adapter.

AVRONE's local MemoryStore remains authoritative for private per-agent memory and fleet
replication. Redis Iris is an opt-in second tier for owner-scoped session transcripts and
cross-session semantic recall. The integration never logs credentials or transcript content.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading
from typing import Any

log = logging.getLogger("avrone.iris_memory")
_lock = threading.Lock()
_memory: Any = None
_checked = False
_warned: set[str] = set()


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _warn_once(key: str, message: str) -> None:
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    if os.getenv("AVRONE_ENV", os.getenv("ENVIRONMENT", "")).strip().lower() != "production":
        log.warning(message)


def _owner() -> str:
    return os.getenv("AVRONE_IRIS_OWNER_ID", "").strip()


def _get_memory() -> Any:
    global _memory, _checked
    if not _truthy("AVRONE_IRIS_ENABLED", False):
        return None
    required = (
        "IRIS_AGENT_MEMORY_URL",
        "IRIS_AGENT_MEMORY_STORE_ID",
        "IRIS_AGENT_MEMORY_API_KEY",
        "AVRONE_IRIS_OWNER_ID",
    )
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        _warn_once(
            "missing:" + ",".join(missing),
            "Redis Iris enabled but required configuration is missing: " + ", ".join(missing)
            + "; local AVRONE memory remains active",
        )
        return None
    with _lock:
        if _checked:
            return _memory
        _checked = True
        try:
            AgentMemory = importlib.import_module("redis_iris.agent_memory").AgentMemory
            _memory = AgentMemory.from_env()
        except Exception as exc:  # noqa: BLE001 - optional service must never block startup
            _memory = None
            log.warning("Redis Iris SDK/service unavailable; local AVRONE memory remains active (%s)",
                        type(exc).__name__)
        return _memory


def record_turn(run_id: str, user_text: str, assistant_text: str) -> bool:
    """Append one completed AVRONE run to an owner-scoped Iris session."""
    memory = _get_memory()
    owner = _owner()
    if memory is None or not owner or not run_id:
        return False
    try:
        session_id = "avrone-" + str(run_id)
        conversation = memory.conversation(owner=owner, session_id=session_id)
        conversation.user(str(user_text))
        conversation.assistant(str(assistant_text))
        return True
    except Exception as exc:  # noqa: BLE001
        _warn_once("write-failed", f"Redis Iris session write failed; local memory is unchanged ({type(exc).__name__})")
        return False


def recall_prompt(query: str) -> str:
    """Return owner-scoped semantic memories as prompt text; empty when Iris is unavailable."""
    memory = _get_memory()
    owner = _owner()
    query = str(query or "").strip()
    if memory is None or not owner or not query:
        return ""
    try:
        result = memory.recall(query, owner=owner)
        if not result:
            return ""
        prompt = result.as_prompt() if callable(getattr(result, "as_prompt", None)) else str(result)
        return str(prompt).strip()[:12000]
    except Exception as exc:  # noqa: BLE001
        _warn_once("recall-failed", f"Redis Iris recall failed; local memory remains available ({type(exc).__name__})")
        return ""


def health() -> dict[str, Any]:
    """Read service health for an operator diagnostic; never exposes credentials."""
    memory = _get_memory()
    if memory is None:
        return {"configured": False, "healthy": False}
    try:
        result = memory.client.store_health()
        features = {
            str(name): str(getattr(getattr(feature, "status", None), "value", "unknown"))
            for name, feature in getattr(result, "features", {}).items()
        }
        status = str(getattr(getattr(result, "status", None), "value", "unknown"))
        return {"configured": True, "healthy": status.lower() == "healthy",
                "status": status, "features": features}
    except Exception as exc:  # noqa: BLE001
        return {"configured": True, "healthy": False, "error": type(exc).__name__}
