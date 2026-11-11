"""Round 161: the key=value rule keeps quotes and spares values that can't be secrets.

`max_tokens=4096`, `"prompt_tokens": 12` and `"token_count": 3` were masked in every model
usage line (the key holds "token"); `password: null` and `secret=""` were masked too; and a
masked quoted value lost its quotes, so a redacted JSON line stopped being JSON.
"""
from __future__ import annotations

import json

import pytest

from avrone.ctxspawn import redact

KEEP = ["max_tokens=4096", '{"max_tokens": 4096, "temperature": 0.2}', '"token_count": 12',
        '"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}',
        "password: true", "api_key: null", 'secret=""', '"token": ""', "num_tokens=3.5"]


@pytest.mark.parametrize("text", KEEP)
def test_not_secret_values_kept(text):
    assert redact(text) == text


MASK = ["password=123456", "access_token=12345", "token=abc123def", "token: 1234567890123",
        "api_key=99", "auth_token: 4242"]


@pytest.mark.parametrize("text", MASK)
def test_secret_values_still_masked(text):
    assert "[REDACTED]" in redact(text)


def test_quoted_value_keeps_quotes_and_json_parses():
    line = json.dumps({"password": "hunter2xx", "user": "bob", "usage": {"max_tokens": 64}})
    out = redact(line)
    assert json.loads(out) == {"password": "[REDACTED]", "user": "bob", "usage": {"max_tokens": 64}}
    assert redact("api_key='abc-def'") == "api_key='[REDACTED]'"
