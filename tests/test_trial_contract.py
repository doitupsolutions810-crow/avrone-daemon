import copy

import pytest

from avrone.trial_contract import (
    MAX_SOURCE_COMPONENT_CHARS,
    canonical_config_bytes,
    make_trial,
    pool_record,
    validate_trial_envelope,
)


def sample(**overrides):
    values = dict(
        system="grokbot", node="node-a", run_id="run-17", check_id="bridge-health",
        check_version="v3", attempt_id="attempt-1", outcome="pass",
        check_config={"python": "3.12", "profile": "strict"},
    )
    values.update(overrides)
    return make_trial(**values)


def test_identity_is_deterministic_and_outcome_is_not_identity():
    passed = sample(outcome="pass")
    failed = sample(outcome="fail")
    skipped = sample(outcome="skipped")
    assert passed["trial_id"] == failed["trial_id"] == skipped["trial_id"]
    assert validate_trial_envelope(passed)
    assert pool_record(passed)["failed"] is False
    assert pool_record(failed)["failed"] is True
    with pytest.raises(ValueError, match="non-pass"):
        pool_record(skipped)


def test_source_check_version_attempt_and_config_are_preserved_in_identity():
    base = sample()
    assert base["source"] == "grokbot:node-a"
    assert base["check_version"] == "v3"
    assert base["trial_id"] != sample(check_version="v4")["trial_id"]
    assert base["trial_id"] != sample(attempt_id="attempt-2")["trial_id"]
    assert base["trial_id"] != sample(check_config={"python": "3.13"})["trial_id"]


def test_unicode_identity_and_config_are_nfc_canonicalized():
    composed = sample(node="caf\u00e9", check_config={"label": "caf\u00e9"})
    decomposed = sample(node="cafe\u0301", check_config={"label": "cafe\u0301"})
    assert composed["trial_id"] == decomposed["trial_id"]
    assert composed["source"] == decomposed["source"]
    assert canonical_config_bytes({"e\u0301": "o\u0308"}) == canonical_config_bytes(
        {"\u00e9": "\u00f6"}
    )


def test_config_rejects_normalized_key_collisions_and_non_json_values():
    with pytest.raises(ValueError, match="duplicate keys"):
        canonical_config_bytes({"\u00e9": 1, "e\u0301": 2})
    with pytest.raises(ValueError, match="finite"):
        canonical_config_bytes({"bad": float("nan")})
    with pytest.raises(ValueError, match="JSON"):
        canonical_config_bytes({"bad": object()})


def test_config_rejects_oversize_depth_and_node_count():
    with pytest.raises(ValueError, match="exceeds"):
        canonical_config_bytes({"payload": "x" * 20_000})
    deep = []
    current = deep
    for _ in range(34):
        child = []
        current.append(child)
        current = child
    with pytest.raises(ValueError, match="depth"):
        canonical_config_bytes(deep)
    with pytest.raises(ValueError, match="values"):
        canonical_config_bytes(list(range(5_000)))


@pytest.mark.parametrize(
    "field,value",
    [
        ("system", ""), ("node", " "), ("run_id", 3), ("check_id", ""),
        ("check_version", "v" * 257), ("attempt_id", "bad\nvalue"),
        ("system", "grok:bot"), ("node", "worker:1"),
        ("outcome", "unknown"), ("outcome", True),
    ],
)
def test_identity_and_outcome_fields_are_bounded_and_typed(field, value):
    with pytest.raises(ValueError):
        sample(**{field: value})


def test_source_components_are_bounded_to_pool_source_limit():
    record = sample(
        system="s" * MAX_SOURCE_COMPONENT_CHARS,
        node="n" * MAX_SOURCE_COMPONENT_CHARS,
    )
    assert len(record["source"]) == 127
    assert validate_trial_envelope(record)


def test_validation_rejects_extra_or_tampered_fields():
    record = sample()
    assert validate_trial_envelope(record)
    extra = copy.deepcopy(record)
    extra["untrusted"] = True
    assert not validate_trial_envelope(extra)
    bad = copy.deepcopy(record)
    bad["trial_id"] = "0" * 64
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record)
    bad["check_version"] = "tampered"
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record)
    bad["source"] = "forged:node"
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record)
    bad["node"] = "cafe\u0301"
    assert not validate_trial_envelope(bad)

def test_config_whitespace_is_preserved_as_semantic_data():
    assert canonical_config_bytes({"label": " x "}) != canonical_config_bytes({"label": "x"})
    assert canonical_config_bytes({" x ": 1}) != canonical_config_bytes({"x": 1})
    assert sample(check_config={"label": " x "})["trial_id"] != sample(
        check_config={"label": "x"}
    )["trial_id"]


def test_config_strings_reject_control_characters_and_lone_surrogates():
    with pytest.raises(ValueError, match="control characters"):
        canonical_config_bytes({"label": "line\nbreak"})
    with pytest.raises(ValueError, match="valid Unicode"):
        canonical_config_bytes({"label": "\ud800"})
