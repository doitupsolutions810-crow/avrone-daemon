"""The integration merge keeps strict provenance/access checks and newer replay contracts."""
import pytest

from avrone.evidence_pool import pool_trials
from avrone.fusion_loopback import is_access_loopback, origin_key
from avrone.fusion_plane import classify_execute


def test_safe_canonicalization_cannot_manufacture_independent_sources():
    result = pool_trials([
        {"source": "Relay", "trial_id": "one", "failed": False},
        {"source": "\uff52elay\u200b", "trial_id": "two", "failed": True},
    ])
    assert result["runs"] == 2
    assert len(result["sources"]) == 1
    assert result["orientation_conflict"] is False
    assert result["independence"] == "not_assessed"


@pytest.mark.parametrize("character", ["\n", "\t", "\u202e", "\u2066"])
def test_unsafe_provenance_is_refused_even_before_canonical_pooling(character):
    with pytest.raises(ValueError, match="control|bidi"):
        pool_trials([{"source": "relay" + character, "trial_id": "one", "failed": False}])


def test_scheme_port_and_reserved_hostname_buckets_do_not_collide():
    urls = ["http://127.0.0.1:8090", "https://127.0.0.1:8090",
            "http://loopback:8090", "https://loopback:8090"]
    assert len({origin_key(url) for url in urls}) == len(urls)
    assert origin_key("http://localhost") == origin_key("http://127.0.0.1:80")
    assert origin_key("https://localhost") == origin_key("https://[::1]:443")


@pytest.mark.parametrize("host", [" localhost", "localhost\t", "[[::1]]", "127.0.0.1%lo"])
def test_stricter_peer_spelling_contract_survives_merge(host):
    assert is_access_loopback(host) is False


class HostileEquality:
    def __eq__(self, other):
        raise RuntimeError("external comparison must not execute")


def test_hostile_error_cannot_turn_success_or_logging_into_an_exception():
    error = HostileEquality()
    succeeded = classify_execute({"status": "executed", "execution_status": "SUCCEEDED", "error": error})
    assert succeeded == {"executed": False, "reason": "contradictory", "runner": "none"}
    failed = classify_execute({"status": "error", "error": error})
    assert failed == {"executed": False, "reason": "error", "runner": "none", "detail": "HostileEquality"}
