"""Round 15: parse_action against the reply shapes local models (gpt-oss, qwen) really produce.
The old parser used one greedy {.*} regex, so any brace in prose or a second object broke it."""
import time

import pytest

from avrone.trinity import parse_action

SHELL_LS = {"tool": "shell", "args": {"cmd": "ls"}}


@pytest.mark.parametrize("text,want", [
    # prose braces around the action
    ('I will run {curly} then {"tool":"shell","args":{"cmd":"ls"}}', SHELL_LS),
    ('{"tool":"shell","args":{"cmd":"ls"}} done {ok}', SHELL_LS),
    # the model repeats itself: take the first action, not nothing
    ('{"tool":"shell","args":{"cmd":"ls"}}\n{"tool":"shell","args":{"cmd":"ls"}}', SHELL_LS),
    ('{"tool":"a"} {"tool":"b"}', {"tool": "a"}),
    # a non-action object first (an example of args), then the action
    ('args look like {"cmd": "x"}; action: {"tool":"shell","args":{"cmd":"ls"}}', SHELL_LS),
    # truncated outer object still holding a complete action
    ('{"plan": 1, "next": {"tool":"shell","args":{"cmd":"ls"}}', SHELL_LS),
    # fence language tags other than json
    ('```jsonc\n{"final":"c"}\n```', {"final": "c"}),
    ('```json5\n{"final":"c"}\n```', {"final": "c"}),
    ('```python\nd = {1: 2}\n```\n{"final":"f"}', {"final": "f"}),
    # fence whose block has prose before the object
    ('```\nAction:\n{"tool":"shell","args":{"cmd":"ls"}}\n```', SHELL_LS),
])
def test_prose_and_multiple_objects(text, want):
    assert parse_action(text) == want


def test_think_block_json_loses_to_the_answer():
    assert parse_action('<think>maybe {"tool":"rm"}</think> ok {"tool":"ls"}') == {"tool": "ls"}
    assert parse_action('<THINKING>{"tool":"rm"}</THINKING>{"final":"x"}') == {"final": "x"}


def test_unclosed_think_block_still_parses_as_last_resort():
    assert parse_action('<think>{"tool":"only"}') == {"tool": "only"}


def test_harmony_tokens_leaked_by_the_server():
    t = ('<|start|>assistant<|channel|>analysis<|message|>maybe {"tool":"bad"}<|end|>'
         '<|start|>assistant<|channel|>final<|message|>{"tool":"good"}<|return|>')
    assert parse_action(t) == {"tool": "good"}
    t2 = '<|channel|>commentary to=functions.shell<|message|>{"tool":"shell","args":{"cmd":"ls"}}<|call|>'
    assert parse_action(t2) == SHELL_LS


@pytest.mark.parametrize("bad", [None, 42, b'{"tool":"x"}', ["x"]])
def test_non_text_is_none(bad):
    assert parse_action(bad) is None


@pytest.mark.parametrize("text", [
    "{" * 200_000,
    '{"a":' * 5000 + "1",
    '{"x":[' * 3000 + "]",
    '{"a":1}' * 100_000,
    "} {" * 100_000,
])
def test_pathological_replies_stay_fast(text):
    t0 = time.monotonic()
    assert parse_action(text) is None
    assert time.monotonic() - t0 < 2.0


def test_action_after_a_huge_string_value():
    assert parse_action('{"k":"' + "a" * 1_000_000 + '"} {"tool":"shell"}') == {"tool": "shell"}


def test_deep_non_action_object_does_not_hide_later_action():
    deep = '{"outer":' + '{"a":' * 3000 + "0" + "}" * 3000 + '}'
    assert parse_action(deep + ' {"tool":"shell"}') == {"tool": "shell"}


def test_deep_object_scanner_ignores_braces_inside_escaped_strings():
    from avrone.trinity import _skip_balanced_object

    text = '{"x":"escaped \\\" { } \\\\"} {"tool":"shell"}'
    end = _skip_balanced_object(text, 0)
    assert text[end:].strip() == '{"tool":"shell"}'
