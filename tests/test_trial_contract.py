import copy, pytest
from avrone.evidence_pool import pool_trials
from avrone.trial_contract import make_trial, validate_trial_envelope

def sample(**overrides):
    values = dict(system="avrone", node="node-a", run_id="run-17", check_id="security-suite", check_version="3", attempt_id="attempt-1", failed=False, check_config={"python":"3.11","profile":"strict"})
    values.update(overrides)
    return make_trial(**values)

def test_replay_is_idempotent_and_pool_rejects_duplicate():
    a, b = sample(), sample()
    assert a["trial_id"] == b["trial_id"] and validate_trial_envelope(a)
    with pytest.raises(ValueError, match="duplicate trial_id"): pool_trials([a,b])

def test_new_attempt_is_new_observation(): assert sample()["trial_id"] != sample(attempt_id="attempt-2")["trial_id"]

@pytest.mark.parametrize("field,value", [("system",""),("node"," "),("run_id",3),("check_id",""),("check_version","v"*257),("attempt_id","bad\nvalue")])
def test_identity_fields_bounded_and_typed(field,value):
    with pytest.raises(ValueError): sample(**{field:value})

def test_boolean_outcome_is_strict():
    with pytest.raises(ValueError, match="boolean"): sample(failed=1)

def test_outcome_does_not_change_identity_and_conflicting_replay_is_rejected():
    passed, failed = sample(failed=False), sample(failed=True)
    assert passed["trial_id"] == failed["trial_id"]
    with pytest.raises(ValueError, match="duplicate trial_id"): pool_trials([passed,failed])

def test_check_version_and_config_are_part_of_identity():
    base = sample()
    assert base["trial_id"] != sample(check_version="4")["trial_id"]
    assert base["trial_id"] != sample(check_config={"python":"3.12","profile":"strict"})["trial_id"]

def test_config_rejects_nonfinite_and_oversized_values():
    with pytest.raises(ValueError): sample(check_config={"bad":float("nan")})
    with pytest.raises(ValueError, match="exceeds"): sample(check_config={"payload":"x"*20000})

def test_envelope_rejects_extra_or_tampered_fields():
    record = sample()
    assert validate_trial_envelope(record)
    extra = copy.deepcopy(record); extra["untrusted_extra"] = True
    assert not validate_trial_envelope(extra)
    bad = copy.deepcopy(record); bad["trial_id"] = "0" * 64
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record); bad["check_version"] = "tampered"
    assert not validate_trial_envelope(bad)
    bad = copy.deepcopy(record); bad["source"] = "forged:node"
    assert not validate_trial_envelope(bad)

def test_composite_source_accepts_maximum_component_lengths():
    record = sample(system="s" * 256, node="n" * 256)
    assert len(record["source"]) == 513
    assert validate_trial_envelope(record)


def test_unicode_identity_is_canonicalized():
    assert sample(node="caf\u00e9")["trial_id"] == sample(node="cafe\u0301")["trial_id"]
