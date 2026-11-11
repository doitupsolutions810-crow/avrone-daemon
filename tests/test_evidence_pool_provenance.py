"""Provenance IDs must not embed control or bidirectional-format characters."""
import random
import pytest
from avrone.evidence_pool import pool_trials

CONTROL = ["\n", "\r", "\0", "\t", "\x7f", "\x85", "\u2028", "\u2029",
           "\u200e", "\u200f", "\u061c", "\u202e", "\u2066", "\u2069"]

@pytest.mark.parametrize("character", CONTROL)
@pytest.mark.parametrize("field", ["source", "trial_id"])
def test_reject_provenance_control_and_bidi(character, field):
    record = {"source": "node-a", "trial_id": "node-01", "failed": False}
    record[field] += character + "FORGED"
    with pytest.raises(ValueError, match="control|bidi"):
        pool_trials([record])

def test_valid_namespaces_and_emoji_unaffected():
    result = pool_trials([
        {"source": " node🔬 ", "trial_id": " test-001 ", "failed": False},
        {"source": "node🔬", "trial_id": "test-002", "failed": True},
    ])
    assert result["runs"] == 2 and result["failures"] == 1
    assert result["sources"][0]["source"] == "node🔬"

def test_seeded_control_mutation_never_crosses_boundary():
    rng = random.Random(48022)
    for i in range(1000):
        field = rng.choice(["source", "trial_id"])
        record = {"source": "node", "trial_id": "trial", "failed": bool(i % 2)}
        record[field] = f"{i}{rng.choice(CONTROL)}route"
        with pytest.raises(ValueError, match="control|bidi"):
            pool_trials([record])
