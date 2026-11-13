# Cross-system trial identity contract v1

## Purpose

AVRONE evidence_pool.pool_trials rejects duplicate trial IDs and reports source-orientation conflicts. This contract makes IDs deterministic across relays so retries and duplicate delivery cannot manufacture extra observations.

## Identity

trial_id = SHA256(canonical_json(identity)); identity includes schema version, explicit source system and node fields, source run ID, check ID and version, SHA-256 of canonical check configuration, and attempt ID.

The outcome is deliberately excluded. If a relay repeats the same attempt with a different outcome, the IDs collide and the pool rejects the contradiction instead of counting both. A genuinely new attempt must have a new attempt ID.

Canonical JSON uses sorted keys, compact separators, UTF-8, NFC-normalized bounded identity strings, and rejects NaN/Infinity and non-JSON configuration values. Configuration is limited to 16 KiB; identity fields are limited to 256 characters and reject control characters.

## Pool adapter

The returned envelope contains source, trial_id, and boolean failed fields and can be projected directly into pool_trials. Keep the full envelope in the event/receipt record for audit. Do not silently drop duplicate IDs or retry them as new observations.

## Trust and statistical limits

- A digest is identity, not authentication. Authenticate the sender with the existing signed transport/capability boundary; never trust a self-asserted system or node string.
- Distinct IDs do not prove independent sampling, equivalent environments, causal comparability, or representative coverage.
- Only completed pass/fail trials enter the pool. Skipped, cancelled, timed-out, or inconclusive runs remain separate operational outcomes; never coerce them to failed=false.
- Preserve source identity and check version.
- On duplicate ID with changed payload, surface a conflict and quarantine the conflicting relay. Never overwrite the first record.

## Adoption sequence

1. AVRONE producers call make_trial when a check attempt completes.
2. Bridge and fleet relays forward the entire envelope unchanged with origin metadata.
3. Grok Bot and CycleKernel adapters validate the schema and authenticate transport identity before accepting it.
4. Receiving AVRONE validates and stores envelopes idempotently, then projects accepted unique trials into pool_trials.
5. Great Scale consumes the pooled snapshot and retains independence: not_assessed unless a separately reviewed sampling design establishes more.

This contract is additive. It does not alter bridge journals, fleet wire messages, APIs, or existing evidence semantics until each adapter is integrated and independently tested.