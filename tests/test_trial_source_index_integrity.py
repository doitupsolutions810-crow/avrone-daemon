"""Round 198: the expected index name does not establish unique identity.

An adversarial local SQLite writer can drop the source identity uniqueness
index and replace it with another index of the same name. Detect forged index
definitions at restart, and detect duplicate canonical source/attempt aliases
even if each row's canonical bytes and hash are individually correct.
"""
from __future__ import annotations

import hashlib

import pytest

from avrone.trial_contract import canonical_trial_bytes, make_trial
from avrone.trial_receiver import TrialStore, TrialStoreError, _source_identity_key


def trial(system="grokbot", attempt="198"):
    return make_trial(
        system=system, node="node-a", run_id="run-198",
        check_id="source-uniqueness", check_version="v2",
        attempt_id=attempt, outcome="pass",
        check_config={"profile": "strict"},
    )


def seeded(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    store = TrialStore(path)
    first = trial()
    assert store.ingest_many([first], sender="peer-a", transport="fleet")["accepted"] == 1
    return store, path, first


def mutate_index(store, replacement):
    db = store._connect()
    try:
        db.execute("DROP INDEX trial_source_identity_key")
        if replacement is not None:
            db.execute(replacement)
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize(
    "definition",
    [
        pytest.param(
            "CREATE INDEX trial_source_identity_key ON trials(source_identity_key)",
            id="same-column-but-not-unique",
        ),
        pytest.param(
            "CREATE UNIQUE INDEX trial_source_identity_key ON trials(run_id)",
            id="unique-but-wrong-column",
        ),
        pytest.param(
            "CREATE UNIQUE INDEX trial_source_identity_key "
            "ON trials(source_identity_key,run_id)",
            id="unique-compound-not-identity-only",
        ),
        pytest.param(
            "CREATE UNIQUE INDEX trial_source_identity_key "
            "ON trials(source_identity_key) WHERE outcome='pass'",
            id="partial-index",
        ),
    ],
)
def test_spoofed_named_index_rejected_during_restart(tmp_path, definition):
    store, path, original = seeded(tmp_path)
    mutate_index(store, definition)
    assert store.pool_snapshot()["runs"] == 1
    with pytest.raises(TrialStoreError, match="index integrity"):
        TrialStore(path)
    assert store.pool_snapshot()["runs"] == 1


def insert_alias_by_bypassing_index(store, original, alias):
    assert original["trial_id"] != alias["trial_id"]
    assert _source_identity_key(original) == _source_identity_key(alias)
    raw = canonical_trial_bytes(alias)
    db = store._connect()
    try:
        db.execute("DROP INDEX trial_source_identity_key")
        db.execute(
            """
            INSERT INTO trials(
                trial_id,source_identity_key,canonical_json,payload_sha256,outcome,
                system,node,source,run_id,check_id,check_version,
                check_config_sha256,attempt_id,first_sender,first_transport,
                bridge_run_id,received_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                alias["trial_id"], _source_identity_key(alias), raw,
                hashlib.sha256(raw).hexdigest(), alias["outcome"],
                alias["system"], alias["node"], alias["source"], alias["run_id"],
                alias["check_id"], alias["check_version"],
                alias["check_config_sha256"], alias["attempt_id"],
                "peer-b", "fleet", None, 42.0,
            ),
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize(
    "alias_system",
    ["GROKBOT", "Ｇｒｏｋｂｏｔ", "grokbot" + chr(0x200B)],
)
def test_duplicate_canonical_source_attempt_cannot_inflate_live_pool(
    tmp_path, alias_system,
):
    store, path, original = seeded(tmp_path)
    insert_alias_by_bypassing_index(store, original, trial(alias_system))
    with pytest.raises(TrialStoreError, match="source identity alias collision"):
        store.pool_snapshot()
    with pytest.raises(TrialStoreError, match="source identity alias collision"):
        store.non_pass_records()
    # A new receiver tries to recreate the missing unique index and refuses
    # the duplicate historical data instead of silently accepting it.
    with pytest.raises(TrialStoreError, match="initialization failed"):
        TrialStore(path)


def test_missing_index_is_rebuilt_from_clean_trusted_data_on_restart(tmp_path):
    store, path, original = seeded(tmp_path)
    mutate_index(store, None)
    assert store.pool_snapshot()["runs"] == 1
    reopened = TrialStore(path)
    db = reopened._connect()
    try:
        matching = [
            row for row in db.execute("PRAGMA index_list('trials')")
            if row["name"] == "trial_source_identity_key"
        ]
        assert len(matching) == 1
        assert matching[0]["unique"] == 1 and matching[0]["partial"] == 0
        fields = [
            row["name"] for row in db.execute("PRAGMA index_info('trial_source_identity_key')")
        ]
        assert fields == ["source_identity_key"]
    finally:
        db.close()
    assert reopened.pool_snapshot()["runs"] == 1


def test_valid_source_alias_via_ingest_is_quarantined_not_counted(tmp_path):
    store, path, original = seeded(tmp_path)
    alias = trial("GROKBOT")
    result = store.ingest_many([alias], sender="peer-b", transport="fleet")
    assert result["quarantined"] == 1
    assert store.pool_snapshot()["runs"] == 1
    assert "canonical source" in store.quarantine_records()[0]["reason"]
    assert TrialStore(path).pool_snapshot()["runs"] == 1


def test_distinct_attempts_remain_independent_despite_matching_source(tmp_path):
    store, path, original = seeded(tmp_path)
    second = trial("GROKBOT", "second-attempt")
    assert store.ingest_many([second], sender="peer-b", transport="fleet")["accepted"] == 1
    assert store.pool_snapshot()["runs"] == 2
    assert TrialStore(path).pool_snapshot()["runs"] == 2
