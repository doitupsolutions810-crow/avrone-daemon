"""Round 82: redaction literal gates.

Every CodeX event string ran all eleven redaction rules (ten regex passes plus the key/value
tokeniser) on the event loop: ~0.9 s per MB of plain text, so the 1,000,000-char field cap from
round 81 still meant a near-second stall. Each rule now runs only when the literals it needs are
present (case-folded the way re.IGNORECASE folds for the case-insensitive rules). Output must be
byte-identical to running every rule.
"""
from __future__ import annotations

import random
import re
import string
import time

import pytest

from avrone import ctxspawn as c


def _ungated(text: str) -> str:
    s = str(text)
    for v in c._env_secret_values():
        s = s.replace(v, c.REDACTED)
    for pat, rep in c._PATTERNS:
        s = pat.sub(rep, s)
    return s


FRAGS = ["sk-", "pk-", "rk-", "ghp_", "ghs_", "github_pat_", "xoxb-", "xoxp-", "AKIA", "AIza", "eyJ", ".",
         "://", "@", ":", "=", '"', "'", " ", "Bearer ", "bEaReR ", "Authorization", "AUTHOR\u0130ZATION",
         "author\u0131zation", "x-api-key", "X-API-KEY", "x-admin-token", "token", "TOKEN", "to\u212aen",
         "secret", "pa\u017fsword", "passwd", "api_key", "apiKey", "private_key", "access-key", "my_key",
         "credential", "-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----",
         "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ", "user:pw", "http://", "\n", ",", ";", "}", "]",
         "[REDACTED]", "\u00e9", "0123456789abcdef0123456789abcdef0123"]


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_gated_redact_matches_every_rule(seed):
    rnd = random.Random(seed)
    for _ in range(8000):
        s = "".join(rnd.choice(FRAGS) for _ in range(rnd.randint(1, 30)))
        assert c.redact(s) == _ungated(s), repr(s)


def test_known_secrets_still_masked():
    for s in ["Authorization: Bearer abcdefghijkl", "AUTHOR\u0130ZATION=xyz123456", "api_key = 'hunter22222'",
              "sk-" + "a" * 20, "ghp_" + "b" * 24, "AKIA" + "C" * 16, "https://u:p@host/x",
              "eyJabcdefghi.abcdefghij.abcdefghij", "-----BEGIN EC PRIVATE KEY-----\nzz",
              "pa\u017fsword: s3cr3tvalue", "xoxb-1234567890-abc"]:
        out = c.redact(s)
        assert "[REDACTED]" in out or "***@" in out, s


def test_fold_table_covers_every_ignorecase_ascii_alias():
    # the four code points re's (?i) matches against an ASCII letter; every other non-ASCII char is
    # covered by str.lower() or cannot match an ASCII literal at all
    for ch in c._FOLD_CHARS:
        assert any(re.fullmatch("(?i)" + l, ch) for l in string.ascii_lowercase)
        assert c._fold(ch) in string.ascii_lowercase
    for cp in list(range(0x80, 0x3000)) + [0xFB00, 0xFB01, 0xFF21, 0xFF41]:
        ch = chr(cp)
        if ch in c._FOLD_CHARS:
            continue
        for l in "aceiklnoprstwxyz_":
            if l.isalpha() and re.fullmatch("(?i)" + l, ch):
                assert ch.lower() == l, hex(cp)


def test_plain_megabyte_is_much_faster_than_ungated():
    s = ("version 1.2.3 at https://example.com/a?q=1 said hello, fox. " * 20000)[:1_000_000]
    t0 = time.perf_counter()
    ref = _ungated(s)
    full = time.perf_counter() - t0
    t0 = time.perf_counter()
    out = c.redact(s)
    gated = time.perf_counter() - t0
    assert out == ref
    assert gated < full / 4


def test_secret_scan_rules_untouched():
    assert len(c._PATTERNS) == 12 and isinstance(c._PATTERNS[3][0], c._KVRule)
    # round 152: + secretshapes.TOKEN_PREFIX_RE as the 12th rule; secret_scan names it "vendor-token"
    from avrone.tools_secrets import _kinds
    assert _kinds("hf_" + "a" * 34) == ["vendor-token"]
