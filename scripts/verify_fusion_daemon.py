#!/usr/bin/env python3
"""Run the fusion daemon tests and write an evidence JSON file.

The pytest run is the result. This script does not claim a soak, a Docker
success, or a remote delivery it did not perform. Related repos are read with
``gh`` when the command is available. Nothing is pushed.
"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    "tests/test_fusion_loopback.py",
    "tests/test_fusion_plane.py",
    "tests/test_fusion_blackboard.py",
    "tests/test_fusion_deliver.py",
    "tests/test_fusion_smoke.py",
    "tests/test_fusion_edges.py",
]
EVIDENCE = Path(os.environ.get("FUSION_EVIDENCE_PATH", "/tmp/fusion-daemon-evidence.json"))


def _delay_math() -> dict:
    sys.path.insert(0, str(ROOT))
    from avrone.fusion_deliver import DeliverConfig, retry_delay

    config = DeliverConfig(
        base="http://127.0.0.1:8090", events_path="/v1/events", health_path="/health",
        token="", timeout=5, deadline=0, breaker_threshold=1, breaker_cooldown=30,
        backoff_base=0.5, backoff_max=8, interval=1,
    )
    delays = [retry_delay("status429", n, config) for n in range(1, 21)]
    return {
        "unit": "seconds",
        "model": "delay_n = min(8 s, 0.5 s * 2^(n-1)) for n = 1..20",
        "assumption": "one admitted event, twenty HTTP 429 responses, then one 2xx; virtual clock",
        "delays_s": delays,
        "sum_s": sum(delays),
        "first_s": delays[0],
        "capped_from_attempt": next(i + 1 for i, d in enumerate(delays) if d == 8),
        "floor_s": 0.05,
        "identity": "A = D + P + F over unique (source, id); retries increment attempts only",
    }


def _gh_sha(repo: str, ref: str) -> dict:
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{repo}/commits/{ref}", "--jq", "{sha:.sha,date:.commit.committer.date,msg:.commit.message}"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ref": ref, "error": type(exc).__name__}
    if out.returncode != 0:
        return {"ref": ref, "error": (out.stderr or out.stdout)[:300]}
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"ref": ref, "error": "non-json"}
    msg = data.get("msg") or ""
    data["msg"] = msg.split("\n", 1)[0][:160]
    data["ref"] = ref
    return data


def main() -> int:
    os.chdir(ROOT)
    env = os.environ.copy()
    env["FUSION_EVIDENCE_PATH"] = str(EVIDENCE)
    started = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=line", *TESTS],
        cwd=ROOT, env=env, timeout=180, check=False,
    )
    elapsed = time.perf_counter() - started
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    # Linux ru_maxrss is kilobytes for the children pytest just reaped, plus this process.
    self_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    evidence = {
        "pytest_exit": proc.returncode,
        "pytest_files": TESTS,
        "wall_s": round(elapsed, 3),
        "ru_maxrss_children_kb": usage.ru_maxrss,
        "ru_maxrss_self_kb": self_kb,
        "delay_math": _delay_math(),
        "inspected": {
            "cyclekernal_main": _gh_sha("doitupsolutions810-crow/CycleKernal", "main"),
            "cyclekernal_pr14": _gh_sha("doitupsolutions810-crow/CycleKernal", "cursor/relay-events-intake-a5d0"),
            "avrone_pr20": _gh_sha("doitupsolutions810-crow/Avrone", "cursor/avrone-fusion-blackboard-59b2"),
            "fusion_pr1": _gh_sha("doitupsolutions810-crow/GreatScale-Avrone-Grok-Fusion-Bridge", "cursor/fusion-bridge-contracts-5b39"),
            "plane_main": _gh_sha("doitupsolutions810-crow/capability-authority", "main"),
            "plane_pr17": _gh_sha("doitupsolutions810-crow/capability-authority", "cursor/issuance-idempotency-contract-436d"),
        },
        "not_claimed": [
            "no 60-minute soak",
            "no production deploy",
            "no push to the Fusion Bridge repository",
            "capability-authority execute is not a runner",
        ],
    }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"evidence": str(EVIDENCE), "pytest_exit": proc.returncode, "wall_s": evidence["wall_s"]}, indent=2))
    return proc.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.TimeoutExpired:
        print("pytest exceeded 180s", file=sys.stderr)
        raise SystemExit(124)
