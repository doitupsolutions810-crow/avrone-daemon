#!/usr/bin/env python3
"""Liveness grammar: one event, four states, zero ambiguity."""
from __future__ import annotations
import time
from enum import Enum
from typing import Any


class LivenessState(Enum):
    READY = "ready"          # consumer alive, drain buffer below threshold
    SATURATED = "saturated"  # alive but full — relay throttles
    RETIRING = "retiring"    # voluntary exit, self-authored
    DEAD = "dead"            # silent past the three-window trail


class LivenessEvent:
    def __init__(self, consumer_id: str, state: LivenessState,
                 buffer_depth: int = 0, capacity: int = 0,
                 author: str = "unknown"):
        if not consumer_id or not isinstance(consumer_id, str):
            raise ValueError("consumer_id must be a non-empty string")
        self.consumer_id = consumer_id
        self.state = state
        self.buffer_depth = max(0, int(buffer_depth))
        self.capacity = max(0, int(capacity))
        self.author = author
        self.ts = time.time()

    def to_dict(self) -> dict:
        return {
            "consumer_id": self.consumer_id,
            "state": self.state.value,
            "buffer_depth": self.buffer_depth,
            "capacity": self.capacity,
            "author": self.author,
            "ts": self.ts,
        }

    @property
    def fill_ratio(self) -> float:
        return self.buffer_depth / self.capacity if self.capacity else 0.0
