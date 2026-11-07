# Descent XV — Lesson Accumulator (2026-11-07)

## Finding

`distill_lessons` treated every re-derivation as a discard (`if key in have: continue`).
That is the heat-blind LRU of Descent II: a cone-floor truth re-found 40 times never
gains evidence. Memory could ADD lessons but could not REINFORCE them.

## Mitigation (success criteria)

1. On re-derivation: confirmations += 1, refresh ts, attach evidence snapshot
   (failures=0, runs=confirmations → S descends toward 0, Psi toward CONE).
2. Return value still lists only *new* keys (backward compatible).
3. `relevant_lessons` ranks by keyword, then higher confirmations, then colder heat.

## Code

- `avrone/lesson_accumulator.py` — reinforce_or_create + rank_score
- `avrone/bridge.py` — distill_lessons / relevant_lessons call the helper

## Verification

- Existing lesson tests green (dedupe, relevance, off/env).
- New: test_lessons_reinforce_on_rederive, test_relevant_prefers_confirmed_colder_lessons.
- evidence_pool dual-channel suite: 28 passed.
- Focused bridge+pool: 31 passed.

## Mathematics

For a pure confirmation path: p = 0, S = 0 after any N on the raw channel;
Jeffreys p_J = 0.5/(N+1), S_J = p_J(1-p_J) → 0 as N grows. Psi_J → CONE = φ⁻².

The system finally buys the runs the billing lock could not afford — in memory.
