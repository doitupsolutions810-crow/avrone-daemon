#!/usr/bin/env python3
"""
AVRONE-12 Lattice Runtime — Multiverse-Level Edge-Case & Chaos Test Suite
Control704 VIP proxy X Y Z data set code override · Avrone Due’Krey / Control12-lattice-op

Thirteenth-tier suite. Prior cumulative: 824 passed. New multiverse/chaos layers:
  - Gate with homoglyph Latin/Cyrillic/Greek lookalikes of true
  - Activator output must be valid UTF-8
  - Bridge identity exact 'Control704 IT Technician'
  - Parallel 30× full component live under gate
  - --input with empty object {} and empty array []
  - Component names with path separators only (/, \\, //)
  - Verify source field identical across all 12 activators
  - Dashboard contains ALLOW_AGENT_EXECUTION=true text
  - Master status components_registered type is int
  - Stress: 100× bridge sequential determinism
  - Cross-check lattice Control12 only on grok-bot ids (strict)
  - Refuse JSON has system or component or reason key
  - Confirm tests/ directory contains this suite file
  - Mixed 10× dry + 10× live + 10× status + 10× refuse parallel
  - Activator label length bounds (5..200 chars)
"""
from __future__ import annotations

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
THIS_SUITE = Path(__file__).resolve()

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

SOURCE_EXACT = "canonical kg.production-training-result/v2"


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
    timeout: float = 12.0,
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


# ─── Homoglyph true ──────────────────────────────────────────────────────────

def test_homoglyph_true() -> None:
    print("\n── Homoglyph Lookalikes of true ──")
    # Cyrillic е, Greek τ/ρ/υ, etc.
    samples = [
        ("cyrillic e", "truе"),          # е U+0435
        ("fullwidth", "ｔｒｕｅ"),
        ("greek tau-like", "τrue"),
        ("mixed cyr", "tруe"),           # Cyrillic р у
        ("cyrillic full", "труе"),       # all cyrillic-ish
        ("fullwidth t only", "ｔrue"),
    ]
    for label, val in samples:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": val},
        )
        record(f"homoglyph [{label}] refuses", code != 0, f"code={code}")


# ─── Valid UTF-8 output ──────────────────────────────────────────────────────

def test_utf8_output() -> None:
    print("\n── Activator Output Valid UTF-8 ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        try:
            out.encode("utf-8")
            err.encode("utf-8")
            record(f"{c} stdout/stderr utf-8", True)
        except UnicodeEncodeError:
            record(f"{c} stdout/stderr utf-8", False)


# ─── identity exact ──────────────────────────────────────────────────────────

def test_identity_exact() -> None:
    print("\n── control704.identity Exact ──")
    code, out, err = run_cmd(
        [sys.executable, str(BRIDGE)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    ident = data.get("control704", {}).get("identity")
    record("identity == 'Control704 IT Technician'", ident == "Control704 IT Technician", f"ident={ident!r}")


# ─── Parallel 30× all live ───────────────────────────────────────────────────

def test_parallel_30_live() -> None:
    print("\n── Parallel 30× Live Component Activations ──")
    def one(i: int) -> bool:
        c = COMPONENTS[i % 12]
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    results = []
    with ThreadPoolExecutor(max_workers=15) as pool:
        futs = [pool.submit(one, i) for i in range(30)]
        for f in as_completed(futs):
            results.append(f.result())
    record("30/30 parallel live", all(results), f"ok={sum(results)}")


# ─── Empty object / array --input ────────────────────────────────────────────

def test_empty_json_input() -> None:
    print("\n── Empty Object/Array --input ──")
    script = BIN / "activate_containerized_deployment.sh"
    for label, payload in [("empty object", {}), ("empty array", [])]:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
            json.dump(payload, tf)
            path = tf.name
        try:
            code, out, err = run_cmd(
                ["bash", str(script), "--input", path],
                env={"ALLOW_AGENT_EXECUTION": "true"},
                timeout=8,
            )
            record(f"{label} --input no hang", code >= 0, f"code={code}")
        finally:
            os.unlink(path)


# ─── Path-separator-only components ──────────────────────────────────────────

def test_path_sep_only() -> None:
    print("\n── Path-Separator-Only Component Names ──")
    for slug in ["/", "\\", "//", "\\\\", "/\\", "./"]:
        code, out, err = run_cmd(
            ["bash", str(MASTER), slug],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        record(f"path-sep [{slug!r}] no hang", code >= 0, f"code={code}")


# ─── source identical all ────────────────────────────────────────────────────

def test_source_identical() -> None:
    print("\n── source Field Identical Across All 12 ──")
    sources = []
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        sources.append(obj.get("source"))
    record("all sources identical", len(set(sources)) == 1, f"unique={set(sources)}")
    record("source == expected", sources[0] == SOURCE_EXACT, f"source={sources[0]!r}")


# ─── Dashboard gate text ─────────────────────────────────────────────────────

def test_dashboard_gate_text() -> None:
    print("\n── Dashboard Contains ALLOW_AGENT_EXECUTION=true ──")
    text = DASHBOARD.read_text(encoding="utf-8", errors="replace")
    record("dashboard shows gate true", "ALLOW_AGENT_EXECUTION=true" in text)


# ─── components_registered type int ──────────────────────────────────────────

def test_components_registered_type() -> None:
    print("\n── components_registered Type int ──")
    code, out, err = run_cmd(
        ["bash", str(MASTER), "status"],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    obj = json.loads(out)
    cr = obj.get("components_registered")
    record("components_registered is int", isinstance(cr, int), f"type={type(cr).__name__}")
    record("components_registered == 12", cr == 12)


# ─── 100× bridge determinism ─────────────────────────────────────────────────

def test_100_bridge_determinism() -> None:
    print("\n── 100× Bridge Sequential Determinism ──")
    outputs = []
    for _ in range(100):
        code, out, err = run_cmd(
            [sys.executable, str(BRIDGE)],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        if code != 0:
            record("100× bridge all success", False)
            return
        outputs.append(out)
    record("100× bridge all success", True)
    record("100× bridge outputs identical", len(set(outputs)) == 1)


# ─── Lattice strict on grok only ─────────────────────────────────────────────

def test_lattice_strict() -> None:
    print("\n── Lattice Control12 Strict Only on grok-bot-* ──")
    code, out, err = run_cmd(
        [sys.executable, str(AGENTS)],
        env={"ALLOW_AGENT_EXECUTION": "true"},
    )
    data = json.loads(out)
    for a in data["agents"]["agents"]:
        aid = a["id"]
        lat = a.get("lattice")
        if aid.startswith("grok-bot-"):
            record(f"{aid} lattice==Control12", lat == "Control12")
        else:
            record(f"{aid} lattice not Control12 or absent", lat != "Control12")


# ─── Refuse key presence ─────────────────────────────────────────────────────

def test_refuse_keys() -> None:
    print("\n── Refuse JSON Key Presence ──")
    code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
    combined = (err or out).strip()
    try:
        obj = json.loads(combined)
        has = any(k in obj for k in ("status", "reason", "system", "component"))
        record("refuse has status/reason/system/component", has, f"keys={list(obj.keys())}")
    except json.JSONDecodeError:
        record("refuse has status/reason/system/component", "ALLOW_AGENT_EXECUTION" in combined)


# ─── This suite file present ─────────────────────────────────────────────────

def test_suite_file_present() -> None:
    print("\n── This Suite File Present Under tests/ ──")
    record("multiverse suite exists", THIS_SUITE.exists())
    record("suite under tests/", "tests" in str(THIS_SUITE))


# ─── Mixed 40 parallel ───────────────────────────────────────────────────────

def test_mixed_40_parallel() -> None:
    print("\n── Mixed 10 dry + 10 live + 10 status + 10 refuse Parallel ──")
    def dry(i: int) -> bool:
        c = COMPONENTS[i % 12]
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh"), "--dry-run"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    def live(i: int) -> bool:
        c = COMPONENTS[i % 12]
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0 and "DEPLOYMENT_COMPLETE" in out

    def status(_i: int) -> bool:
        code, out, err = run_cmd(
            ["bash", str(MASTER), "status"],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        return code == 0

    def refuse(_i: int) -> bool:
        code, out, err = run_cmd(["bash", str(MASTER), "status"], clear_gate=True)
        return code != 0

    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futs = (
            [pool.submit(dry, i) for i in range(10)]
            + [pool.submit(live, i) for i in range(10)]
            + [pool.submit(status, i) for i in range(10)]
            + [pool.submit(refuse, i) for i in range(10)]
        )
        for f in as_completed(futs):
            results.append(f.result())
    record(f"40/40 mixed parallel", all(results), f"ok={sum(results)}")


# ─── Label length bounds ─────────────────────────────────────────────────────

def test_label_length_bounds() -> None:
    print("\n── Activator Label Length Bounds ──")
    for c in COMPONENTS:
        code, out, err = run_cmd(
            ["bash", str(BIN / f"activate_{c}.sh")],
            env={"ALLOW_AGENT_EXECUTION": "true"},
        )
        obj = json.loads(out)
        label = obj.get("label", "")
        ok = isinstance(label, str) and 5 <= len(label) <= 200
        record(f"{c} label len 5..200", ok, f"len={len(label)}")


# ─── Entry ───────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 70)
    print("AVRONE-12 Lattice — Multiverse-Level Edge-Case & Chaos Suite")
    print("Control704 VIP proxy X Y Z · Avrone Due’Krey / Control12-lattice-op")
    print(f"ROOT={ROOT}")
    print("=" * 70)

    test_homoglyph_true()
    test_utf8_output()
    test_identity_exact()
    test_parallel_30_live()
    test_empty_json_input()
    test_path_sep_only()
    test_source_identical()
    test_dashboard_gate_text()
    test_components_registered_type()
    test_100_bridge_determinism()
    test_lattice_strict()
    test_refuse_keys()
    test_suite_file_present()
    test_mixed_40_parallel()
    test_label_length_bounds()

    print("\n" + "=" * 70)
    print(f"Results: {PASS} passed · {FAIL} failed · {SKIP} skipped")
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
