from __future__ import annotations

import copy
import random
import unicodedata

import pytest

from avrone.integration_contract import (
    EvidenceContractError,
    MAX_PROVENANCE_DEPTH,
    MAX_REF,
    build_evidence_event,
    verify_evidence_event,
)


def make(**overrides):
    args = dict(source="security-suite", run_id="security-run", failures=1, runs=37)
    args.update(overrides)
    return build_evidence_event(**args)


@pytest.mark.parametrize("digest", [
    "💥", "é" * 64, "a" * 63, "a" * 65, "A" * 64, "g" * 64, "", None, 123, b"0" * 64,
])
def test_malformed_digest_is_rejected_without_raising(digest):
    record = make()
    record["digest"] = digest
    ok, reason = verify_evidence_event(record)
    assert ok is False
    assert reason


def test_depth_upper_boundary_is_accepted_and_next_value_rejected():
    record = make(provenance_depth=MAX_PROVENANCE_DEPTH, human_root="operator:ref")
    assert verify_evidence_event(record) == (True, "")
    with pytest.raises(EvidenceContractError):
        make(provenance_depth=MAX_PROVENANCE_DEPTH + 1, human_root="operator:ref")


# Use actual Unicode code points, not strings containing the literal characters
# backslash-u. Every value below is tested through every identifier channel.
_UNSAFE_CODEPOINTS = (
    *range(0x00, 0x20),                 # C0 controls
    *range(0x7F, 0xA0),                 # DEL and C1 controls
    0x061C, 0x200E, 0x200F,             # Arabic letter mark / LRM / RLM
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E,  # bidi embeddings, overrides, pop
    0x2066, 0x2067, 0x2068, 0x2069,     # bidi isolates and PDI
    0x200B, 0x200C, 0x200D,             # zero-width space, non-joiner, joiner
    0xFEFF,                              # BOM / zero-width no-break space
    0xD800, 0xDBFF, 0xDC00, 0xDFFF,     # high and low surrogate boundaries
    0x2028, 0x2029,                     # Unicode line and paragraph separators
)


@pytest.mark.parametrize(
    "unsafe",
    [pytest.param(chr(codepoint), id=f"U+{codepoint:04X}") for codepoint in _UNSAFE_CODEPOINTS],
)
@pytest.mark.parametrize("field", ["source", "run_id", "check_id", "capability_id", "human_root", "refs"])
def test_machine_identifiers_reject_controls_bidi_zero_width_and_separators(field, unsafe):
    value = f"prefix{unsafe}suffix"
    kwargs = {field: [value] if field == "refs" else value}
    if field == "human_root":
        kwargs["provenance_depth"] = 1
    with pytest.raises(EvidenceContractError):
        make(**kwargs)


def test_exact_maximum_human_root_length_is_accepted():
    record = make(human_root="x" * MAX_REF, provenance_depth=1)
    assert verify_evidence_event(record) == (True, "")


@pytest.mark.parametrize("depth", [True, False, -1, 1.0, "2", None, 10**100])
def test_invalid_provenance_depths_are_rejected(depth):
    with pytest.raises(EvidenceContractError):
        make(provenance_depth=depth)


def test_unicode_identifiers_round_trip_but_unicode_digest_does_not():
    record = make(source="sourcé", run_id="rún-⚛")
    assert verify_evidence_event(record) == (True, "")
    decomposed = unicodedata.normalize("NFD", "sourcé")
    with pytest.raises(EvidenceContractError, match="NFC"):
        make(source=decomposed)
    forged = copy.deepcopy(record)
    forged["source"] = decomposed
    from avrone.integration_contract import _canonical
    import hashlib
    forged["digest"] = hashlib.sha256(
        _canonical({key: value for key, value in forged.items() if key != "digest"})
    ).hexdigest()
    assert verify_evidence_event(forged)[0] is False
    record["digest"] = "é" * 64
    assert verify_evidence_event(record)[0] is False


@pytest.mark.parametrize("field", ["source", "run_id", "check_id", "capability_id", "human_root", "refs"])
def test_every_identifier_channel_rejects_non_nfc_text(field):
    decomposed = unicodedata.normalize("NFD", "café")
    kwargs = {field: [decomposed] if field == "refs" else decomposed}
    if field == "human_root":
        kwargs["provenance_depth"] = 1
    with pytest.raises(EvidenceContractError, match="NFC"):
        make(**kwargs)


def test_deeply_nested_untrusted_summary_is_rejected_cleanly():
    record = make()
    nested = []
    for _ in range(2000):
        nested = [nested]
    record["evidence"]["untrusted"] = nested
    ok, reason = verify_evidence_event(record)
    assert ok is False
    assert reason


def test_large_extra_event_field_is_rejected_before_digest_work():
    record = make()
    record["unexpected"] = "x" * 2_000_000
    assert verify_evidence_event(record) == (False, "event fields do not match schema v1")


def test_seeded_measurement_fuzz_always_round_trips():
    rng = random.Random(20261105)
    for _ in range(300):
        runs = rng.randint(1, 100_000)
        failures = rng.randint(0, runs)
        record = make(failures=failures, runs=runs)
        assert verify_evidence_event(record) == (True, "")
        assert record["evidence"]["orientation"] == (
            "pass" if failures == 0 else "fail" if failures == runs else "mixed"
        )


@pytest.mark.parametrize("field,value", [
    ("source", "forged-source"),
    ("run_id", "forged-run"),
    ("check_id", "forged-check"),
    ("refs", ["forged-ref"]),
    ("capability_id", "forged-capability"),
    ("human_root", "forged-root"),
    ("provenance_depth", 1),
])
def test_identity_mutation_without_digest_refresh_is_rejected(field, value):
    record = make()
    original_digest = record["digest"]
    changed = copy.deepcopy(record)
    changed[field] = value
    assert changed["digest"] == original_digest
    assert verify_evidence_event(changed)[0] is False


def test_canonical_digest_golden_vector_is_stable_across_implementations():
    record = build_evidence_event(
        source="golden",
        run_id="golden-1",
        check_id="check-1",
        failures=1,
        runs=4,
    )
    assert record["digest"] == "756e0fe4737fb0820345ea265960740f4ec9c44df8eaaf8ebac8d81aa8886b5e"
    assert verify_evidence_event(record) == (True, "")


def test_maximum_supported_trial_count_remains_finite_and_verifiable():
    record = make(failures=1, runs=1_000_000_000)
    assert verify_evidence_event(record) == (True, "")
    assert 0 < record["evidence"]["detectInRuns"] < 1
    assert record["evidence"]["runsFor95"] > 1_000_000_000
