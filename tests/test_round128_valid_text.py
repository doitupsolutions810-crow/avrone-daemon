"""Round 128: every tool answer is valid text, whatever the call carried.

v0.120 (probes /workspace/work/surleak.py with an encode check, nestfuzz.py): answers that echo
their input kept a lone UTF-16 surrogate: "ERROR: no loop a\\ud800b" (evolve_status, evolve_cancel,
evolve_start resume_loop_id) and "ERROR: unknown tool 'shell\\ud800'". That text then went to the
transcript, codex, fleet replies and the next model request, none of which can encode it as UTF-8.
ToolRegistry.call and check() now pass every answer through tools.valid_text.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from avrone import tools
from avrone.trinity import AgentContext
from tests.test_round114_deep_paths import _core

S = "\ud800"


@pytest.fixture
def ctx():
    core = _core(Path(tempfile.mkdtemp()))
    return AgentContext(core=core, run_id="r", name="A", depth=0)


def _call(ctx, name, args):
    return asyncio.run(ctx.core.tools.call(ctx, name, args))


def _encodes(text):
    text.encode("utf-8")
    json.dumps({"t": text}, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize("tool,args", [
    ("evolve_status", {"loop_id": "a" + S + "b"}),
    ("evolve_cancel", {"loop_id": "a" + S + "b"}),
    ("evolve_start", {"resume_loop_id": "a" + S + "b"}),
    (S, {}),
    ("shell" + S, {}),
])
def test_answers_echoing_input_encode(ctx, tool, args):
    out = _call(ctx, tool, args)
    _encodes(out)
    assert "\\ud800" in out  # the character is shown escaped, not dropped


def test_check_messages_encode(ctx):
    r = ctx.core.tools.check("nope" + S, {})
    assert r and r[0] == "unknown_tool"
    _encodes(r[1])


def test_plugin_tool_returning_surrogates(ctx):
    async def bad(c, a):
        return "x" + S + "y" + "\udfff"
    ctx.core.tools.register(tools.Tool("r128_bad", "returns lone surrogates", "{}", bad, source="plugin:t"))
    out = _call(ctx, "r128_bad", {})
    assert out == "x\\ud800y\\udfff"


def test_valid_text_unit():
    vt = getattr(tools, "valid_text", None) or pytest.fail("no tools.valid_text")
    plain = "héllo 世界 🙂"
    assert vt(plain) is plain
    assert vt("a\ud83db") == "a\\ud83db"
    assert vt("\U0001F642") == "\U0001F642"  # a real astral char is untouched
    assert vt(5) == "5"


def test_valid_text_large_input_fast():
    import time
    vt = getattr(tools, "valid_text", None) or pytest.fail("no tools.valid_text")
    big = "x" * 2_000_000
    t = time.monotonic()
    assert vt(big) is big
    assert time.monotonic() - t < 1.0
