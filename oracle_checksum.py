import math

FLOOR = 0.1
H_DEFAULT = 2.0


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite(v):
    if not math.isfinite(v):
        raise ValueError(f"non-finite value: {v!r}")


def psi_checksum(sigma, H=H_DEFAULT, floor=FLOOR):
    """Input valve. Returns (ok, psi). Rejects non-numbers and non-finite values."""
    if not _is_num(sigma) or not _is_num(H) or not _is_num(floor):
        raise ValueError("type error: sigma, H, floor must be numbers")
    _finite(sigma); _finite(H); _finite(floor)
    psi = H - sigma
    return psi >= floor, psi


class OracleFeed:
    """Input valve with receipt. accepted / rejected are the audit trail."""
    def __init__(self, H=H_DEFAULT, floor=FLOOR):
        self.H = H
        self.floor = floor
        self.accepted = []
        self.rejected = []

    def ingest(self, sigma):
        ok, psi = psi_checksum(sigma, self.H, self.floor)
        if not ok:
            self.rejected.append(sigma)
            raise ValueError(f"psi-collapse: psi={psi:.4f}")
        self.accepted.append(sigma)
        return True
