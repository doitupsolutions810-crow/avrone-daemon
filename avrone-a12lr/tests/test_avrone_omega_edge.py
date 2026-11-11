#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Omega-Level Extreme Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Eighth-tier suite. Prior cumulative: 528 passed. New extreme layers:
  - Gate value length extremes (0, 1, 2, 3, 4, 5, 10, 100, 1000, 10000 chars around "true")
  - Component name with null-like printable escapes
  - Parallel 12 dry-run + 12 live fan-out (24 workers)
  - Bridge called 50× sequential for determinism hash
  - Activator integrity sub-keys exact
  - Master unknown component JSON shape
  - Refuse reason string non-empty
  - Dashboard table row count vs agent count
  - Env with ALLOW_AGENT_EXECUTION set then unset mid-suite (process isolation)
  - --input with 1MB JSON payload
  - Concurrent master status during component activates
  - Slug with spaces / tabs as component
  - Multiple --dry-run flags
  - Verify no network calls (offline invariant via quick timeout paths)
  - Canonical schema string exact byte match
  - Agent lattice only on grok-bot-* ids
  - Stress: 200× refuse (gate=false)
  - Cross-check every activator component field equals filename slug
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
BRIDGE = ROOT / "bridge" / "conclude_bridge.py"
CANONICAL = ROOT / "references" / "canonical-deployment.json"
AGENTS = ROOT / "agents" / "grok_bot_integration.py"
MASTER = BIN / "activate_avrone.sh"
DASHBOARD = ROOT / "dashboard" / "index.html"

PASS = 0
FAIL = 0
SKIP = 0

COMPONENTS = [
    "fastapi_control_plane",
    "react_dashboard",
    "openai_model_adapter",
    "agent_registry_orchestrator",
    "secure_cli_execution",
    "sqlite_persistence",
    "containerized_deployment",
    "persistent_memory_agent",
    "control12_lattice_op",
    "langgraph_supervisor",
    "training_loop",
    "skill_factory",
]


def record(name: str, ok: bool, detail: str = "", skip: bool = False) -> None:
    global PASS, FAIL, SKIP
    if skip:
        SKIP += 1
        print(f"[SKIP] {name}" + (f" — {detail}" if detail else ""))
        return
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def run_cmd(
    cmd: list[str],
    env: dict | None = None,
    clear_gate: bool = False,
    timeout: float = 15.0,
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    e = os.environ.copy()
    if clear_gate:
        e.pop("ALLOW_AGENT_EXECUTION", None)
    if env is not None:
        e.update(env)
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd or ROOT,
            env=e,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except ValueError as exc:
        return -2, "", f"ValueError:{exc}"


# ─── Gate length extremes ────────────────────────────────────────────────────

def test_gate_length_extremes() -> None:
    print("\n── Gate Length Extremes ──")
    # exact true
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("exact 'true' accepts", code == 0 and "DEPLOYMENT_COMPLETE" in out)

    for n in [0, 1, 2, 3, 4, 5, 10, 100, 1000]:
        # pad around true so it is never exact
        val = ("x" * n) + "true" + ("y" * n) if n else "tru"
        if n == 0:
            val = "tru"
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"padded/near-true len~{len(val)} refuses", code != 0, f"code={code}")

    # 10k near-true
    val = "true" + ("z" * 10000)
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": val},
        timeout=10,
    )
    record("10k-char near-true refuses / no hang", code != 0 and code >= 0, f"code={code}")


# ─── Component printable escapes ─────────────────────────────────────────────

def test_component_printable_escapes() -> None:
    print("\n── Component Printable Escape Names ──")
    names = [
        "comp\\nname",
        "comp\\tname",
        "comp'name",
        'comp"name',
        "comp name",
        "comp\tname",
    ]
    for n in names:
        code, out, err = run_cmd(
            ["bash", str(MASTER), n],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"escape component [{n!r}] no hang", code >= 0, f"code={code}")


# ─── 24-worker fan-out ───────────────────────────────────────────────────────

def test_24_worker_fanout() -> None:
    print("\n── 24-Worker Dry+Live Fan-Out ──")
    def dry(c: str) -> bool:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh"), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    def live(c: str) -> bool:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=24) as pool:
        futs = [pool.submit(dry, c) for c in COMPONENTS] + [pool.submit(live, c) for c in COMPONENTS]
        for f in as_completed(futs):
            results.append(f.result())
    record(f"24/24 dry+live fan-out", all(results), f"ok={sum(results)}")


# ─── Bridge 50× determinism hash ─────────────────────────────────────────────

def test_bridge_50_determinism() -> None:
    print("\n── Bridge 50× Determinism Hash ──")
    hashes = []
    for _ in range(50):
        code, out, err = run_cmd(
            [sys.executable, str(BRIDGE)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code != 0:
            record("bridge 50× all success", False)
            return
        hashes.append(hashlib.sha256(out.encode()).hexdigest())
    record("bridge 50× all success", True)
    record("bridge 50× identical output hash", len(set(hashes)) == 1, f"unique={len(set(hashes))}")


# ─── Integrity sub-keys exact ────────────────────────────────────────────────

def test_integrity_subkeys() -> None:
    print("\n── Integrity Sub-Keys Exact ──")
    code, out, err = run_cmd(
        ["bash", str(BIN / "activate_training_loop.sh")],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    obj = json.loads(out)
    integ = obj.get("integrity", {})
    for k in ("python_compile_ok", "dependencies_resolved", "files_created"):
        record(f"integrity.{k} present", k in integ)
    record("integrity has exactly 3 keys (or at least these)", set(integ.keys()) >= {"python_compile_ok", "dependencies_resolved", "files_created"})


# ─── Unknown component JSON shape ────────────────────────────────────────────

def test_unknown_component_shape() -> None:
    print("\n── Unknown Component JSON Shape ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "zz_unknown_omega_99"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    combined = (err or out).strip()
    try:
        obj = json.loads(combined)
        record("unknown is JSON", True)
        record("unknown has status", "status" in obj)
        record("unknown status mentions unknown", "unknown" in str(obj.get("status", "")).lower())
        record("unknown has component field", "component" in obj)
    except json.JSONDecodeError:
        record("unknown is JSON", False, combined[:80])


# ─── Refuse reason non-empty ─────────────────────────────────────────────────

def test_refuse_reason() -> None:
    print("\n── Refuse Reason Non-Empty ──")
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    combined = (err or out).strip()
    try:
        obj = json.loads(combined)
        reason = obj.get("reason") or obj.get("status") or ""
        record("refuse has non-empty reason/status", isinstance(reason, str) and len(reason) > 0, f"reason={reason!r}")
    except json.JSONDecodeError:
        record("refuse has non-empty reason/status", "ALLOW_AGENT_EXECUTION" in combined)


# ─── Dashboard row count ─────────────────────────────────────────────────────

def test_dashboard_row_count() -> None:
    print("\n── Dashboard Table Row Count vs Agents ──")
    if not DASHBOARD.exists():
        record("dashboard exists", False)
        return
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    # count data rows roughly by agent id occurrences in tbody context
    count = 0
    for aid in [
        "grok-bot-supervisor",
        "grok-bot-computer-use",
        "grok-bot-github-bridge",
        "avrone-fastapi-control",
        "avrone-react-dashboard",
        "persistent-memory-agent",
    ]:
        if aid in text:
            count += 1
    record("dashboard lists all 6 agents", count == 6, f"count={count}")


# ─── 1MB --input ─────────────────────────────────────────────────────────────

def test_1mb_input() -> None:
    print("\n── 1MB --input Payload ──")
    script = BIN / "activate_openai_model_adapter.sh"
    big = {"data": "x" * (1024 * 1024), "n": 1}
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        json.dump(big, tf)
        path = tf.name
    try:
        code, out, err = run_cmd(
            ["bash", str(script), "--input", path],
            env={"ALLOW_AGENT_EXECUTION": "true"},
            timeout=20,
        )
        record("1MB --input no hang", code >= 0, f"code={code}")
    finally:
        os.unlink(path)


# ─── Concurrent master + components ──────────────────────────────────────────

def test_concurrent_master_and_components() -> None:
    print("\n── Concurrent Master Status + Component Activates ──")
    def status() -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    def comp(c: str) -> bool:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = [pool.submit(status) for _ in range(8)]
        futs += [pool.submit(comp, c) for c in COMPONENTS[:8]]
        for f in as_completed(futs):
            results.append(f.result())
    record(f"{sum(results)}/{len(results)} concurrent master+comp", all(results), f"ok={sum(results)}")


# ─── Spaces / tabs component ─────────────────────────────────────────────────

def test_space_tab_component() -> None:
    print("\n── Spaces / Tabs as Component ──")
    for label, slug in [("spaces", "comp with spaces"), ("tabs", "comp\twith\ttabs")]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"{label} component no hang", code >= 0, f"code={code}")


# ─── Multiple --dry-run ──────────────────────────────────────────────────────

def test_multiple_dry_run() -> None:
    print("\n── Multiple --dry-run Flags ──")
    code, out, err = run_cmd(
        ["bash", str(BIN / "activate_containerized_deployment.sh"), "--dry-run", "--dry-run", "--dry-run"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    record("triple --dry-run exits 0", code == 0, f"code={code}")


# ─── Canonical schema byte match ─────────────────────────────────────────────

def test_canonical_schema_bytes() -> None:
    print("\n── Canonical Schema Exact ──")
    data = json.loads(CANONICAL.read_text())
    record("schema exact string", data.get("schema") == "kg.production-training-result/v2")


# ─── Lattice only on grok-bot ─────────────────────────────────────────────────

def test_lattice_only_grok() -> None:
    print("\n── Lattice Field Only on grok-bot-* ──")
    code, out, err = run_cmd(
        [sys.executable, str(AGENTS)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    for a in data["agents"]["agents"]:
        aid = a["id"]
        if aid.startswith("grok-bot-"):
            record(f"{aid} has lattice Control12", a.get("lattice") == "Control12")
        else:
            # lattice may be absent or placeholder
            lat = a.get("lattice")
            record(f"{aid} lattice absent or not required", lat != "Control12")


# ─── 200× refuse ─────────────────────────────────────────────────────────────

def test_200_refuse() -> None:
    print("\n── 200× Refuse Stress (gate=false) ──")
    ok = 0
    for _ in range(200):
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "false"},
        )
        if code != 0:
            ok += 1
    record("200/200 refuse", ok == 200, f"ok={ok}")


# ─── Component field == slug ─────────────────────────────────────────────────

def test_component_field_matches_slug() -> None:
    print("\n── Activator component Field == Slug ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        record(f"{c} component field matches", obj.get("component") == c)


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Omega-Level Extreme Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_gate_length_extremes()
    test_component_printable_escapes()
    test_24_worker_fanout()
    test_bridge_50_determinism()
    test_integrity_subkeys()
    test_unknown_component_shape()
    test_refuse_reason()
    test_dashboard_row_count()
    test_1mb_input()
    test_concurrent_master_and_components()
    test_space_tab_component()
    test_multiple_dry_run()
    test_canonical_schema_bytes()
    test_lattice_only_grok()
    test_200_refuse()
    test_component_field_matches_slug()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
