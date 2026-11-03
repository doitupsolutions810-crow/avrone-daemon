#!/usr/bin/env python3
"""Heat-weighted trim verification: 200-post hammer at p=1/2 plus edge cases.

Success criteria (derived from the mitigation):
  1. Retained mean heat > evicted mean heat under the inverted trim.
  2. Cone floor (p=0): heat_field == 0.0 for every post.
  3. Max pressure (p=1/2): heat scales with kind weight; conclusion > finding.
  4. Proportional budget: mean heat 6.25 -> full 50 posts; 2.25 -> floor 12.
  5. Old FIFO pathology: evicted mean heat > retained mean heat (regression guard).
"""
from __future__ import annotations

import importlib.util
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # repo root
PASS = FAIL = 0


def record(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    PASS += ok
    FAIL += (not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def load(name: str):
    path = ROOT / "avrone" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def main() -> int:
    hc = load("heat_contract")
    ht = load("heat_trim")

    # --- edge: cone floor ---
    for kind in hc.KIND_WEIGHT:
        post = {"kind": kind, "text": "x" * 40, "ts": time.time()}
        record(f"cone floor p=0 {kind}", hc.heat_field(post, 0.0) == 0.0)
        record(f"cone floor p=1 {kind}", hc.heat_field(post, 1.0) == 0.0)

    # --- edge: max pressure, conclusion > finding at p=1/2 ---
    t = time.time()
    c = hc.heat_field({"kind": "conclusion", "text": "z" * 80, "ts": t}, 0.5)
    f = hc.heat_field({"kind": "finding", "text": "z" * 80, "ts": t}, 0.5)
    record("max pressure conclusion > finding", c > f, f"c={c:.4f} f={f:.4f}")
    record("bernoulli peaks at 1/2", hc.bernoulli_heat(0.5) == 0.25)
    record("soft_contract bounds", hc.soft_contract(0) == 2.25 and hc.soft_contract(9) == 6.25)

    # --- 200-post hammer at p=1/2, limit 50 ---
    rng = random.Random(7)
    posts = {
        f"p{i:04d}": {
            "id": f"p{i:04d}",
            "kind": rng.choice(list(hc.KIND_WEIGHT)),
            "text": rng.choice(["a", "bb", "ccc", "dddd"]) * rng.randint(10, 200),
            "ts": t + i,
        }
        for i in range(200)
    }
    evicted = ht.heat_trim(posts, 50)
    stats = ht.eviction_stats(posts, evicted)
    record(
        "inverted trim: retained heat > evicted heat",
        stats["retained_mean_heat"] > stats["evicted_mean_heat"],
        f"retained={stats['retained_mean_heat']:.3f} evicted={stats['evicted_mean_heat']:.3f}",
    )

    # --- regression guard: plain FIFO would do the opposite ---
    # Simulate FIFO: drop the 150 oldest.
    fifo_evicted = list(posts.keys())[:150]
    fifo_stats = ht.eviction_stats(posts, fifo_evicted)
    record(
        "FIFO pathology (regression guard): evicted > retained",
        fifo_stats["evicted_mean_heat"] > fifo_stats["retained_mean_heat"],
        f"evicted={fifo_stats['evicted_mean_heat']:.3f} retained={fifo_stats['retained_mean_heat']:.3f}",
    )

    # --- proportional budget ---
    hot = [{"kind": "conclusion", "text": "Q" * 500} for _ in range(50)]
    cold = [{"kind": "finding", "text": "q" * 10} for _ in range(50)]
    hn, hb = ht.proportional_budget(hot)
    cn, cb = ht.proportional_budget(cold)
    record("hot budget near full cap", hn >= 40 and hb >= 50_000, f"n={hn} b={hb}")
    record("cold budget at floor", cn == 12 and cb == 12 * 1024, f"n={cn} b={cb}")

    print(f"\n=== Results: {PASS} passed, {FAIL} failed ===")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
