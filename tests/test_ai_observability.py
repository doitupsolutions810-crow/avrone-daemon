from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from avrone import ai_observability


def reset_module():
    ai_observability._client = None
    ai_observability._client_checked = False
    ai_observability._warned.clear()


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    reset_module()
    for key in (
        "POSTHOG_PROJECT_TOKEN", "POSTHOG_HOST", "AVRONE_POSTHOG_ENABLED",
        "AVRONE_POSTHOG_CAPTURE_CONTENT", "AVRONE_ENV", "ENVIRONMENT",
    ):
        monkeypatch.delenv(key, raising=False)
    yield
    reset_module()


def setup_posthog(monkeypatch, *, capture_content="false", fail_capture=False):
    events = []

    class FakePosthog:
        def __init__(self, token, host):
            assert token == "test-project-token"
            assert host == "https://us.i.posthog.com"

        def capture(self, **kwargs):
            if fail_capture:
                raise RuntimeError("offline")
            events.append(kwargs)

    monkeypatch.setitem(sys.modules, "posthog", SimpleNamespace(Posthog=FakePosthog))
    monkeypatch.setenv("POSTHOG_PROJECT_TOKEN", "test-project-token")
    monkeypatch.setenv("POSTHOG_HOST", "https://us.i.posthog.com")
    monkeypatch.setenv("AVRONE_POSTHOG_ENABLED", "true")
    monkeypatch.setenv("AVRONE_POSTHOG_CAPTURE_CONTENT", capture_content)
    return events


def test_generation_capture_uses_agent_trace_ids_and_omits_content_by_default(monkeypatch):
    events = setup_posthog(monkeypatch)
    ai_observability.capture_generation(
        session="avrone:run123:Builder",
        provider="ollama",
        model="qwen2.5:3b",
        latency_s=0.25,
        messages=[{"role": "user", "content": "private prompt"}],
        output="private response",
    )
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "$ai_generation"
    props = event["properties"]
    assert props["$ai_trace_id"] == "avrone:run123:Builder"
    assert props["$ai_session_id"] == "avrone:run123"
    assert props["$ai_provider"] == "ollama"
    assert props["$ai_model"] == "qwen2.5:3b"
    assert props["$ai_latency"] == pytest.approx(0.25)
    assert "$ai_input" not in props
    assert "$ai_output_choices" not in props


def test_content_is_captured_only_after_explicit_opt_in(monkeypatch):
    events = setup_posthog(monkeypatch, capture_content="true")
    messages = [{"role": "user", "content": "operator-approved prompt"}]
    ai_observability.capture_generation(
        session="avrone:run124:Planner",
        provider="openai",
        model="model-x",
        latency_s=1.0,
        messages=messages,
        output="answer",
    )
    props = events[0]["properties"]
    assert props["$ai_input"] == messages
    assert props["$ai_output_choices"] == [{"role": "assistant", "content": "answer"}]


def test_tool_span_is_linked_to_agent_trace_and_redacts_payload_by_default(monkeypatch):
    events = setup_posthog(monkeypatch)
    ai_observability.capture_tool_span(
        run_id="run789", agent="Engineer", tool="shell",
        args={"cmd": "secret command"}, output="secret output",
        latency_s=0.03, error=False,
    )
    event = events[0]
    assert event["event"] == "$ai_span"
    props = event["properties"]
    assert props["$ai_trace_id"] == "avrone:run789:Engineer"
    assert props["$ai_session_id"] == "avrone:run789"
    assert props["$ai_parent_id"] == props["$ai_trace_id"]
    assert props["$ai_span_name"] == "shell"
    assert props["$ai_is_error"] is False
    assert "$ai_input_state" not in props
    assert "$ai_output_state" not in props


def test_missing_config_and_capture_failure_never_raise(monkeypatch):
    monkeypatch.setenv("AVRONE_POSTHOG_ENABLED", "true")
    # Incomplete configuration disables capture without raising.
    ai_observability.capture_generation(
        session="avrone:r:a", provider="ollama", model="local",
        latency_s=0.1, error=True,
    )
    reset_module()
    setup_posthog(monkeypatch, fail_capture=True)
    ai_observability.capture_tool_span(
        run_id="r", agent="a", tool="fs_read", args={}, output="",
        latency_s=0.0, error=True,
    )
