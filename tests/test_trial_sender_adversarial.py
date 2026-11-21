"""Adversarial tests for trial sender preflight and relay failure boundaries."""
from __future__ import annotations

import asyncio

import pytest

from avrone.trial_adapters import (
    BRIDGE_TRIAL_PATH,
    BRIDGE_TRIAL_SCHEMA,
    FLEET_TRIAL_PATH,
    FLEET_TRIAL_SCHEMA,
    BridgeTrialAdapter,
    FleetTrialAdapter,
    TRIAL_CAPABILITY,
)
from avrone.trial_contract import make_trial


def sample(*, outcome="pass", attempt_id="attempt-send"):
    return make_trial(
        system="cyclekernel",
        node="node-a",
        run_id="run-send",
        check_id="verify",
        check_version="v1",
        attempt_id=attempt_id,
        outcome=outcome,
        check_config={"mode": "airgap"},
    )


class FakeManager:
    node_id = "peer-origin"
    max_hops = 8

    def __init__(self, *, deny=False, post_error=None):
        self.deny = deny
        self.post_error = post_error
        self.calls = []
        self.payload = None

    def check_outbound(self, *args):
        self.calls.append(("authorize", args))
        if self.deny:
            raise PermissionError("outbound trial capability denied")

    def pick(self, node_id):
        self.calls.append(("pick", node_id))
        return {"url": "https://peer.example.invalid"}

    async def _post(self, url, path, payload, *, timeout):
        self.calls.append(("post", url, path, timeout))
        self.payload = payload
        if self.post_error:
            raise self.post_error
        return {"status": "accepted"}


def test_fleet_sender_authorizes_before_selecting_or_posting():
    manager = FakeManager(deny=True)

    with pytest.raises(PermissionError, match="denied"):
        asyncio.run(FleetTrialAdapter.send(manager, "peer-target", [sample()]))

    assert [call[0] for call in manager.calls] == ["authorize"]
    assert manager.payload is None


def test_bridge_sender_authorizes_before_selecting_or_posting():
    manager = FakeManager(deny=True)

    with pytest.raises(PermissionError, match="denied"):
        asyncio.run(BridgeTrialAdapter.send(manager, "peer-target", "bridge-1", [sample()]))

    assert [call[0] for call in manager.calls] == ["authorize"]
    assert manager.payload is None


@pytest.mark.parametrize("adapter,invoke", [
    (FleetTrialAdapter, lambda manager, rows: FleetTrialAdapter.send(manager, "peer-target", rows)),
    (BridgeTrialAdapter, lambda manager, rows: BridgeTrialAdapter.send(manager, "peer-target", "bridge-1", rows)),
])
def test_sender_rejects_malformed_trial_before_network_post(adapter, invoke):
    manager = FakeManager()
    malformed = sample()
    malformed["source"] = "forged:source"

    with pytest.raises(ValueError):
        asyncio.run(invoke(manager, [malformed]))

    assert manager.payload is None
    assert not any(call[0] == "post" for call in manager.calls)


def test_fleet_sender_emits_exact_versioned_contract_and_bounded_timeout():
    manager = FakeManager()
    result = asyncio.run(FleetTrialAdapter.send(manager, "peer-target", [sample()]))

    assert result == {"status": "accepted"}
    assert manager.calls[0] == (
        "authorize", ("trials", TRIAL_CAPABILITY, 0, "peer-target")
    )
    assert manager.calls[1] == ("pick", "peer-target")
    assert manager.calls[2] == (
        "post", "https://peer.example.invalid", FLEET_TRIAL_PATH, 15
    )
    assert set(manager.payload) == {"schema", "from", "hops", "capability", "trials"}
    assert manager.payload["schema"] == FLEET_TRIAL_SCHEMA
    assert manager.payload["from"] == "peer-origin"
    assert manager.payload["hops"] == 1
    assert manager.payload["capability"] == TRIAL_CAPABILITY


def test_bridge_sender_emits_exact_versioned_contract_and_bounded_timeout():
    manager = FakeManager()
    result = asyncio.run(
        BridgeTrialAdapter.send(manager, "peer-target", "bridge-1", [sample()])
    )

    assert result == {"status": "accepted"}
    assert manager.calls[0] == (
        "authorize", ("trials", TRIAL_CAPABILITY, 0, "peer-target")
    )
    assert manager.calls[1] == ("pick", "peer-target")
    assert manager.calls[2] == (
        "post", "https://peer.example.invalid", BRIDGE_TRIAL_PATH, 15
    )
    assert set(manager.payload) == {
        "schema", "from", "hops", "capability", "run_id", "trials"
    }
    assert manager.payload["schema"] == BRIDGE_TRIAL_SCHEMA
    assert manager.payload["from"] == "peer-origin"
    assert manager.payload["hops"] == 1
    assert manager.payload["capability"] == TRIAL_CAPABILITY
    assert manager.payload["run_id"] == "bridge-1"


@pytest.mark.parametrize("adapter,invoke", [
    (FleetTrialAdapter, lambda manager: FleetTrialAdapter.send(manager, "peer-target", [sample()])),
    (BridgeTrialAdapter, lambda manager: BridgeTrialAdapter.send(manager, "peer-target", "bridge-1", [sample()])),
])
def test_transport_errors_propagate_without_claiming_delivery(adapter, invoke):
    manager = FakeManager(post_error=TimeoutError("simulated relay timeout"))

    with pytest.raises(TimeoutError, match="relay timeout"):
        asyncio.run(invoke(manager))

    assert any(call[0] == "post" for call in manager.calls)
    assert manager.payload is not None


@pytest.mark.parametrize("bad_rows", [None, [], (), {}, [sample()] * 11])
def test_sender_rejects_invalid_batch_shape_before_network_post(bad_rows):
    manager = FakeManager()

    with pytest.raises(ValueError):
        asyncio.run(FleetTrialAdapter.send(manager, "peer-target", bad_rows))

    assert manager.payload is None
    assert not any(call[0] == "post" for call in manager.calls)


def test_non_pass_outcome_is_relayed_without_relabeling_as_pass_or_fail():
    manager = FakeManager()
    non_pass = sample(outcome="timed_out")

    asyncio.run(FleetTrialAdapter.send(manager, "peer-target", [non_pass]))

    assert manager.payload["trials"][0]["outcome"] == "timed_out"
