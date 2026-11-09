"""Round 130: a model "final" holding a non-finite number renders as valid text.

v0.122 (probe /workspace/work/llmfuzz.py final_num/final_nan): {"final": 1e999} answered "Infinity"
and {"final": {"x": NaN}} answered '{"x": NaN}', which is not JSON. Non-finite numbers now read
"inf", "-inf" or "nan", and structured finals stay valid JSON.
"""
from __future__ import annotations

import json
import math

import pytest

from avrone import trinity


def _norm(obj):
    reg = None
    fn = getattr(trinity, "normalize_reply", None) or getattr(trinity, "_normalize", None)
    if fn is None:
        pytest.skip("no normalizer")
    return fn(reg, obj)


def test_helper_exists():
    getattr(trinity, "_finite_json", None) or pytest.fail("no _finite_json")


@pytest.mark.parametrize("v,want", [(math.inf, "inf"), (-math.inf, "-inf"), (math.nan, "nan")])
def test_scalar(v, want):
    fj = getattr(trinity, "_finite_json", None) or pytest.fail("no _finite_json")
    assert fj(v) == want


def test_nested_dumps_strict():
    fj = getattr(trinity, "_finite_json", None) or pytest.fail("no _finite_json")
    out = json.dumps(fj({"a": [1.5, math.nan, {"b": -math.inf}], "c": "x"}), allow_nan=False)
    assert json.loads(out) == {"a": [1.5, "nan", {"b": "-inf"}], "c": "x"}


@pytest.mark.parametrize("raw,want", [('{"final": 1e999}', "inf"), ('{"final": -1e999}', "-inf"),
                                      ('{"final": NaN}', "nan"), ('{"final": 42}', "42"),
                                      ('{"final": {"x": NaN}}', '{"x": "nan"}'), ('{"final": "ok"}', "ok")])
def test_normalize_action(raw, want):
    out = trinity.normalize_action(None, json.loads(raw))
    assert out["final"] == want
    json.loads(json.dumps(out, allow_nan=False))
