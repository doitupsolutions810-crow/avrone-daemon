import math, random, pytest
from cycle_link import evolve, GateError, psi
from evidence import EvidenceRecord

def test_finite_nan():
    with pytest.raises(GateError, match="non-finite"):
        evolve({"sigma": 0.0}, {"sigma": float("nan")})

def test_finite_inf():
    with pytest.raises(GateError, match="non-finite"):
        evolve({"sigma": 0.0}, {"sigma": float("inf")})

def test_type_str():
    with pytest.raises(GateError, match="not a number"):
        evolve({"sigma": "bad"}, {"sigma": 0.1})

def test_type_none():
    with pytest.raises(GateError, match="not a number"):
        evolve({"sigma": None}, {"sigma": 0.1})

def test_clamp_large_step():
    p = evolve({"sigma": 0.0}, {"sigma": 100.0}, eps=0.5)
    assert abs(p["sigma"] - 0.5) < 1e-12

def test_psi_floor_breach():
    with pytest.raises(GateError, match="psi-collapse"):
        evolve({"sigma": 0.6}, {"sigma": 1.0}, eps=0.5, H=1.0, floor=0.0)

def test_psi_floor_ok():
    p = evolve({"sigma": 0.1}, {"sigma": 0.2}, eps=0.5, H=1.0, floor=0.0)
    assert p["psi"] == 0.7

def test_genesis_bypass():
    with pytest.raises(GateError, match="psi-collapse"):
        evolve({"sigma": 0.6}, {"sigma": 1.0}, eps=0.5, H=1.0, floor=0.0)
    p = evolve({"sigma": 0.6}, {"sigma": 1.0}, eps=0.5, H=1.0, floor=0.0, genesis=True)
    assert p["psi"] < 0.0

def test_evidence_chain():
    a = EvidenceRecord({"v": 1})
    b = EvidenceRecord({"v": 2}, prev_hash=a.hash)
    b.verify(a)
    c = EvidenceRecord({"v": 3}, prev_hash=b.hash)
    c.verify(b)

def test_evidence_tamper():
    a = EvidenceRecord({"v": 1})
    b = EvidenceRecord({"v": 2}, prev_hash=a.hash)
    b.payload["v"] = 99
    with pytest.raises(ValueError):
        b.verify(a)

def test_duplicate_payload_distinct_ts():
    a = EvidenceRecord({"v": 1}, ts=100.0)
    b = EvidenceRecord({"v": 1}, ts=101.0)
    assert a.payload == b.payload and a.hash != b.hash

def test_long_chain_5000():
    prev = EvidenceRecord({"i": 0})
    for i in range(1, 5001):
        rec = EvidenceRecord({"i": i}, prev_hash=prev.hash)
        rec.verify(prev)
        prev = rec
    assert prev.payload["i"] == 5000

def test_many_steps_r_accumulates():
    p = {"r": 0.0, "delta": 0.0, "c": 0.0, "sigma": 0.0, "T": 0.0}
    for _ in range(50):
        p = evolve(p, {"r": 0.1}, eps=0.2)
    assert p["r"] == pytest.approx(5.0, abs=1e-9)

def test_sigma_only_axis():
    p = evolve({"r": 5, "delta": 5, "c": 5, "sigma": 0.2, "T": 5},
               {"r": 0, "delta": 0, "c": 0, "sigma": 0.3, "T": 0}, eps=3.0)
    assert p["psi"] == pytest.approx(1.0 - 0.5)
    assert p["r"] == 5 and p["T"] == 5

def test_concurrent_style_sequential():
    p = {"sigma": 0.0}
    for i in range(100):
        p = evolve(p, {"sigma": 0.01}, eps=0.05, H=10.0, floor=-100.0)
    assert p["sigma"] == pytest.approx(1.0, abs=1e-9)

def test_adversarial_alternating():
    p = {"sigma": 0.25}
    for s in [1.0, -1.0, 1.0, -1.0]:
        try:
            p = evolve(p, {"sigma": s}, eps=0.5, H=1.0, floor=0.0)
        except GateError:
            pass
    assert p["sigma"] <= 0.75

def test_fuzz_random_steps():
    rng = random.Random(42)
    p = {"r": 0.0, "delta": 0.0, "c": 0.0, "sigma": 0.0, "T": 0.0}
    for _ in range(200):
        dP = {k: rng.uniform(-2, 2) for k in p}
        try:
            p = evolve(p, dP, eps=0.3, H=1.0, floor=-10.0)
        except GateError:
            pass
    assert all(math.isfinite(v) for v in p.values())

def test_evidence_to_dict_roundtrip_keys():
    r = EvidenceRecord({"a": 1, "b": [2, 3]})
    d = r.to_dict()
    assert set(d) == {"payload", "prev_hash", "ts", "hash"}

def test_psi_helper():
    assert psi({"sigma": 0.3}, H=1.0) == 0.7

def test_eps_zero_rejects_nonzero():
    with pytest.raises(GateError):
        evolve({"sigma": 0.0}, {"sigma": 0.1}, eps=0.0)

def test_negative_eps():
    with pytest.raises(GateError):
        evolve({"sigma": 0.0}, {"sigma": 0.1}, eps=-1.0)

def test_bool_rejected():
    with pytest.raises(GateError):
        evolve({"sigma": True}, {"sigma": 0.1})

def test_deep_descent_10000_iterations():
    p = {"r": 0.0, "delta": 0.0, "c": 0.0, "sigma": 0.0, "T": 0.0}
    accepted = rejected = 0
    for i in range(10000):
        try:
            p = evolve(p, {"sigma": 0.02 if i % 3 else -0.05,
                            "r": 0.01, "c": -0.01}, eps=0.1, H=2.0, floor=0.0)
            accepted += 1
            assert p["psi"] >= 0.0
        except GateError:
            rejected += 1
    assert accepted + rejected == 10000
    assert all(math.isfinite(v) for v in p.values())
    assert p["psi"] >= 0.0

def test_hash_chain_monotonic_ts():
    prev = EvidenceRecord({"i": 0}, ts=1000.0)
    for i in range(1, 200):
        rec = EvidenceRecord({"i": i}, prev_hash=prev.hash, ts=1000.0 + i)
        rec.verify(prev)
        assert rec.ts > prev.ts
        prev = rec

def test_psi_null_axes_invariant():
    """r, delta, c, T changes never affect psi."""
    p = evolve({"r": 1, "delta": 2, "c": 3, "sigma": 0.4, "T": 4},
               {"r": 10, "delta": -10, "c": 7, "sigma": 0.0, "T": -4}, eps=20.0, H=1.0)
    assert p["psi"] == 0.6

def test_clamp_then_reject_closure():
    """Every terminal state satisfies epsilon invariant."""
    p = {"sigma": 0.0}
    for _ in range(1000):
        try:
            p = evolve(p, {"sigma": random.uniform(-5, 5)}, eps=0.3, H=1.0, floor=0.0)
        except GateError:
            pass
    assert p["psi"] >= 0.0
