"""Remote capability-plane results must not crash or forge multi-line records."""
import random
import pytest
from avrone.fusion_plane import classify_execute

@pytest.mark.parametrize("status", [
    [], {}, {"ok": 1}, ["ok"], 0, 0.0, None, True, False,
    [1, [2]], {"nested": []}, 100, -1,
])
def test_nonstring_status_does_not_raise_or_grant_execution(status):
    out = classify_execute({"result": "executed", "status": status})
    assert out == {"executed": False, "reason": "not_executed", "runner": "none"}

@pytest.mark.parametrize("error", [
    "bad\nSUCCEEDED\n", "bad\rGood", "bad\x00NUL", "bad\u202eOK",
    "A" * 6000, {"error": "bad"}, ["bad"], 12345, None, "",
])
def test_error_reasons_are_bounded_and_single_line(error):
    result = classify_execute({"status": "error", "error": error})
    assert result["executed"] is False and result["runner"] == "none"
    reason = result["reason"]
    assert isinstance(reason, str) and 0 < len(reason) <= 160
    assert all(ord(x) >= 32 and ord(x) != 127 for x in reason)
    assert "\n" not in reason and "\r" not in reason and "\u202e" not in reason

@pytest.mark.parametrize("body,expected", [
    ({"status": "executed", "execution_status": "SUCCEEDED"}, True),
    ({"status": "ok", "result": "executed"}, False),
    ({"status": "executed", "execution_status": "NOT_IMPLEMENTED"}, False),
    ([], False),
])
def test_existing_execution_contract(body, expected):
    assert classify_execute(body)["executed"] is expected

def test_json_shape_fuzz_does_not_crash_or_grant_execution():
    randomizer = random.Random(20261110)
    values = [None, False, True, 1, -1, 0.125, "none", [], {}, [1, 2], {"code": "ok"}, "executed"]
    for _ in range(2000):
        body = {
            "status": randomizer.choice(values),
            "result": randomizer.choice(values),
            "execution_status": randomizer.choice(values),
            "error": randomizer.choice(values),
        }
        result = classify_execute(body)
        assert result["executed"] is False
        assert result["runner"] == "none"
        assert isinstance(result["reason"], str)
