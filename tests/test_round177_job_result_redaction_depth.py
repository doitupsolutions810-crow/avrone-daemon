"""Round 177: job-result redaction must not leak secrets past its depth limit."""
from __future__ import annotations

import json

from avrone.jobqueue import _encode_result


def _token() -> str:
    return "ghp_" + "A" * 28


def test_secret_string_at_depth_65_is_still_redacted():
    value = _token()
    for _ in range(65):
        value = [value]
    encoded = _encode_result(value)
    assert _token() not in encoded
    assert "[REDACTED]" in encoded
    json.loads(encoded)


def test_uninspected_container_beyond_depth_limit_is_omitted():
    value = {"credential": _token()}
    for _ in range(66):
        value = {"next": value}
    encoded = _encode_result(value)
    assert _token() not in encoded
    assert "OMITTED: result nesting limit" in encoded
    json.loads(encoded)


def test_secret_shaped_dictionary_keys_are_redacted():
    encoded = _encode_result({_token(): "value"})
    assert _token() not in encoded
    assert "[REDACTED]" in encoded
    json.loads(encoded)


def test_nested_secrets_within_supported_depth_are_redacted():
    payload = {"outer": [{"inner": {"access_token": _token()}}]}
    encoded = _encode_result(payload)
    assert _token() not in encoded
    assert "[REDACTED]" in encoded
    json.loads(encoded)
