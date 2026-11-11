"""Hardening round 43: evidence_pool identity canonicalisation (NFC/NFD, NFKC, invisible chars,
case-insensitive sources), control characters, hostile mappings and odd inputs."""
from __future__ import annotations

import json

import pytest

from avrone import evidence_pool as ep


def T(src, tid, failed=False):
    return {"source": src, "trial_id": tid, "failed": failed}


# ------------------------------------------------------------ duplicate relays in disguise
@pytest.mark.parametrize("a,b", [
    ("trial-\u00e9", "trial-e\u0301"),          # NFC vs NFD
    ("t1", "t\u200b1"),                          # zero-width space
    ("t1", "\ufefft1"),                          # BOM
    ("t1", "t1\u200d"),                          # zero-width joiner
    ("t1", "t\u00ad1"),                          # soft hyphen
    ("t1", "t\u200e1"),                          # LRM
    ("t1", "\u202et1"),                          # RLO
    ("t1", "t\uff11"),                           # fullwidth digit
    ("ffi-1", "\ufb03-1"),                       # ligature
    ("a b", "a\u00a0b"),                         # no-break space
    ("a b", "a\u3000b"),                         # ideographic space
    ("a b", "a  \t b"),                          # whitespace run
    ("t1", "  t1  "),
])
def test_disguised_duplicate_ids_rejected(a, b):
    with pytest.raises(ValueError, match="duplicate trial_id"):
        ep.pool_trials([T("n", a), T("n", b)])


def test_distinct_ids_still_distinct():
    r = ep.pool_trials([T("n", "t1"), T("n", "t2"), T("n", "T1")])
    assert r["runs"] == 3  # ids stay case-sensitive


# ------------------------------------------------------------ sources
@pytest.mark.parametrize("variant", ["NODE-A", "Node-A", "node-a\u200b", " node-a ", "node\u2011a".replace("\u2011", "-"),
                                     "\uff4eode-a"])
def test_source_variants_merge(variant):
    r = ep.pool_trials([T("node-a", "1", False), T(variant, "2", True)])
    assert len(r["sources"]) == 1
    row = r["sources"][0]
    assert row["source"] == "node-a" and row["runs"] == 2 and row["orientation"] == "mixed"
    assert r["orientation_conflict"] is False


def test_fake_orientation_conflict_prevented():
    r = ep.pool_trials([T("Relay", "1", False), T("relay\u200b", "2", True)])
    assert r["orientation_conflict"] is False


def test_first_spelling_reported():
    r = ep.pool_trials([T("Node-A", "1"), T("node-a", "2")])
    assert r["sources"][0]["source"] == "Node-A"


def test_source_cap_counts_canonical(monkeypatch):
    monkeypatch.setattr(ep, "MAX_SOURCES", 2)
    r = ep.pool_trials([T("a", "1"), T("A", "2"), T("a\u200b", "3"), T("b", "4")])
    assert len(r["sources"]) == 2
    with pytest.raises(ValueError, match="sources"):
        ep.pool_trials([T("a", "1"), T("b", "2"), T("c", "3")])


# ------------------------------------------------------------ control characters and surrogates
@pytest.mark.parametrize("bad", ["t\x00", "t\x1b[2J", "t\x7f", "t\x85", "t\u2028", "t\ud800", "\x07"])
@pytest.mark.parametrize("field", ["source", "trial_id"])
def test_controls_rejected(bad, field):
    rec = T("n", "1")
    rec[field] = bad
    with pytest.raises(ValueError, match="control"):
        ep.pool_trials([rec])


def test_internal_newline_tab_collapse_to_space():
    with pytest.raises(ValueError, match="duplicate"):
        ep.pool_trials([T("n", "a\nb"), T("n", "a\tb")])


@pytest.mark.parametrize("empty", ["", "   ", "\u200b", "\ufeff\u200d", "\u00a0"])
def test_invisible_only_ids_rejected(empty):
    with pytest.raises(ValueError, match="trial_id"):
        ep.pool_trials([T("n", empty)])


@pytest.mark.parametrize("empty", ["\u200b", " \u2060 "])
def test_invisible_only_source_rejected(empty):
    with pytest.raises(ValueError, match="source"):
        ep.pool_trials([T(empty, "1")])


# ------------------------------------------------------------ lengths judged on the canonical form
def test_padding_does_not_reject_valid_id():
    tid = " " * 100 + "x" * 256 + " " * 100
    assert ep.pool_trials([T("n", tid)])["runs"] == 1


def test_nfkc_expansion_over_limit_rejected():
    # U+FDFA expands to 18 characters under NFKC
    with pytest.raises(ValueError, match="256"):
        ep.pool_trials([T("n", "\ufdfa" * 20)])


def test_raw_length_hard_bound():
    with pytest.raises(ValueError):
        ep.pool_trials([T("n", "x" * (256 * 4 + 1))])
    with pytest.raises(ValueError):
        ep.pool_trials([T("s" * (128 * 4 + 1), "1")])


def test_source_exactly_at_limit_ok():
    assert ep.pool_trials([T("s" * 128, "1")])["runs"] == 1
    with pytest.raises(ValueError):
        ep.pool_trials([T("s" * 129, "1")])


# ------------------------------------------------------------ error messages
def test_duplicate_message_escaped_and_bounded():
    tid = "a" * 200 + "\u202e"
    with pytest.raises(ValueError) as ei:
        ep.pool_trials([T("n", "x\u00e9" + tid[:50]), T("n", "xe\u0301" + tid[:50])])
    msg = str(ei.value)
    assert "\u202e" not in msg and len(msg) < 120 and msg.isascii()


# ------------------------------------------------------------ odd inputs
@pytest.mark.parametrize("bad", [None, "trials", b"x", {"source": "n", "trial_id": "1", "failed": False}])
def test_non_list_inputs_refused(bad):
    with pytest.raises(ValueError, match="iterable"):
        ep.pool_trials(bad)


def test_generator_ok():
    assert ep.pool_trials(T("n", str(i)) for i in range(5))["runs"] == 5


class Hostile(dict):
    def get(self, k, d=None):
        raise RuntimeError("boom")


def test_hostile_mapping():
    with pytest.raises(ValueError, match="could not be read"):
        ep.pool_trials([Hostile()])


@pytest.mark.parametrize("failed", [1, 0, "true", None, 1.0])
def test_failed_must_be_bool(failed):
    with pytest.raises(ValueError, match="boolean"):
        ep.pool_trials([T("n", "1", failed)])


@pytest.mark.parametrize("v", [1, None, ["x"], b"x"])
def test_non_str_identity(v):
    with pytest.raises(ValueError):
        ep.pool_trials([T(v, "1")])
    with pytest.raises(ValueError):
        ep.pool_trials([T("n", v)])


def test_report_is_json_serialisable():
    r = ep.pool_trials([T("n\u00e9", "1", True), T("m", "2")])
    json.dumps(r)


def test_trial_cap_still_enforced(monkeypatch):
    monkeypatch.setattr(ep, "MAX_POOL_TRIALS", 3)
    with pytest.raises(ValueError, match="hard cap"):
        ep.pool_trials(T("n", str(i)) for i in range(10))


def test_empty_still_unmeasured():
    r = ep.pool_trials([])
    assert r["runs"] == 0 and r["evidence"] is None
