"""Round 16: actions that parse as JSON but miss AVRONE's format the way local models do.
Before this, each of these either ended the run early ("None"), or became an ARG ERROR turn."""
import asyncio
import json

import pytest

from avrone.codex import Codex
from avrone.config import Settings
from avrone.sovereign import LocalRunner
from avrone.trinity import TrinityCore, normalize_action, normalize_call


class Reg:
    def __init__(self, names): self.names = set(names)
    def get(self, n): return object() if n in self.names else None


R = Reg({"shell", "write_file"})


@pytest.mark.parametrize("call,tool", [
    ({"tool": "functions.shell"}, "shell"),
    ({"tool": "tools.shell"}, "shell"),
    ({"tool": " shell "}, "shell"),
    ({"tool": "functions.unknown"}, "functions.unknown"),   # unknown stays as sent
    ({"tool": "shell"}, "shell"),
])
def test_tool_prefixes(call, tool):
    assert normalize_call(R, call)["tool"] == tool


@pytest.mark.parametrize("key", ["arguments", "parameters", "params", "input"])
def test_args_under_another_key(key):
    out = normalize_call(R, {"tool": "shell", key: {"cmd": "ls"}})
    assert out["args"] == {"cmd": "ls"} and key not in out


def test_args_key_wins_over_alias():
    out = normalize_call(R, {"tool": "shell", "args": {"cmd": "a"}, "arguments": {"cmd": "b"}})
    assert out["args"] == {"cmd": "a"}


@pytest.mark.parametrize("raw,want", [
    ('{"cmd": "ls"}', {"cmd": "ls"}),
    ('  {"cmd": "ls"}', {"cmd": "ls"}),
    ('{"cmd": broken', '{"cmd": broken'),      # undecodable stays for the arg check
    ('["ls"]', '["ls"]'),
    ("ls", "ls"),
])
def test_string_args(raw, want):
    assert normalize_call(R, {"tool": "shell", "args": raw})["args"] == want
    assert normalize_call(R, {"tool": "shell", "arguments": raw})["args"] == want


def test_input_not_mutated_and_non_dict_passthrough():
    c = {"tool": "functions.shell", "arguments": '{"cmd":"ls"}'}
    snap = json.dumps(c)
    normalize_call(R, c)
    assert json.dumps(c) == snap
    assert normalize_call(R, "x") == "x" and normalize_call(R, None) is None
    assert normalize_call(None, {"tool": "functions.shell"})["tool"] == "functions.shell"


@pytest.mark.parametrize("final", [None, "", "   "])
def test_empty_final_beside_a_tool_is_dropped(final):
    out = normalize_action(R, {"tool": "shell", "args": {"cmd": "ls"}, "final": final})
    assert "final" not in out


def test_real_final_beside_a_tool_still_wins():
    assert normalize_action(R, {"tool": "shell", "final": "done"})["final"] == "done"


def test_final_without_tool_kept_and_structured_final_is_json():
    assert normalize_action(R, {"tool": None, "final": "both"})["final"] == "both"
    assert normalize_action(R, {"final": {"a": 1, "é": [1]}})["final"] == '{"a": 1, "é": [1]}'
    assert normalize_action(R, {"final": 3})["final"] == "3"


def test_batch_entries_normalized():
    out = normalize_action(R, {"tools": [{"tool": "functions.shell", "arguments": '{"cmd":"ls"}'}, 7]})
    assert out["tools"] == [{"tool": "shell", "args": {"cmd": "ls"}}, 7]


# --- through the real agent loop ------------------------------------------------------------

class ScriptLLM:
    def __init__(self, replies): self.replies = list(replies); self.seen = []
    async def chat(self, messages, **kw):
        self.seen.append(messages[-1]["content"])
        return self.replies.pop(0) if self.replies else '{"final": "ran out"}'


def _core(tmp_path, replies):
    s = Settings(); s.data_dir = tmp_path / "d"; s.workspace = tmp_path / "ws"; s.max_steps = 6
    llm = ScriptLLM(replies)
    return TrinityCore(s, llm, LocalRunner(s), Codex(s.data_dir)), llm


@pytest.mark.parametrize("first", [
    '{"tool": "shell", "args": {"cmd": "echo hi-avrone"}, "final": null}',
    '{"tool": "functions.shell", "arguments": "{\\"cmd\\": \\"echo hi-avrone\\"}"}',
    '{"tool": "shell", "parameters": {"cmd": "echo hi-avrone"}}',
    '{"name": "shell", "arguments": {"cmd": "echo hi-avrone"}}',
    '{"function": {"name": "shell", "arguments": {"cmd": "echo hi-avrone"}}}',
    '<tool_call>\n{"name": "shell", "arguments": {"cmd": "echo hi-avrone"}}\n</tool_call>',
])
def test_loop_runs_the_tool(tmp_path, first):
    core, llm = _core(tmp_path, [first, '{"final": "ok"}'])
    out = asyncio.run(core.run_agent("r1", "Worker", "say hi"))
    assert out == "ok"
    assert "hi-avrone" in llm.seen[-1]           # the tool really ran and its output came back
    kinds = [e["kind"] for e in core.codex.replay("r1")]
    assert "tool.arg_error" not in kinds


def test_loop_null_final_alone_is_empty_not_None(tmp_path):
    core, _ = _core(tmp_path, ['{"final": null}'])
    assert asyncio.run(core.run_agent("r1", "Worker", "x")) == ""
