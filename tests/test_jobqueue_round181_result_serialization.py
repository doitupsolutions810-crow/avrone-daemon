"""Round 181: result serialization remains bounded, valid JSON, and secret-safe.

Exercise unsupported objects, hostile string conversion, cyclic containers, and
large Unicode output through the public result encoder.
"""
from __future__ import annotations

import json

from avrone.jobqueue import RESULT_MAX, _encode_result


def _token() -> str:
    return "ghp_" + "B" * 28


class HostileString:
    def __str__(self):
        raise RuntimeError("str must not escape result encoding")

    def __repr__(self):
        raise RuntimeError("repr must not escape result encoding")


class SecretString:
    def __str__(self):
        return "upstream credential " + _token()


def test_hostile_str_and_repr_still_produce_valid_json():
    encoded = _encode_result(HostileString())
    decoded = json.loads(encoded)
    assert "unprintable" in decoded
    assert "RuntimeError" not in encoded


def test_fallback_stringification_redacts_secret_material():
    encoded = _encode_result(SecretString())
    decoded = json.loads(encoded)
    assert _token() not in encoded
    assert "[REDACTED]" in decoded


def test_cyclic_result_is_serialized_without_recursion_escape():
    cyclic = []
    cyclic.append(cyclic)
    encoded = _encode_result(cyclic)
    decoded = json.loads(encoded)
    assert "OMITTED: result nesting limit" in encoded
    depth = 0
    while isinstance(decoded, list) and len(decoded) == 1:
        decoded = decoded[0]
        depth += 1
        assert depth <= 65
    assert decoded == "[OMITTED: result nesting limit]"


def test_oversized_result_is_truncated_to_valid_json():
    # Separate the credential from the payload: when concatenated directly, the
    # redactor correctly treats the whole token-like run as one secret value.
    value = ("x" * (RESULT_MAX + 4096)) + " " + _token()
    encoded = _encode_result(value)
    decoded = json.loads(encoded)
    assert decoded["truncated"] is True
    assert decoded["bytes"] > RESULT_MAX
    assert len(encoded) <= RESULT_MAX + 256
    assert _token() not in encoded


def test_unicode_result_truncation_preserves_json_validity():
    value = ("界🙂" * (RESULT_MAX // 2)) + _token()
    encoded = _encode_result(value)
    decoded = json.loads(encoded)
    assert decoded["truncated"] is True
    assert _token() not in encoded
