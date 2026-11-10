import asyncio

import pytest

from avrone.trial_adapters import (
    BRIDGE_TRIAL_SCHEMA,
    BRIDGE_TRIAL_PATH,
    FLEET_TRIAL_SCHEMA,
    FLEET_TRIAL_PATH,
    BridgeTrialAdapter,
    FleetTrialAdapter,
    strict_json_loads,
)
from avrone.trial_contract import make_trial
from avrone.trial_receiver import TrialStore


def sample():
    return make_trial(
        system="grokbot", node="worker-1", run_id="run-2", check_id="mcp-health",
        check_version="v1", attempt_id="attempt-1", outcome="pass",
    )


def wrapper(schema, trials, **extra):
    return {
        "schema": schema, "from": "peer-a", "hops": 1,
        "capability": "evidence:trial:ingest", "trials": trials, **extra,
    }


def test_fleet_adapter_requires_exact_authenticated_wrapper(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    adapter = FleetTrialAdapter(store)
    result = adapter.receive(wrapper(FLEET_TRIAL_SCHEMA, [sample()]), "peer-a", 3)
    assert result["accepted"] == 1
    with pytest.raises(ValueError, match="sender"):
        adapter.receive(wrapper(FLEET_TRIAL_SCHEMA, [sample()]), "peer-b", 3)
    bad = wrapper(FLEET_TRIAL_SCHEMA, [sample()])
    bad["capability"] = "spawn:squad"
    with pytest.raises(ValueError, match="capability"):
        adapter.receive(bad, "peer-a", 3)
    bad = wrapper(FLEET_TRIAL_SCHEMA, [sample()])
    bad["unexpected"] = True
    with pytest.raises(ValueError, match="fields"):
        adapter.receive(bad, "peer-a", 3)


def test_bridge_adapter_preserves_run_scope(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    adapter = BridgeTrialAdapter(store)
    payload = wrapper(BRIDGE_TRIAL_SCHEMA, [sample()], run_id="bridge-run-7")
    result = adapter.receive(payload, "peer-a", 3)
    assert result["accepted"] == 1
    assert store.non_pass_records() == []
    db = store._connect()
    try:
        row = db.execute("SELECT bridge_run_id,first_transport FROM trials").fetchone()
    finally:
        db.close()
    assert row["bridge_run_id"] == "bridge-run-7"
    assert row["first_transport"] == "bridge"


def test_bridge_adapter_uses_existing_wave1_shared_run_semantics(tmp_path):
    store = TrialStore(tmp_path / "trials.sqlite3")
    adapter = BridgeTrialAdapter(store)
    payload = wrapper(
        BRIDGE_TRIAL_SCHEMA, [sample()], run_id="bridge-run-7-wave1"
    )
    result = adapter.receive(payload, "peer-a", 3)
    assert result["accepted"] == 1
    db = store._connect()
    try:
        row = db.execute("SELECT bridge_run_id FROM trials").fetchone()
    finally:
        db.close()
    assert row["bridge_run_id"] == "bridge-run-7"


def test_batch_and_hop_bounds_are_enforced(tmp_path):
    adapter = FleetTrialAdapter(TrialStore(tmp_path / "trials.sqlite3"))
    too_many = wrapper(FLEET_TRIAL_SCHEMA, [sample()] * 11)
    with pytest.raises(ValueError, match="1..10"):
        adapter.receive(too_many, "peer-a", 3)
    too_far = wrapper(FLEET_TRIAL_SCHEMA, [sample()])
    too_far["hops"] = 4
    with pytest.raises(ValueError, match="hops"):
        adapter.receive(too_far, "peer-a", 3)
    bad_sender = wrapper(FLEET_TRIAL_SCHEMA, [sample()])
    bad_sender["from"] = "peer-a/agent"
    with pytest.raises(ValueError, match="sender"):
        adapter.receive(bad_sender, "peer-a", 3)


def test_strict_json_rejects_duplicate_keys_nonfinite_and_deep_input():
    with pytest.raises(ValueError, match="unambiguous"):
        strict_json_loads(b'{"from":"peer-a","from":"peer-b"}')
    with pytest.raises(ValueError, match="unambiguous"):
        strict_json_loads(b'{"value":NaN}')
    deep = b'{"x":' * 1200 + b'0' + b'}' * 1200
    with pytest.raises(ValueError, match="unambiguous"):
        strict_json_loads(deep)


def test_outbound_adapters_use_fleet_policy_and_signed_post_paths():
    class FakeManager:
        enabled = True
        node_id = "peer-a"
        max_hops = 3

        def __init__(self):
            self.calls = []

        def check_outbound(self, *args, **kwargs):
            self.calls.append(("policy", args))

        def pick(self, node_id):
            self.calls.append(("pick", node_id))
            return {"url": "https://receiver.test"}

        async def _post(self, base, path, payload, timeout=30):
            self.calls.append(("post", base, path, payload, timeout))
            return {"status": "accepted"}

    manager = FakeManager()
    result = asyncio.run(FleetTrialAdapter.send(manager, "receiver", [sample()]))
    assert result["status"] == "accepted"
    assert any(call[0] == "post" and call[2] == FLEET_TRIAL_PATH for call in manager.calls)

    manager.calls.clear()
    result = asyncio.run(
        BridgeTrialAdapter.send(manager, "receiver", "bridge-1", [sample()])
    )
    assert result["status"] == "accepted"
    assert any(call[0] == "post" and call[2] == BRIDGE_TRIAL_PATH for call in manager.calls)
    assert any(call[0] == "post" and call[3]["schema"] == BRIDGE_TRIAL_SCHEMA for call in manager.calls)
