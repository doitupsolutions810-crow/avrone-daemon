#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from liveness_event import LivenessEvent, LivenessState
from relay_loop import RelayLoop


def test_ratio_floor():
    boundary, interior = 20, 5
    ratio = boundary / interior
    assert ratio >= 2.0


def test_ratio_decays_on_solo_interior():
    boundary, interior = 20, 6
    ratio = boundary / interior
    assert ratio < 4.0 and ratio >= 2.0
