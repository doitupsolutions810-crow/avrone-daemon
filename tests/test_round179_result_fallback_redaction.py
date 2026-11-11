"""Round 179: fallback JSON serialization must not bypass result redaction."""
from __future__ import annotations

import json

from avrone.jobqueue import _encode_result


def _token() -> str:
    return "ghp_" + "B" * 30


class SecretString:
    def __str__(self):
        return "result=" + _token()


class SecretRepr:
    def __repr__(self):
        return "opaque<" + _token() + ">"


class BrokenStringSecretRepr:
    def __str__(self):
        raise RuntimeError("string conversion failed")

    def __repr__(self):
        return "fallback<" + _token() + ">"


def _assert_redacted(value):
    encoded = _encode_result(value)
    assert _token() not in encoded
    assert "[REDACTED]" in encoded or "unprintable" in encoded
    json.loads(encoded)


def test_default_serializer_redacts_secret_returned_by_str():
    _assert_redacted({"result": SecretString()})


def test_default_serializer_redacts_secret_returned_by_repr():
    _assert_redacted({"result": SecretRepr()})


def test_unprintable_object_does_not_fall_back_to_raw_repr():
    _assert_redacted({"result": BrokenStringSecretRepr()})


def test_repr_fallback_redacts_secret_in_non_json_dictionary_key():
    _assert_redacted({(_token(),): "not-json-key"})
