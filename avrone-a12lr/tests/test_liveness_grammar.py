#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from liveness_event import LivenessEvent, LivenessState
from schema_gate import validate_event
from tombstone import Tombstone
from relay_loop import RelayLoop


def test_rejects_non_dict():
    try:
        validate_event([1, 2, 3])
        assert False, "should have raised"
    except TypeError:
        pass


def test_rejects_missing_fields():
    try:
        validate_event({})
        assert False, "should have raised"
    except ValueError as e:
        assert "missing required fields" in str(e)


def test_rejects_bad_state():
    try:
        validate_event({"consumer_id": "c1", "state": "glowing", "ts": 1.0})
        assert False, "should have raised"
    except ValueError as e:
        assert "invalid state" in str(e)


def test_depth_over_capacity():
    try:
        validate_event({"consumer_id": "c1", "state": "ready", "ts": 1.0,
                       "buffer_depth": 5, "capacity": 3})
        assert False, "should have raised"
    except ValueError as e:
        assert "exceeds capacity" in str(e)


def test_tombstone_three_window():
    t = Tombstone("daemon-7", "poll-stall", "leo", "saturated")
    for w in ("w1", "w2", "w3"):
        t.add_window(w)
    assert t.is_proven()
    assert len(t.windows) == 3


def test_relay_routes_four_states():
    r = RelayLoop({"d1": object()})
    r.dispatch(LivenessEvent("d1", LivenessState.SATURATED))
    assert r.paused
    r.dispatch(LivenessEvent("d1", LivenessState.READY))
    assert not r.paused
    r.dispatch(LivenessEvent("d1", LivenessState.DEAD))
    assert "d1" not in r.census()
