
## 5. Post-XCIV — Trust Gates (Descent XCV, 2026-11-05)

The scalar geometry is sound; the limiting technicality is that mathematical, cryptographic,
capability, and receipt channels remain separate and a consumer can treat an envelope as
accepted when any gate is missing. Descent XCV records this in `docs/DESCENT_XCV_TRUST_GATES.md`,
adds an optional default-off `trust_context` field to `evidence_snapshot` (records, never decides),
and adds `tests/test_xcv_trust_separation.py` asserting a perfect snapshot with a missing
capability is not authorized. Success criteria: all six independent gates must pass before
acceptance. Next merges remain ranked A–E above; the trust-gate wiring is the prerequisite
that makes them safe to compose.
