"""Round 127: lone UTF-16 surrogates in shell/git commands and in text that cannot be stored.

v0.119 (probe /workspace/work/surleak.py): `shell` and `git` with "\\ud800" in the command answered
"[exit -1] could not start command: UnicodeEncodeError: 'utf-8' codec can't encode ..." and echoed
the raw surrogate back into the transcript; evolve_skill_propose (and any tool whose body hit the
same encode) answered the bare Python codec message. Both now say what is wrong and what to do.
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


@pytest.mark.parametrize("tool,args", [
    ("shell", {"cmd": "echo a" + S + "b"}),
    ("git", {"args": "status " + S}),
])
def test_command_with_surrogate_refused_plainly(ctx, tool, args):
    out = _call(ctx, tool, args)
    assert "lone UTF-16 surrogate" in out, out
    assert "codec" not in out and "UnicodeEncodeError" not in out
    assert S not in out  # nothing un-encodable is echoed back
    out.encode("utf-8")
    json.dumps(out, ensure_ascii=False).encode("utf-8")


def test_surrogate_position_is_in_the_command(ctx):
    out = _call(ctx, "shell", {"cmd": "ab" + S})
    assert "U+D800 at position 2" in out


def test_ordinary_unicode_command_still_runs(ctx):
    out = _call(ctx, "shell", {"cmd": "echo héllo 世界 🙂"})
    assert "héllo 世界 🙂" in out and "refused" not in out


def test_error_text_maps_surrogate_encode_errors():
    try:
        ("a" + S).encode("utf-8")
    except UnicodeEncodeError as e:
        msg = tools.error_text(e)
    assert msg.startswith("ERROR: ")
    assert "lone UTF-16 surrogate (U+D800 at position 1)" in msg
    assert "codec" not in msg


def test_error_text_other_encode_errors_unchanged():
    try:
        "é".encode("ascii")
    except UnicodeEncodeError as e:
        msg = tools.error_text(e)
    assert msg.startswith("ERROR: UnicodeEncodeError:")


def test_evolve_skill_propose_surrogate(ctx):
    out = _call(ctx, "evolve_skill_propose", {"name": "x", "content": "a" + S + "b"})
    assert out.startswith("ERROR:") and "surrogate" in out and "codec" not in out
