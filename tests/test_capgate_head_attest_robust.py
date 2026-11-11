"""Hardening round 46: capgate_head_attest (the attested-history bridge from daemon main).
Its log was trusted past the digest check: seq was never validated, duplicate keys and NaN
constants parsed, NaN could be written, and its own selftest failed."""
from __future__ import annotations

import json
import os
import threading

import pytest

from avrone import capgate
from avrone import capgate_head_attest as cha


@pytest.fixture
def env(tmp_path, monkeypatch):
    log = tmp_path / "attested" / "history.jsonl"
    monkeypatch.setenv("AVRONE_ATTESTED_HISTORY_LOG", str(log))
    monkeypatch.setenv("AVRONE_CAPGATE_KEY", "test-key")
    monkeypatch.delenv("AVRONE_FLEET_TOKEN", raising=False)
    dd = tmp_path / "data"
    dd.mkdir()
    return dd, log


def _entry(seq, prev, **over):
    e = {"seq": seq, "ts": 1.0, "source": "x", "kind": "k", "digest": "", "provenance_depth": 1,
         "residual": {}, "capability_id": None, "human_root": None, "raw": {}, "prev_hash": prev}
    e.update(over)
    e["digest"] = cha._digest({k: v for k, v in e.items() if k != "digest"})
    return e


def _write(log, entries, raw_lines=()):
    log.parent.mkdir(parents=True, exist_ok=True)
    body = b"".join(cha._canon(e) + b"\n" for e in entries) + b"".join(raw_lines)
    log.write_bytes(body)


# ------------------------------------------------------------ selftest
def test_module_selftest_passes(capsys):
    saved = dict(os.environ)
    try:
        assert cha._selftest() == 0
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert "0 failed" in capsys.readouterr().out


# ------------------------------------------------------------ happy path
def test_chain_of_appends(env):
    dd, log = env
    es = [cha.attest_head(dd, note=f"n{i}") for i in range(5)]
    assert [e["seq"] for e in es] == list(range(5))
    for a, b in zip(es, es[1:]):
        assert b["prev_hash"] == a["digest"]
    for ln in log.read_bytes().splitlines():
        json.loads(ln, parse_constant=cha._no_constants)


def test_lines_are_canonical_bytes(env):
    dd, log = env
    e = cha.attest_head(dd)
    assert log.read_bytes() == cha._canon(e) + b"\n"


# ------------------------------------------------------------ seq validation
@pytest.mark.parametrize("bad", ["0", "x", True, False, 0.0, 1.5, None, [0], {"a": 0}, -1, 1, 10**20])
def test_bad_first_seq_fails_closed(env, bad):
    dd, log = env
    _write(log, [_entry(bad, None)])
    with pytest.raises(OSError, match="seq"):
        cha.attest_head(dd)


def test_missing_seq_fails_closed(env):
    dd, log = env
    e = _entry(0, None)
    del e["seq"]
    e["digest"] = cha._digest({k: v for k, v in e.items() if k != "digest"})
    _write(log, [e])
    with pytest.raises(OSError, match="seq"):
        cha.attest_head(dd)


@pytest.mark.parametrize("second", [0, 2, 5, -1])
def test_skipped_or_repeated_seq_fails_closed(env, second):
    dd, log = env
    a = _entry(0, None)
    _write(log, [a, _entry(second, a["digest"])])
    with pytest.raises(OSError, match="seq"):
        cha.attest_head(dd)


def test_infinite_seq_line_is_unreadable_not_overflow(env):
    dd, log = env
    a = _entry(0, None)
    line = cha._canon(a).replace(b'"seq":0', b'"seq":Infinity')
    _write(log, [], [line + b"\n"])
    with pytest.raises(OSError):
        cha.attest_head(dd)


def test_valid_hand_built_chain_continues(env):
    dd, log = env
    a = _entry(0, None)
    b = _entry(1, a["digest"])
    _write(log, [a, b])
    e = cha.attest_head(dd)
    assert e["seq"] == 2 and e["prev_hash"] == b["digest"]


# ------------------------------------------------------------ strict JSON
def test_duplicate_key_line_refused(env):
    dd, log = env
    a = _entry(0, None)
    line = cha._canon(a)
    forged = line[:-1] + b',"kind":"something-else"}'
    _write(log, [], [forged + b"\n"])
    with pytest.raises(OSError, match="unreadable"):
        cha.attest_head(dd)


@pytest.mark.parametrize("const", [b"NaN", b"Infinity", b"-Infinity"])
def test_nan_constants_refused(env, const):
    dd, log = env
    a = _entry(0, None)
    line = cha._canon(a).replace(b'"ts":1.0', b'"ts":' + const)
    _write(log, [], [line + b"\n"])
    with pytest.raises(OSError):
        cha.attest_head(dd)


def test_canon_refuses_nan():
    with pytest.raises(ValueError):
        cha._canon({"x": float("nan")})


def test_nested_duplicate_keys_refused():
    with pytest.raises(ValueError):
        cha._strict_loads(b'{"a":{"b":1,"b":2}}')


@pytest.mark.parametrize("junk", [b"\xff\xfe\n", b"[1,2]\n", b'"str"\n', b"null\n", b"[" * 100000 + b"\n"])
def test_junk_lines_fail_closed(env, junk):
    dd, log = env
    _write(log, [], [junk])
    with pytest.raises(OSError):
        cha.attest_head(dd)


def test_blank_lines_tolerated(env):
    dd, log = env
    a = _entry(0, None)
    _write(log, [], [b"\n", cha._canon(a) + b"\n", b"   \n"])
    assert cha.attest_head(dd)["seq"] == 1


def test_crlf_lines_tolerated(env):
    dd, log = env
    a = _entry(0, None)
    _write(log, [], [cha._canon(a) + b"\r\n"])
    assert cha.attest_head(dd)["seq"] == 1


# ------------------------------------------------------------ torn tails
@pytest.mark.parametrize("tail", [b'{"seq":1,"tor', b"\x00" * 64, b"\xff", b"{"])
def test_torn_tail_healed_and_kept(env, tail):
    dd, log = env
    a = _entry(0, None)
    _write(log, [a], [tail])
    e = cha.attest_head(dd)
    assert e["seq"] == 1
    assert tail in (log.parent / "history.jsonl.torn").read_bytes()
    assert log.read_bytes().count(b"\n") == 2


def test_torn_sidecar_symlink_refused(env, tmp_path):
    dd, log = env
    a = _entry(0, None)
    _write(log, [a], [b"{"])
    target = tmp_path / "victim"
    target.write_bytes(b"keep")
    os.symlink(target, str(log) + ".torn")
    with pytest.raises(OSError):
        cha.attest_head(dd)
    assert target.read_bytes() == b"keep"


# ------------------------------------------------------------ notes and errors
@pytest.mark.parametrize("note", ["\ud800", "a\udfffb", b"\xff\xfe", 12345, None, "", "x" * 10000,
                                  "line\nbreak\x00nul", "\u202eevil", "\U0001f600" * 300])
def test_odd_notes_are_written_as_valid_utf8_json(env, note):
    dd, log = env
    e = cha.attest_head(dd, note=note)
    assert isinstance(e["raw"]["note"], str) and len(e["raw"]["note"]) <= 200
    text = log.read_bytes().decode("utf-8")  # strict decode
    rec = json.loads(text)
    rec["raw"]["note"].encode("utf-8")  # no lone surrogate


def test_emoji_note_keeps_pairs():
    assert cha._short("\U0001f600" * 3, "") == "\U0001f600" * 3


def test_verify_error_non_string_is_text(env, monkeypatch):
    dd, log = env
    monkeypatch.setattr(capgate, "verify_chain", lambda d: {"ok": False, "count": 1, "error": {"x": float("nan")}})
    e = cha.attest_head(dd)
    assert isinstance(e["raw"]["verify_error"], str)
    json.loads(log.read_bytes(), parse_constant=cha._no_constants)


def test_verify_error_missing_named(env, monkeypatch):
    dd, log = env
    monkeypatch.setattr(capgate, "verify_chain", lambda d: {"ok": False})
    assert cha.attest_head(dd)["raw"]["verify_error"] == "unknown"


# ------------------------------------------------------------ files that are not files
def test_log_symlink_refused(env, tmp_path):
    dd, log = env
    log.parent.mkdir(parents=True)
    target = tmp_path / "t"
    target.write_text("")
    os.symlink(target, log)
    with pytest.raises(OSError):
        cha.attest_head(dd)
    assert target.read_text() == ""


def test_dangling_log_symlink_refused(env, tmp_path):
    dd, log = env
    log.parent.mkdir(parents=True)
    os.symlink(tmp_path / "nowhere", log)
    with pytest.raises(OSError):
        cha.attest_head(dd)
    assert not (tmp_path / "nowhere").exists()


def test_lock_symlink_refused(env, tmp_path):
    dd, log = env
    log.parent.mkdir(parents=True)
    os.symlink(tmp_path / "lk", str(log) + ".lock")
    with pytest.raises(OSError):
        cha.attest_head(dd)


def test_lock_fifo_refused(env):
    dd, log = env
    log.parent.mkdir(parents=True)
    os.mkfifo(str(log) + ".lock")
    with pytest.raises(OSError):
        cha.attest_head(dd)


def test_log_directory_refused(env):
    dd, log = env
    log.mkdir(parents=True)
    with pytest.raises(OSError):
        cha.attest_head(dd)


def test_oversize_log_refused(env, monkeypatch):
    dd, log = env
    monkeypatch.setattr(cha, "MAX_LOG_BYTES", 100)
    _write(log, [_entry(0, None)])
    with pytest.raises(OSError, match="over"):
        cha.attest_head(dd)


# ------------------------------------------------------------ concurrency
def test_threads_append_one_unbroken_chain(env):
    dd, log = env
    errs = []

    def go():
        try:
            for _ in range(5):
                cha.attest_head(dd)
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs
    lines = log.read_bytes().splitlines()
    assert [json.loads(ln)["seq"] for ln in lines] == list(range(30))
    assert cha._last_good(log)["seq"] == 29


# ------------------------------------------------------------ env switch and CLI
@pytest.mark.parametrize("v,on", [("on", True), (" ON ", True), ("1", True), ("yes", True), ("true", True),
                                  ("off", False), ("", False), ("0", False), ("enable", False), ("ｏｎ", False)])
def test_enabled(monkeypatch, v, on):
    monkeypatch.setenv("AVRONE_CAPGATE_ATTEST", v)
    assert cha.enabled() is on


def test_main_refused_log_returns_1(env, monkeypatch, capsys):
    dd, log = env
    monkeypatch.setenv("AVRONE_CAPGATE_ATTEST", "on")
    log.mkdir(parents=True)
    assert cha.main([str(dd)]) == 1
    assert "refused" in capsys.readouterr().err


def test_main_off_is_noop(env, monkeypatch):
    dd, log = env
    monkeypatch.delenv("AVRONE_CAPGATE_ATTEST", raising=False)
    assert cha.main([str(dd)]) == 0 and not log.exists()


def test_unmeasured_without_shared_key(env, monkeypatch):
    dd, log = env
    monkeypatch.delenv("AVRONE_CAPGATE_KEY", raising=False)
    monkeypatch.setenv("AVRONE_FLEET_TOKEN", "   ")
    e = cha.attest_head(dd)
    assert e["residual"]["orientation"] == "unmeasured" and e["residual"]["S"] is None
