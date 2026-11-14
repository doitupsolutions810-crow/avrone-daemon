"""Round 199: deterministic replay, source alias, and concurrent sender stress.

Fixed seeds preserve reproducibility. Each scenario exercises many independent
SQLite transactions and checks that first-seen evidence, sender quarantine and
retained receipts agree after restart. This is bounded stress, not a formal
proof of all interleavings or a live multi-node simulation.
"""
from __future__ import annotations

import random
import sqlite3
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore


SOURCE_VARIANTS = ("grokbot", "GROKBOT", "Ｇｒｏｋｂｏｔ", "grokbot" + chr(0x200B))


def trial(index, *, system="grokbot", outcome="pass"):
    return make_trial(
        system=system, node="node-a", run_id="run-199",
        check_id="replay-fuzz", check_version="v2",
        attempt_id=f"attempt-{index}", outcome=outcome,
        check_config={"index": index},
    )


def store_at(tmp_path):
    path = tmp_path / "private" / "trials.sqlite3"
    return TrialStore(path), path


def integrity_check(store):
    db = store._connect()
    try:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        counts = tuple(
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("trials", "trial_quarantine", "trial_quarantined_senders", "trial_receipts")
        )
        assert counts[1] == counts[2]
        return counts
    finally:
        db.close()


@pytest.mark.parametrize("seed", [0, 7, 42, 199])
def test_shuffled_same_id_alias_contradiction_and_replay_is_order_safe(tmp_path, seed):
    store, path = store_at(tmp_path)
    items = []
    attempts = 8
    for i in range(attempts):
        base_outcome = ("pass", "fail", "skipped")[i % 3]
        opposite = "fail" if base_outcome != "fail" else "pass"
        base = trial(i, outcome=base_outcome)
        variants = (
            ("base", base),
            ("duplicate", base),
            ("contradiction", trial(i, outcome=opposite)),
            ("alias", trial(i, system=SOURCE_VARIANTS[(i % 3) + 1], outcome=base_outcome)),
        )
        for label, envelope in variants:
            items.append((i, label, envelope))
    random.Random(seed).shuffle(items)

    accepted_outcomes = []
    statuses = Counter()
    for index, label, envelope in items:
        sender = f"peer-{index}-{label}"
        result = store.ingest_many([envelope], sender=sender, transport="fleet")
        status = result["results"][0]["status"]
        statuses[status] += 1
        if status in {"accepted", "non_pass_stored"}:
            accepted_outcomes.append(envelope["outcome"])

    assert len(accepted_outcomes) == attempts
    assert statuses["accepted"] + statuses["non_pass_stored"] == attempts
    assert statuses["sender_quarantined"] == 0
    assert statuses["conflict_quarantined"] >= 2 * attempts
    snapshot = store.pool_snapshot()
    assert snapshot["runs"] == sum(x in {"pass", "fail"} for x in accepted_outcomes)
    assert snapshot["failures"] == accepted_outcomes.count("fail")
    assert len(store.quarantine_records()) == statuses["conflict_quarantined"]
    assert len(store.receipt_records()) == len(items)
    assert integrity_check(store) == (
        attempts, statuses["conflict_quarantined"],
        statuses["conflict_quarantined"], len(items),
    )

    reopened = TrialStore(path)
    assert reopened.pool_snapshot() == snapshot
    assert reopened.quarantine_records() == store.quarantine_records()
    assert reopened.receipt_records() == store.receipt_records()

    for k, (index, label, envelope) in enumerate(items):
        result = reopened.ingest_many(
            [envelope], sender=f"peer-replay-{seed}-{k}", transport="fleet"
        )
        assert result["accepted"] == 0 and result["non_pass"] == 0
        assert result["results"][0]["status"] in {"duplicate", "conflict_quarantined"}

    assert reopened.pool_snapshot() == snapshot
    assert integrity_check(reopened)[0] == attempts
    assert len(reopened.receipt_records()) == 2 * len(items)
    assert TrialStore(path).pool_snapshot() == snapshot


@pytest.mark.parametrize("seed", [2, 19, 91])
def test_concurrent_duplicate_and_alias_storm_keeps_unique_first_seen_pool(tmp_path, seed):
    store, path = store_at(tmp_path)
    count = 8
    bases = [trial(i, outcome="pass" if i % 2 == 0 else "fail") for i in range(count)]
    for i, envelope in enumerate(bases):
        assert store.ingest_many(
            [envelope], sender=f"seed-{i}", transport="fleet"
        )["accepted"] == 1

    tasks = []
    for i, base in enumerate(bases):
        opposite = "fail" if base["outcome"] == "pass" else "pass"
        tasks.extend([
            (f"duplicate-{i}", base),
            (f"contradict-{i}", trial(i, outcome=opposite)),
            (f"alias-{i}", trial(
                i, system=SOURCE_VARIANTS[(i % 3) + 1], outcome=base["outcome"],
            )),
        ])
    random.Random(seed).shuffle(tasks)

    def deliver(task):
        sender, payload = task
        return store.ingest_many(
            [payload], sender=sender, transport="fleet"
        )["results"][0]["status"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = Counter(pool.map(deliver, tasks))

    assert statuses == Counter({"duplicate": count, "conflict_quarantined": 2 * count})
    snapshot = store.pool_snapshot()
    assert snapshot["runs"] == count
    assert snapshot["failures"] == count // 2
    assert len(store.quarantine_records()) == 2 * count
    assert len(store.receipt_records()) == 4 * count
    assert integrity_check(store) == (count, 2 * count, 2 * count, 4 * count)

    reopened = TrialStore(path)
    assert reopened.pool_snapshot() == snapshot
    assert len(reopened.quarantine_records()) == 2 * count
    assert reopened.receipt_records() == store.receipt_records()


def test_thirty_equivalent_sources_with_distinct_attempts_remain_independent(tmp_path):
    store, path = store_at(tmp_path)
    for index in range(30):
        envelope = trial(index + 100, system=SOURCE_VARIANTS[index % len(SOURCE_VARIANTS)])
        result = store.ingest_many(
            [envelope], sender=f"peer-independent-{index}", transport="fleet"
        )
        assert result["accepted"] == 1

    snapshot = store.pool_snapshot()
    assert snapshot["runs"] == 30
    assert snapshot["failures"] == 0
    assert len(store.quarantine_records()) == 0
    assert integrity_check(store) == (30, 0, 0, 30)
    assert TrialStore(path).pool_snapshot() == snapshot
