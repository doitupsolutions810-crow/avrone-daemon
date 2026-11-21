"""Optional PostHog AI Observability for AVRONE.

Telemetry is deliberately fail-open: missing configuration, an unavailable SDK, or an
ingestion failure must never interrupt an agent run. Prompt and tool payload capture is
disabled unless the operator explicitly opts in with AVRONE_POSTHOG_CAPTURE_CONTENT=1.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading
import uuid
from typing import Any

log = logging.getLogger("avrone.ai_observability")
_lock = threading.Lock()
_client: Any = None
_client_checked = False
_warned: set[str] = set()


def _truthy(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def is_enabled() -> bool:
    """True only when telemetry is explicitly or implicitly enabled and fully configured."""
    token = os.getenv("POSTHOG_PROJECT_TOKEN", "").strip()
    host = os.getenv("POSTHOG_HOST", "").strip()
    enabled = _truthy("AVRONE_POSTHOG_ENABLED", bool(token and host))
    return bool(enabled and token and host)


def _warn_once(key: str, message: str) -> None:
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    if os.getenv("AVRONE_ENV", os.getenv("ENVIRONMENT", "")).strip().lower() != "production":
        log.warning(message)


def _get_client() -> Any:
    """Return the one PostHog client for this process, or None when not configured."""
    global _client, _client_checked
    project_token = os.getenv("POSTHOG_PROJECT_TOKEN", "").strip()
    host = os.getenv("POSTHOG_HOST", "").strip()
    enabled = _truthy("AVRONE_POSTHOG_ENABLED", bool(project_token and host))
    if not enabled:
        return None
    if not project_token or not host:
        missing = "POSTHOG_PROJECT_TOKEN" if not project_token else "POSTHOG_HOST"
        _warn_once(
            "missing:" + missing,
            f"{missing} variable required by PostHog is missing or un-configured; "
            "AI telemetry is disabled until it is configured",
        )
        return None
    with _lock:
        if _client_checked:
            return _client
        _client_checked = True
        try:
            Posthog = importlib.import_module("posthog").Posthog
            _client = Posthog(project_token, host=host)
        except Exception as exc:  # telemetry is never allowed to block the runtime
            _client = None
            log.warning("PostHog SDK unavailable; AI telemetry remains disabled (%s)", type(exc).__name__)
        return _client


def _identity() -> str:
    # This is a non-personal service identity. The runtime does not currently have a
    # per-request authenticated user principal to pass into the model gateway.
    return "avrone-runtime"


def _trace_ids(session: str | None) -> tuple[str, str | None]:
    if not isinstance(session, str) or not session:
        return uuid.uuid4().hex, None
    # PostHog IDs accept a restricted character set. Agent labels can contain spaces or
    # punctuation, so normalize them without changing the AVRONE run's grouping structure.
    import re
    safe = re.sub(r"[^A-Za-z0-9._@()!':|~-]", "-", session).replace(" ", "-")[:200]
    parts = safe.split(":")
    session_id = ":".join(parts[:2]) if len(parts) >= 3 and parts[0] == "avrone" else safe
    return safe or uuid.uuid4().hex, session_id


def _capture(event: str, properties: dict[str, Any]) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.capture(distinct_id=_identity(), event=event, properties=properties)
    except Exception as exc:  # noqa: BLE001
        _warn_once("capture-failed", f"PostHog capture failed; AVRONE continues ({type(exc).__name__})")


def capture_generation(
    *,
    session: str | None,
    provider: str,
    model: str,
    latency_s: float,
    messages: list[dict[str, Any]] | None = None,
    output: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    error: bool = False,
) -> None:
    """Capture one completed or failed upstream generation, without changing its result."""
    trace_id, session_id = _trace_ids(session)
    props: dict[str, Any] = {
        "$ai_trace_id": trace_id,
        "$ai_session_id": session_id,
        "$ai_provider": str(provider)[:100],
        "$ai_model": str(model)[:200],
        "$ai_latency": max(0.0, float(latency_s)),
        "$ai_is_error": bool(error),
    }
    if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) and input_tokens >= 0:
        props["$ai_input_tokens"] = input_tokens
    if isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens >= 0:
        props["$ai_output_tokens"] = output_tokens
    if _truthy("AVRONE_POSTHOG_CAPTURE_CONTENT", False):
        if messages is not None:
            props["$ai_input"] = messages
        if output is not None:
            props["$ai_output_choices"] = [{"role": "assistant", "content": str(output)[:12000]}]
    _capture("$ai_generation", props)


def capture_tool_span(
    *,
    run_id: str,
    agent: str,
    tool: str,
    args: Any,
    output: str,
    latency_s: float,
    error: bool,
) -> None:
    """Capture a tool execution span. Payloads stay local unless explicitly opted in."""
    trace_id, session_id = _trace_ids(f"avrone:{run_id}:{agent}")
    span_id = uuid.uuid4().hex
    props: dict[str, Any] = {
        "$ai_trace_id": trace_id,
        "$ai_session_id": session_id,
        "$ai_span_id": span_id,
        "$ai_parent_id": trace_id,
        "$ai_span_name": str(tool)[:200],
        "$ai_latency": max(0.0, float(latency_s)),
        "$ai_is_error": bool(error),
    }
    if _truthy("AVRONE_POSTHOG_CAPTURE_CONTENT", False):
        props["$ai_input_state"] = args
        props["$ai_output_state"] = str(output)[:12000]
    _capture("$ai_span", props)


def shutdown() -> None:
    """Flush the process client during orderly shutdown, without raising."""
    client = _client
    if client is None:
        return
    try:
        close = getattr(client, "shutdown", None) or getattr(client, "flush", None)
        if callable(close):
            close()
    except Exception as exc:  # noqa: BLE001
        log.warning("PostHog shutdown failed; AVRONE exits normally (%s)", type(exc).__name__)
