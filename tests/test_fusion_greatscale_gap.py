"""Gap bridge: fusion does not yet emit Great Scale evidence.

INTEGRATION_ANALYSIS.md states:
  "Nothing here produces source: 'greatscale' or kind: 'evidence'."

These tests lock that gap so a future wiring PR is forced to turn them green
by actually emitting the evidence vector, not by deleting the assertions.
"""
from __future__ import annotations

import importlib
import inspect

import pytest

from avrone.greatscale import evidence_snapshot, CONE, heat, psi


def test_evidence_snapshot_is_importable_and_pure():
    snap = evidence_snapshot(0, 10)
    assert snap["onConeFloor"] is True
    assert snap["psi"] == pytest.approx(CONE)
    assert snap["orientation"] == "pass"


def test_fusion_modules_exist():
    # The fusion surface that should eventually emit evidence
    for mod in ("avrone.fusion_blackboard", "avrone.fusion_deliver", "avrone.fusion_peer"):
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            pytest.skip(f"{mod} not present in this checkout")


def test_no_greatscale_source_in_fusion_source_text():
    """Until the gap is closed, fusion source must not claim greatscale evidence."""
    found = []
    for name in ("fusion_blackboard", "fusion_deliver", "fusion_peer", "fusion_plane", "fusion_loopback"):
        try:
            mod = importlib.import_module(f"avrone.{name}")
        except ModuleNotFoundError:
            continue
        src = inspect.getsource(mod)
        if 'source": "greatscale"' in src or "source='greatscale'" in src or 'kind": "evidence"' in src:
            found.append(name)
    # Gap is still open: we expect zero modules to emit it yet.
    # When a wiring PR lands, this test should be inverted to assert presence.
    assert found == [], (
        f"Unexpected greatscale evidence emission in {found}. "
        "If intentional, update this test to require the emission contract."
    )


def test_evidence_contract_shape_for_future_wiring():
    """The shape a fusion emitter must produce when the gap is closed."""
    snap = evidence_snapshot(2, 10)
    required = {
        "failures", "runs", "p", "S", "psi", "cone", "onConeFloor",
        "orientation", "runsFor95", "singleRunDetect", "detectInRuns",
    }
    assert required <= set(snap.keys())
    assert snap["S"] == heat(snap["p"])
    assert snap["psi"] == pytest.approx(psi(snap["S"]))
    assert snap["orientation"] in ("pass", "fail", "mixed")


def test_contradiction_alarm_shape():
    """Descent III alarm: two cone-floor silos that disagree pool to max heat."""
    a = evidence_snapshot(5, 5)
    b = evidence_snapshot(0, 5)
    assert a["onConeFloor"] and b["onConeFloor"]
    pooled_f = a["failures"] + b["failures"]
    pooled_r = a["runs"] + b["runs"]
    pooled = evidence_snapshot(pooled_f, pooled_r)
    assert pooled["S"] == 0.25
    assert pooled["orientation"] == "mixed"
    # This is the signal fusion must surface when silos disagree.
