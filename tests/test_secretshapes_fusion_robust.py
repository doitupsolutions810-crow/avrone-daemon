"""Hardening round 57: secretshapes / redaction on raw JSON lines, and fusion_deliver edges.

- pgpass rule: quadratic backtracking (a 40,000-char line took 6.8 s, 160,000 took 104 s; its
  gate is ":" so it ran on nearly every event), and CRLF .pgpass lines leaked.
- (?i) context rules were gated case-sensitively: COOKIE:, "Machine host password x", "Auth",
  convertto-securestring and Client-Key-Data skipped their rule.
- quoted CLI values (--password "x", mysql -p'x', curl -u "u:p", sshpass -p 'x') leaked.
- redact() on a raw JSON line ate closing quotes and escapes (key=value rule, Authorization
  rule), so the line stopped being JSON and the next key was masked.
- fusion_deliver: retry_delay overflowed after 1,024 attempts; NaN/Infinity, lone surrogates and
  non-string keys in a payload raised or went out as invalid JSON.
"""
from __future__ import annotations

import json
import random
import re
import time

import pytest

from avrone import ctxspawn as c
from avrone import fusion_deliver as f
from avrone import secretshapes as S
from avrone.ctxspawn import redact

PW = "hunter2pass"
OLD_PGPASS = re.compile(r"(?m)^([^\s:]+:(?:\d{1,5}|\*):[^\s:]+:[^\s:]*[g-zG-Z_*][^\s:]*:)(?:\\:|[^\s:])+$")


def _rule(name):
    return next(r for r in S.CONTEXT_RULES if r[3] == name)


def _ungated(text: str) -> str:
    s = str(text)
    for v in c._env_secret_values():
        s = s.replace(v, c.REDACTED)
    for pat, rep in c._PATTERNS:
        s = pat.sub(rep, s)
    return s


# ---------------------------------------------------------------- pgpass
@pytest.mark.parametrize("n", [40_000, 400_000])
def test_pgpass_rule_is_linear(n):
    rx, rep, _g, _n = _rule("pgpass")
    t = time.perf_counter()
    rx.sub(rep, "a:1:b:" + "g" * n + ":")
    rx.sub(rep, "a:1:b:" + "0" * n)
    redact("db:5432:x:" + "g" * n)
    assert time.perf_counter() - t < 1.0


def test_pgpass_same_as_before_on_lf_text():
    rx, rep, _g, _n = _rule("pgpass")
    rnd = random.Random(57)
    frags = ["a", ":", "1", "*", "g", "Z", "_", "\\", "\\:", " ", "\n", "0", "f", "x", "12345", "\t", "db"]
    for _ in range(20_000):
        s = "".join(rnd.choice(frags) for _ in range(rnd.randint(1, 20)))
        assert rx.sub(rep, s) == OLD_PGPASS.sub(rep, s), repr(s)


def test_pgpass_crlf_line_masked():
    out = redact(f"localhost:5432:app:alice:{PW}\r\nnext:5432:db:bob:{PW}\n")
    assert PW not in out
    assert out == "localhost:5432:app:alice:[REDACTED]\r\nnext:5432:db:bob:[REDACTED]\n"


def test_pgpass_ipv6_and_times_untouched():
    for s in ["fe80:0:1:abcd:ef01", "12:30:45:00:11"]:
        assert redact(s) == s


# ---------------------------------------------------------------- case-insensitive gates
@pytest.mark.parametrize("text", [
    f"COOKIE: session={PW}", f"CooKIE: sid={PW}", f"SET-COOKIE: sid={PW}",
    f"Machine db.example.com login u password {PW}", f"MACHINE db.example.com password {PW}",
    f"Default login u password {PW}",
    '{"Auth": "' + "dXNlcjp" + PW + '"}', '{"IdentityToken": "' + PW + 'abc"}', '{"AUTH": "' + PW + 'zz"}',
    f"convertto-securestring '{PW}' -asplaintext", f"CONVERTTO-SECURESTRING \"{PW}\" -AsPlainText",
    f"Client-Key-Data: {PW}", f"CLIENT-KEY-DATA: {PW}",
])
def test_mixed_case_context_secrets_masked(text):
    assert PW not in redact(text), redact(text)


def test_folded_gates_are_lower_case_and_registered():
    for i, (rx, _rep, gate, name) in enumerate(S.CONTEXT_RULES):
        if S.folded_gate(rx):
            assert all(w == w.lower() for w in gate), name
            assert 12 + i in c._FOLDED_ANY
        else:
            assert 12 + i not in c._FOLDED_ANY


@pytest.mark.parametrize("seed", [1, 2])
def test_gated_redact_matches_every_rule_with_mixed_case(seed):
    frags = ["COOKIE: ", "Cookie:", "set-COOKIE: ", "Machine ", "MACHINE ", "Default ", "db.example.com ",
             "password ", "login ", '"Auth": "', '"IDENTITYTOKEN": "', "abcdefghij", '"', "convertTo-SecureString ",
             "'", "Client-Key-Data: ", "x", " ", "\n", "=", "?KEY=", "&", "--PASSWORD ", "<Password>", "</Password>",
             "Pwd=", ";", "HTTPS://", "0123456789abcdef0123456789abcdef", "@", "\u212a", "\u0130"]
    rnd = random.Random(seed)
    for _ in range(4000):
        s = "".join(rnd.choice(frags) for _ in range(rnd.randint(1, 14)))
        assert redact(s) == _ungated(s), repr(s)


# ---------------------------------------------------------------- quoted CLI values
@pytest.mark.parametrize("text,want", [
    (f'tool --password "{PW}"', 'tool --password "[REDACTED]"'),
    (f"tool --password '{PW} with space'", "tool --password '[REDACTED]'"),
    (f"tool --db-password\t{PW}", "tool --db-password\t[REDACTED]"),
    (f"mysql -u root -p'{PW}' db", "mysql -u root -p'[REDACTED]' db"),
    (f'mysql -p"{PW}"', 'mysql -p"[REDACTED]"'),
    (f"curl -u 'bob:{PW}' x", "curl -u 'bob:[REDACTED]' x"),
    (f'curl -u "bob:{PW} 2" x', 'curl -u "bob:[REDACTED]" x'),
    (f"sshpass -p '{PW}' ssh h", "sshpass -p '[REDACTED]' ssh h"),
    (f'sshpass -p"{PW}" ssh', 'sshpass -p"[REDACTED]" ssh'),
    (f'--password "{PW}', '--password "[REDACTED]'),
])
def test_quoted_cli_values_masked(text, want):
    assert redact(text) == want
    line = json.dumps({"cmd": text})
    out = redact(line)
    assert PW not in out
    assert json.loads(out)["cmd"] == want


@pytest.mark.parametrize("text", ['--password "" x', 'mysql -p"" db', "--password -x", "ls -a --all",
                                  "rsync a b --password-file f"])
def test_quoted_empty_or_flag_untouched(text):
    assert redact(text) == text


def test_json_escaped_flag_separator():
    line = json.dumps({"o": f"--token\t{PW}\n--secret-key\n{PW}"})
    out = redact(line)
    assert PW not in out
    json.loads(out)


# ---------------------------------------------------------------- raw JSON lines stay JSON
@pytest.mark.parametrize("line", [
    '{"cmd": "x token=abc", "n": 1}',
    '{"cmd": "token=abc\\nnext"}',
    '{"a": "token=", "b": "c"}',
    '{"cmd":"token=","b":"c"}',
    '{"cmd": "tool --api-key="}',
    '{"cmd": "x password= ", "n": 1}',
    '{"a": "token=\\"abc\\""}',
    '{"token": "x\\" password: abc", "n": 1}',
    '{"a": "Authorization: Bearer abcdefghijk\\"}"}',
    '{"a": "sshpass -p\\n", "b": "x"}',
    '{"a": "mysql -p\\n", "b": "x"}',
    '{"a": "--password \\"", "b": "x"}',
    '{"tokens": ["Zq8vR2mXk4LpT7wN9cYb"]}',
])
def test_redacted_json_line_is_still_json(line):
    obj = json.loads(line)
    out = redact(line)
    back = json.loads(out)
    assert set(back) == set(obj)


def test_empty_values_and_next_key_untouched():
    assert redact('{"a": "token=", "b": "c"}') == '{"a": "token=", "b": "c"}'
    assert redact('{"cmd": "x token=abc", "n": 1}') == '{"cmd": "x token=[REDACTED]", "n": 1}'
    assert redact('{"tokens": ["Zq8vR2mXk4LpT7wN9cYb"]}') == '{"tokens": ["[REDACTED]"]}'


def test_unquoted_value_keeps_lone_backslash_masked():
    assert redact("password=ab\\cd") == "password=[REDACTED]"
    assert redact('password="hunter2') == 'password="[REDACTED]'


@pytest.mark.parametrize("seed", range(4))
def test_fuzzed_json_lines_stay_json(seed):
    frags = ["token=", "password: ", " ", "x", "=", "abc", ",", '"', "'", "\n", "\t", "\\", 'api_key="q" ',
             "secret=>", "{", "}", "Authorization: Bearer abcdefghijk", "sk_live_" + "a" * 20, " --password ",
             "mysql -p", "curl -u bob:pw ", '-u "', "-u '", "sshpass -p", "cookie: a=b", "<password>x</password>",
             "?key=abc&", "machine a.b password x", "h:1:d:u:pw", '"auth": "abcdefghij"', "https://u:p@h/",
             "--token\t", "Private-Lines: 1\nAAAA\n", "[", "]"]
    rnd = random.Random(seed)
    for _ in range(6000):
        obj = {rnd.choice(["a", "cmd", "token", "b"]): "".join(rnd.choice(frags) for _ in range(rnd.randint(0, 6)))
               for _ in range(3)}
        line = json.dumps(obj)
        out = redact(line)
        assert set(json.loads(out)) == set(obj), (line, out)


def test_hostile_quote_runs_are_fast():
    t = time.perf_counter()
    for big in ['-u "' * 100_000, '--password "' * 50_000, "--password '" + "a " * 100_000, 'mysql -p"' * 30_000,
                "sshpass -p'" * 50_000, "--token\\t" * 100_000, '--password "' + "a" * 300_000,
                'token=\\"' * 80_000, 'token="' + 'a\\"' * 100_000, "COOKIE:" * 100_000, "Machine " * 100_000]:
        redact(big)
    assert time.perf_counter() - t < 4.0


# ---------------------------------------------------------------- fusion_deliver
def test_retry_delay_never_overflows():
    cfg = f.config_from_env()
    for streak in (1, 64, 1024, 1025, 5000, 10**9):
        d = f.retry_delay("status429", streak, cfg)
        assert 0 < d <= max(cfg.backoff_max, cfg.backoff_base, f.MIN_RETRY_DELAY)
    assert f.retry_delay("status5xx", 10**6, cfg) == f.retry_delay("status5xx", 2000, cfg)


def _event(**kw):
    base = {"id": "e1", "source": "avrone", "kind": "finding", "run_id": "r1", "payload": {}}
    base.update(kw)
    return base


@pytest.mark.parametrize("payload,word", [
    ({"x": float("nan")}, "non-finite"), ({"x": [float("inf")]}, "non-finite"),
    ({"y": {"z": float("-inf")}}, "non-finite"), ({"t": "a\ud800"}, "surrogate"), ({"\udfff": 1}, "surrogate"),
    ({1: 2}, "keys"), ({"z": {1: 2, "a": 3}}, "keys"), ({"x": {1, 2}}, "set"), ({"x": object()}, "object"),
])
def test_payload_that_is_not_strict_json_is_refused(payload, word):
    ev, err = f.prepare_event(_event(payload=payload))
    assert ev is None and word in err


def test_surrogate_run_id_is_refused_not_raised():
    ev, err = f.prepare_event(_event(run_id="r\ud800"))
    assert ev is None and "surrogate" in err


def test_good_payload_still_prepared_and_strict():
    ev, err = f.prepare_event(_event(payload={"heat": 1.5, "note": "hi \u00e9", "l": [1, "a", None, True]}))
    assert err is None
    json.loads(f._wire_bytes(ev).decode("utf-8"), parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))


def test_deep_payload_still_too_big_not_crash():
    deep: list = []
    cur = deep
    for _ in range(5000):
        nxt: list = []
        cur.append(nxt)
        cur = nxt
    ev, err = f.prepare_event(_event(payload={"x": deep}))
    assert ev is None and err
