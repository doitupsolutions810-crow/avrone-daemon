"""Round 111: absurdly nested JSON is bad JSON everywhere, never a RecursionError.

v0.103: json.loads/raw_decode raise RecursionError (not ValueError) past the recursion limit, and
several `except ValueError` sites let it escape:
- trinity.parse_action on a model reply: the whole agent run failed;
- gateway._parse_args on tool-call arguments from the model;
- trinity.normalize_call on string args;
- jobqueue._decode on a stored payload/result column;
- capgate.verify_chain on a hand-edited <receipts>.head;
- tools_statehealth._check_jsonl on a state file line.
"""
from __future__ import annotations

import json

import pytest

from avrone import capgate, gateway, jobqueue, safeio, tools_statehealth, trinity

DEEP_OBJ = '{"a":' * 100000 + "1" + "}" * 100000
DEEP_ARR = "[" * 200000 + "]" * 200000


def test_safeio_json_loads_is_decode_error():
    with pytest.raises(json.JSONDecodeError):
        safeio.json_loads(DEEP_ARR)
    assert safeio.json_loads('{"a": [1]}') == {"a": [1]}


@pytest.mark.parametrize("text", [DEEP_OBJ, 'Sure: {"tool": "x", "args": ' + DEEP_OBJ + "}"])
def test_parse_action_deep_reply(text):
    assert trinity.parse_action(text) is None


def test_parse_action_finds_action_after_deep_junk():
    got = trinity.parse_action('{"a":' * 5000 + "1" + "}" * 5000 + ' {"final": "ok"}')
    assert got == {"final": "ok"}


def test_parse_args_deep_is_incomplete():
    args, complete = gateway._parse_args(DEEP_OBJ)
    assert complete is False and isinstance(args, dict)


def test_parse_args_normal_unchanged():
    assert gateway._parse_args('{"cmd": "ls"}') == ({"cmd": "ls"}, True)
    assert gateway._parse_args('"{\\"cmd\\": \\"ls\\"}"') == ({"cmd": "ls"}, True)


def test_normalize_call_deep_string_args():
    try:
        out = trinity.normalize_call(None, {"tool": "x", "args": DEEP_OBJ})
    except RecursionError:
        pytest.fail("normalize_call raised RecursionError on deep string args")
    except Exception:  # noqa: BLE001 - any ordinary refusal is fine
        return
    assert out is None or isinstance(out, (dict, list, str))


def test_jobqueue_decode_deep_marks_corrupt():
    out = jobqueue._decode(DEEP_ARR)
    assert isinstance(out, dict) and out.get("corrupt") is True


def test_capgate_verify_chain_deep_head(tmp_path):
    p = capgate.receipts_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("")
    capgate._head_path(p).write_text("[" * 4000)
    try:
        res = capgate.verify_chain(tmp_path)
    except RecursionError:
        pytest.fail("verify_chain raised RecursionError on a deep head file")
    assert isinstance(res, dict)


def test_state_health_deep_jsonl_line(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"ok": 1}\n' + DEEP_ARR + "\n")
    assert tools_statehealth._check_jsonl(p) == "1 undecodable line(s)"
