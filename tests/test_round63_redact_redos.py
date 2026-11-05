"""Round 63: ReDoS in the secret redactor that every CodeX event passes through.

A scan of all 68 module-level regexes against repeated adversarial units
(/workspace/work/regexscan.py) found catastrophic backtracking in ctxspawn.redact, which
codex._scrub runs on EVERY string of EVERY event before it is logged or sent to /ws/events,
on the event loop (tool.result output up to 4000 chars, tool.call args unclipped):

- the key=value rule `[\\w.-]*SECRET[\\w.-]*\\s*[:=]...` on "a.a.a..." (minified JS, dotted
  versions, paths): 4 KB 0.7 s, 8 KB 2.8 s, 16 KB 11 s, 50 KB 99 s. Replaced by _redact_kv:
  identifiers tokenised once, linear (50 KB: ~4 ms). It keeps its slot in _PATTERNS (an object with
  the same .search/.sub surface), so secret_scan's per-rule names still line up.
- the URL-credential rules `\\b[a-z][a-z0-9+.\\-]*://` (ctxspawn, doctor, tools_extra,
  tools_introspect, tools_researchnote): ~0.36 s per 8 KB, quadratic. Scheme now {0,31} and
  unanchored, so "<40 letters>http://u:p@h" is still redacted (it was, by the unbounded scan).
"""
from __future__ import annotations

import random
import re
import time

import pytest

from avrone import ctxspawn, doctor, tools_extra, tools_introspect, tools_researchnote
from avrone.ctxspawn import REDACTED, _SECRET_WORD, redact

OLD_KV = re.compile(r"(?i)([\"']?\b[\w.-]*" + _SECRET_WORD + r"[\w.-]*[\"']?\s*[:=]\s*)(?!\[REDACTED\])"
                    r"(\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)")


def _old_kv(s: str) -> str:
    return OLD_KV.sub(lambda m: m.group(1) + REDACTED, s)


@pytest.mark.parametrize("unit", ["a.", "a-", "0.", "token", "tokena", "a_key", "token.", "x.y-"])
def test_redact_is_fast_on_long_word_runs(unit):
    s = unit * (50_000 // len(unit))
    t = time.monotonic()
    redact(s)
    assert time.monotonic() - t < 1.0


@pytest.mark.parametrize("seed", range(8))
def test_kv_rule_matches_the_old_regex(seed):
    rnd = random.Random(seed)
    pieces = ["token", "api_key", "password", "a", ".", "-", "_", "=", ":", " ", '"', "'", "x1",
              "secret", "tokenizer", ",", "}", "]", ";", "[REDACTED]", "\t", "my", "aws_key"]
    for _ in range(1500):
        s = "".join(rnd.choice(pieces) for _ in range(rnd.randint(0, 18)))
        assert ctxspawn._redact_kv(s) == _old_kv(s), repr(s)


def test_kv_examples():
    s = 'config: {"api_key": "sk-abc", password=hunter22, my.secret.value: 9, tokenizer=bert}'
    assert redact(s) == ('config: {"api_key": [REDACTED], password=[REDACTED], '
                         "my.secret.value: [REDACTED], tokenizer=[REDACTED]}")
    assert redact("token=[REDACTED] again") == "token=[REDACTED] again"
    assert redact("no secrets here: a.b.c = 1") == "no secrets here: a.b.c = 1"


def test_kv_rule_keeps_its_slot_for_secret_scan():
    rule = ctxspawn._PATTERNS[3][0]
    assert rule.search("password=abcdefgh1")
    assert not rule.search("nothing to see")
    assert rule.sub(None, "x token: y") == "x token: [REDACTED]"


@pytest.mark.parametrize("pat", [
    ctxspawn._PATTERNS[10][0], doctor._USERINFO_RE, tools_extra._URL_USERINFO_RE,
    tools_introspect._URL_USERINFO_RE, tools_researchnote._TOKEN_RES[5],
], ids=["ctxspawn", "doctor", "tools_extra", "introspect", "researchnote"])
def test_url_userinfo_regexes_are_linear(pat):
    for unit in ("a.", "a-", "a+"):
        s = unit * 25_000
        t = time.monotonic()
        pat.sub("", s)
        assert time.monotonic() - t < 0.2, unit


def test_url_credentials_still_redacted():
    assert redact("see https://user:pw@host/x") == "see https://***@host/x"
    long_prefix = "a" * 40 + "http://u:p@h"
    assert "u:p@" not in redact(long_prefix)
    assert redact("git+ssh://me:tok@github.com/x").startswith("git+ssh://***@")
    assert doctor._USERINFO_RE.sub(r"\1***@", "postgres://u:p@db") == "postgres://***@db"


def test_codex_emit_is_fast_on_minified_output(tmp_path):
    from avrone.codex import Codex
    cx = Codex(tmp_path)
    blob = "a.b-c.d" * 3000  # 21 KB of minified-JS-ish text
    t = time.monotonic()
    cx.emit("r63", "tool.call", "A", tool="write_file", args={"content": blob})
    cx.emit("r63", "tool.result", "A", tool="fetch", output=blob[:4000])
    assert time.monotonic() - t < 1.0
    assert len(cx.replay("r63")) == 2
