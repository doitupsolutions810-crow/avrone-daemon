from __future__ import annotations

import json

import pytest

from avrone.fleet import MAX_STRICT_JSON_DEPTH, strict_json_loads


@pytest.mark.parametrize(
    "raw",
    [
        b'{"top":1,"top":2}',
        b'{"outer":{"inner":1,"inner":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e999}',
        b'{"unterminated":"value}',
        b'{"wrong":[1,2}}',
        b'{"value":"x"}\xff',
    ],
)
def test_strict_json_rejects_ambiguous_or_nonportable_documents(raw):
    with pytest.raises((ValueError, UnicodeDecodeError)):
        strict_json_loads(raw)


def test_strict_json_rejects_depth_over_limit_and_accepts_exact_limit():
    too_deep = b"[" * (MAX_STRICT_JSON_DEPTH + 1) + b"0" + b"]" * (MAX_STRICT_JSON_DEPTH + 1)
    accepted = b"[" * MAX_STRICT_JSON_DEPTH + b"0" + b"]" * MAX_STRICT_JSON_DEPTH
    with pytest.raises(ValueError, match="depth"):
        strict_json_loads(too_deep)
    parsed = strict_json_loads(accepted)
    for _ in range(MAX_STRICT_JSON_DEPTH):
        parsed = parsed[0]
    assert parsed == 0


def test_strict_json_does_not_count_braces_inside_escaped_strings():
    sample_text = 'braces [ { and escaped quote " plus slash ' + chr(92)
    raw = json.dumps(
        {"text": sample_text, "items": [1, 2]},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    parsed = strict_json_loads(raw)
    assert parsed["items"] == [1, 2]
    assert parsed["text"] == sample_text
