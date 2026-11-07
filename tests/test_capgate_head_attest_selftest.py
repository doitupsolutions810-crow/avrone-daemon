"""Regression coverage for the capgate attestation self-test."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_capgate_attestation_selftest_exercises_corruption_path():
    result = subprocess.run(
        [sys.executable, str(ROOT / "avrone" / "capgate_head_attest.py"), "--selftest"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert result.returncode == 0, result.stdout
    assert "middle corruption fails closed" in result.stdout
    assert "26 passed, 0 failed" in result.stdout
