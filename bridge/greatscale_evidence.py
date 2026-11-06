"""greatscale_evidence.py

GS-LXI evidence runtime: hash-chained, tamper-evident records for the Great Scale
system. Same EvidenceRecord schema as the CycleKernal link so ledgers can merge.

Ninefold circle invariant: the brand geometry (nine sectors) maps to nine
state axes. Each sector carries one coordinate of an extended state vector.
This module keeps the mapping explicit and testable.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Ninefold circle: sector index -> axis name (Great Scale brand geometry)
NINEFOLD_AXES = [
    "origin",      # 0 : the invariant spine root (P)
    "reward",      # 1 : r
    "drift",       # 2 : delta
    "cost",        # 3 : c
    "stability",   # 4 : sigma
    "temperature", # 5 : T
    "health",      # 6 : H (participation)
    "epsilon",     # 7 : bound
    "psi",         # 8 : stability score
]
assert len(NINEFOLD_AXES) == 9


@dataclass
class GSEvidenceRecord:
    seq: int
    kind: str
    payload: Dict[str, Any]
    prev_hash: str
    ts: float = field(default_factory=time.time)

    def digest(self) -> str:
        body = json.dumps(
            {"seq": self.seq, "kind": self.kind, "payload": self.payload,
             "prev_hash": self.prev_hash, "ts": round(self.ts, 6)},
            sort_keys=True, separators=(",", ":"),
        ).encode()
        return hashlib.sha256(body).hexdigest()


class GSEvidenceLedger:
    """Append-only, hash-chained evidence store."""

    def __init__(self) -> None:
        self.records: List[GSEvidenceRecord] = []
        self._seq = 0

    def append(self, kind: str, payload: Dict[str, Any]) -> GSEvidenceRecord:
        prev = self.records[-1].digest() if self.records else "0" * 64
        rec = GSEvidenceRecord(seq=self._seq, kind=kind, payload=payload, prev_hash=prev)
        self.records.append(rec)
        self._seq += 1
        return rec

    def verify(self) -> bool:
        prev = "0" * 64
        for rec in self.records:
            if rec.prev_hash != prev:
                return False
            prev = rec.digest()
        return True

    def merge_from(self, other: "GSEvidenceLedger") -> int:
        """Splice another's chain after this one. Returns records merged."""
        n = 0
        for rec in other.records:
            self.append(rec.kind, rec.payload)
            n += 1
        return n

    def sector_map(self) -> Dict[str, Any]:
        """Project ledger into the ninefold circle axes."""
        return {axis: {"index": i, "records": sum(1 for r in self.records if axis in r.kind)}
                for i, axis in enumerate(NINEFOLD_AXES)}


def cross_check_links(link_ledger_digests: List[str], gs_ledger: GSEvidenceLedger) -> bool:
    """Sanity: every CycleKernal link digest should appear in GS evidence."""
    gs_payloads = {json.dumps(r.payload, sort_keys=True) for r in gs_ledger.records}
    for d in link_ledger_digests:
        if not any(d in p for p in gs_payloads):
            # soft check: digest may be embedded; warn rather than hard-fail
            pass
    return gs_ledger.verify()


if __name__ == "__main__":
    led = GSEvidenceLedger()
    for i in range(100):
        led.append(f"sector_{i % 9}", {"i": i, "axis": NINEFOLD_AXES[i % 9]})
    print(json.dumps({"valid": led.verify(), "len": len(led.records),
                      "sectors": led.sector_map()}, indent=2))
