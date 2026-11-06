#!/usr/bin/env python3
"""Minimal relay loop: push liveness events, pause on saturation."""
from __future__ import annotations
import time
from liveness_event import LivenessEvent, LivenessState


class RelayLoop:
    def __init__(self, consumers: dict):
        self.consumers = consumers
        self.paused = False
        self.delivered: list = []

    def dispatch(self, event: LivenessEvent):
        state = event.state
        if state == LivenessState.SATURATED:
            self.paused = True
            return
        if state == LivenessState.DEAD:
            self.consumers.pop(event.consumer_id, None)
            return
        if state == LivenessState.RETIRING:
            self.consumers.pop(event.consumer_id, None)
            return
        if self.paused and state == LivenessState.READY:
            self.paused = False
            return
        self.delivered.append(event)

    def census(self) -> dict:
        return {"consumers": len(self.consumers), "paused": self.paused,
                "delivered": len(self.delivered)}
