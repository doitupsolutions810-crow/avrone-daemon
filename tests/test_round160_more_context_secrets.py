"""Round 160: more secrets known by context, and the key=value rule no longer skips nested secrets.

A probe of 44 config/CLI forms found 13 that went out raw: .netrc, .pgpass, docker config "auth",
ConvertTo-SecureString, --client-secret / --db-password, kubeconfig client-key-data, PuTTY .ppk
lines, Sentry DSN keys, Pwd= / AccountKey= in connection strings, crypt hashes in htpasswd, and
{"credentials": {"secret": "x"}}, where the key=value rule masked the "{" and the nested secret
leaked. Secret-shaped samples are built at run time from parts.
"""
from __future__ import annotations

import pytest

from avrone.ctxspawn import redact
from avrone.tools_secrets import _kinds

S = "Zq8vR2mXk4" + "LpT7wN9cYb" + "3HdF6sJa1EgU"
B64 = "Ym9iOnNlY3Jl" + "dHBhc3N3b3JkMTIz"
HEX = "a" * 32

LEAKS = {
    "netrc": "machine api.example.com login bob password " + S,
    "netrc-default": "default login bob password " + S,
    "pgpass": "db.example.com:5432:app:bob:" + S,
    "docker-auth": '{"auths":{"x":{"auth":"' + B64 + '"}}}',
    "securestring": 'ConvertTo-SecureString "' + S + '" -AsPlainText -Force',
    "client-secret-flag": "tool --client-secret " + S,
    "db-password-flag": "tool --db-password " + S,
    "kube-client-key": "    client-key-data: " + S,
    "putty": "PuTTY-User-Key-File-3: ssh-ed25519\nPrivate-Lines: 2\n" + S + "\n" + S[::-1] + "\nPrivate-MAC: ab",
    "putty-json": '{"t":"Private-Lines: 1\\n' + S + '\\nPrivate-MAC: ab"}',
    "nested-secret": '{"credentials": {"secret": "' + S + '"}}',
    "nested-list": '{"tokens": ["' + S + '"]}',
    "sentry-dsn": "https://" + HEX + "@o1.ingest.sentry.io/1",
    "pwd": "Pwd=" + S + ";Server=x",
    "azure-key": "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=" + S + "==;",
    "htpasswd": "bob:$apr1$abcd1234$" + S[:22],
}


@pytest.mark.parametrize("name", sorted(LEAKS))
def test_masked_and_flagged(name):
    text = LEAKS[name]
    out = redact(text)
    for part in (S[:16], S[::-1][:16], B64[:12], HEX):
        assert part not in out, (name, out)
    assert _kinds(text), name


KEEP = ["meeting at 2026:11:10:20:39", "fe80:0:abcd:ef01:1", "see --token-file /etc/tok",
        "the password is in the vault", "machine learning password reset", "a:1:b:c:d",
        "connect --api-key-file k.txt", "http://example.com@evil.com", "Private-Lines and stuff"]


@pytest.mark.parametrize("text", KEEP)
def test_plain_text_untouched(text):
    assert redact(text) == text


def test_nested_secret_masked_sibling_kept():
    out = redact('{"credentials": {"secret": "' + S + '", "user": "bob"}}')
    assert S not in out and '"user": "bob"' in out and out.startswith('{"credentials": {')


def test_linear_on_big_input():
    import time
    big = ("a:1:b " * 50000) + ("machine x " * 20000) + ("Private-Lines: 1\n" + "A" * 80 + "\n") * 2000
    t = time.perf_counter()
    redact(big)
    assert time.perf_counter() - t < 5
