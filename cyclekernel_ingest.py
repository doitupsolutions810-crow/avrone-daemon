"""CycleKernal ingest: OracleFeed.accepted as authoritative state transition.

One linear relation — psi + sigma = H — enforced at two ends:
  input valve  (psi_checksum)  rejects bad inbound scalars
  output valve (evolve)        rejects bad outbound steps
Hash chain makes both auditable after the fact.

This module is the bridge: it takes an accepted sigma from OracleFeed
and maps it onto CycleKernal's CognitiveState so the Evo-Psych trait
layer never sees an unvalidated coordinate.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --- spine (cycle_link) ---------------------------------------------------
_spec = importlib.util.spec_from_file_location("cycle_link", "cycle_link.py")
_mod = importlib.util.module_from_spec(_spec)
sys.modules["cycle_link"] = _mod
_spec.loader.exec_module(_mod)
evolve = _mod.evolve
GateError = _mod.GateError
psi_fn = _mod.psi

# --- oracle checksum (oracle_checksum) -------------------------------------
_spec2 = importlib.util.spec_from_file_location("oracle_checksum", "oracle_checksum.py")
_mod2 = importlib.util.module_from_spec(_spec2)
sys.modules["oracle_checksum"] = _mod2
_spec2.loader.exec_module(_mod2)
psi_checksum = _mod2.psi_checksum
OracleFeed = _mod2.OracleFeed

FLOOR = 0.1
EPS = 0.5
H_DEFAULT = 2.0


def record_hash(payload: str, prev: Optional[str]) -> str:
    blob = f"{prev or 'GENESIS'}|{payload}"
    return hashlib.sha256(blob.encode()).hexdigest()


def ingest_to_cyclekernel(feed: OracleFeed, bridge_state: Dict[str, Any]) -> Dict[str, Any]:
    """The merge: every sigma OracleFeed accepts becomes a validated
    coordinate on CycleKernal's cognitive state. Unvalidated scalars
    never cross this line."""
    out = dict(bridge_state)
    receipts = []
    for sigma in feed.accepted:
        ok, psi_v = psi_checksum(sigma, feed.H, feed.floor)
        assert ok
        prev = receipts[-1]["hash"] if receipts else None
        h = record_hash(f"sigma={sigma:.6f}|psi={psi_v:.6f}", prev)
        receipts.append({"sigma": sigma, "psi": round(psi_v, 6), "hash": h})
    out["receipts"] = receipts
    out["last_sigma"] = feed.accepted[-1] if feed.accepted else None
    out["invariant"] = "psi + sigma = H (linear, half-space safe)"
    return out
