import math

class GateError(ValueError):
    pass

def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)

def _finite(x, name):
    if not _is_num(x):
        raise GateError(f"{name} not a number: {x!r}")
    if not math.isfinite(x):
        raise GateError(f"{name} non-finite: {x!r}")
    return float(x)

def evolve(P_old, dP, eps=0.5, H=1.0, floor=0.0, genesis=False):
    """Bounded evolution gate. Contract: on STEPS (delta), not trajectories."""
    if not isinstance(P_old, dict) or not isinstance(dP, dict):
        raise GateError("P_old and dP must be dicts")
    eps = _finite(eps, "eps")
    if eps < 0:
        raise GateError(f"eps must be non-negative, got {eps}")
    if eps == 0:
        norm0 = math.sqrt(sum(v*v for v in (dP.get(k,0.0) for k in ("r","delta","c","sigma","T"))))
        if norm0 > 0:
            raise GateError("eps=0 rejects nonzero step")
    H = _finite(H, "H"); floor = _finite(floor, "floor")
    r = _finite(P_old.get("r", 0.0), "r")
    delta = _finite(P_old.get("delta", 0.0), "delta")
    c = _finite(P_old.get("c", 0.0), "c")
    sigma = _finite(P_old.get("sigma", 0.0), "sigma")
    T = _finite(P_old.get("T", 0.0), "T")
    dr = _finite(dP.get("r", 0.0), "dr"); dd = _finite(dP.get("delta", 0.0), "ddelta")
    dc = _finite(dP.get("c", 0.0), "dc"); ds = _finite(dP.get("sigma", 0.0), "dsigma")
    dT = _finite(dP.get("T", 0.0), "dT")
    norm = math.sqrt(dr*dr + dd*dd + dc*dc + ds*ds + dT*dT)
    if norm > eps and norm > 0:
        s = eps / norm
        dr, dd, dc, ds, dT = dr*s, dd*s, dc*s, ds*s, dT*s
        norm = eps
    r2, d2, c2, s2, t2 = r+dr, delta+dd, c+dc, sigma+ds, T+dT
    psi_v = H - s2
    if not genesis and psi_v < floor:
        raise GateError(f"psi-collapse: psi={psi_v:.6f} < floor={floor}")
    return {"r": r2, "delta": d2, "c": c2, "sigma": s2, "T": t2,
            "eps": eps, "H": H, "floor": floor, "norm": norm, "psi": psi_v}

def psi(P, H=None):
    H = P.get("H", 1.0) if H is None else H
    return H - P["sigma"]
