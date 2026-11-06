#!/usr/bin/env python3
"""Tombstone: retirement record with three-window trail."""
from __future__ import annotations
import hashlib
import time
from typing import Optional


class Tombstone:
    def __init__(self, subject_id: str, reason: str, author: str,
                 prior_state: str):
        self.subject_id = subject_id
        self.reason = reason
        self.author = author
        self.prior_state = prior_state
        self.created_ts = time.time()
        self.windows: list = []
        self._hash = self._compute_hash()

    def _compute_hash(self) -> str:
        payload = (f"{self.subject_id}|{self.reason}|{self.author}|"
                   f"{self.prior_state}|{self.created_ts}")
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def add_window(self, label: str):
        self.windows.append((time.time(), label))

    def is_proven(self, min_windows: int = 3) -> bool:
        return len(self.windows) >= min_windows

    @property
    def hash(self) -> str:
        return self._hash
