"""Cross-check trial source identity against the canonical evidence-pool rules."""
import pytest

from avrone.evidence_pool import canonical_source_key, pool_trials


def trial(source, trial_id, failed=False):
    return {"source": source, "trial_id": trial_id, "failed": failed}


def test_source_key_collapses_nfkc_case_and_format_character_aliases():
    assert canonical_source_key("GROKBOT") == canonical_source_key("ＧｒｏｋＢｏｔ\u200b")
    assert canonical_source_key("  CycleKernel\tNode-A  ") == "cyclekernel node-a"


def test_pool_groups_compatibility_aliases_into_one_source():
    result = pool_trials([
        trial("GROKBOT", "trial-1", False),
        trial("ＧｒｏｋＢｏｔ\u200b", "trial-2", True),
    ])
    assert result["runs"] == 2
    assert len(result["sources"]) == 1
    assert result["sources"][0]["runs"] == 2
    assert result["independence"] == "not_assessed"


@pytest.mark.parametrize("source", ["", " " * 2, "x" * 600, "bad\ud800"])
def test_source_key_rejects_empty_oversized_or_invalid_unicode(source):
    with pytest.raises(ValueError):
        canonical_source_key(source)
