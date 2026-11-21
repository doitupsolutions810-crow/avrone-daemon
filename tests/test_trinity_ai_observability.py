from __future__ import annotations

from types import SimpleNamespace

import pytest

from avrone import ai_observability
from avrone.trinity import TrinityCore


@pytest.mark.asyncio
async def test_trinity_tool_execution_emits_posthog_span(monkeypatch):
    spans = []
    emitted = []

    class Codex:
        def emit(self, *args, **kwargs):
            emitted.append((args, kwargs))

    class Tools:
        async def call(self, ctx, tool, args):
            return "tool output"

    core = TrinityCore.__new__(TrinityCore)
    core.codex = Codex()
    core.tools = Tools()
    ctx = SimpleNamespace(recent_calls=[])
    monkeypatch.setattr(ai_observability, "capture_tool_span", lambda **kw: spans.append(kw))

    result = await core._one_call(
        "run-abc", "Builder", ctx,
        {"tool": "fs_read", "args": {"path": "README.md"}, "error": None},
        step=1, budget={},
    )

    assert result == ("fs_read", "tool output")
    assert spans and spans[0]["run_id"] == "run-abc"
    assert spans[0]["agent"] == "Builder"
    assert spans[0]["tool"] == "fs_read"
    assert spans[0]["error"] is False
    assert ctx.recent_calls[0][0] == "fs_read"
    assert any(item[0][1] == "tool.result" for item in emitted)


@pytest.mark.asyncio
async def test_trinity_validation_error_is_still_a_tool_span(monkeypatch):
    spans = []

    class Codex:
        def emit(self, *args, **kwargs):
            pass

    class Tools:
        async def call(self, ctx, tool, args):
            raise AssertionError("pre-rejected calls must not execute")

    core = TrinityCore.__new__(TrinityCore)
    core.codex = Codex()
    core.tools = Tools()
    ctx = SimpleNamespace(recent_calls=[])
    monkeypatch.setattr(ai_observability, "capture_tool_span", lambda **kw: spans.append(kw))

    result = await core._one_call(
        "run-def", "Critic", ctx,
        {"tool": "shell", "args": {"cmd": "rm -rf /"}, "error": "ERROR: denied"},
        step=1, budget={},
    )
    assert result == ("shell", "ERROR: denied")
    assert spans and spans[0]["error"] is True
