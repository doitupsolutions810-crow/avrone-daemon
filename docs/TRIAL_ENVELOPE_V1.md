# AVRONE trial envelope v1

## Completed trials

The \`avrone.trial/v1\` envelope represents only a completed pass or fail. Its stable
\`trial_id\` is SHA-256 over canonical identity fields: schema version, system, node,
run ID, check ID, check version, configuration digest, and attempt ID. The boolean
\`failed\` outcome is deliberately excluded. A replay of the same attempt therefore
has the same ID; a changed outcome or payload for that ID is a conflict, not a new sample.

System and node identifiers are NFC-normalized, trimmed, limited to 63 characters,
and reject C0/C1 controls, bidi/format characters, surrogates, and colon. This bounds
the composite \`source\` to 127 characters, within the current evidence pool's 128-character
limit, without truncation or source aliasing. Run, check, version, and attempt fields
are limited to 256 characters.

## Canonical configuration

Configuration is recursively NFC-normalized. Object keys must be strings and may not
collide after normalization. Cycles, unsupported Python values, excessive depth or
node count, non-finite numbers, unsafe integers, and canonical UTF-8 JSON over 16 KiB
are rejected. The reference encoder uses UTF-8, Unicode code-point key ordering,
compact separators, and finite JSON numbers. Other runtimes must pass the same golden
vectors before producing records; a JSON serialization library's default output is
not a cross-language canonicalization guarantee.

## Non-pass operational outcomes

Skipped, cancelled, timed-out, inconclusive, and blocked checks use the separate
\`avrone.trial-outcome/v1\` contract. They share the same identity derivation but do not
contain a boolean pass/fail field and are never projected into \`pool_trials\`. A later
completed result for the same attempt ID conflicts and is quarantined; a genuinely new
execution needs a new attempt ID.

## Statistical limits

A distinct ID proves only that the declared execution identity differs. It does not
prove independent sampling, equivalent environments, causal comparability, or
representative coverage. The evidence pool continues to report
\`independence: not_assessed\` unless a separately reviewed sampling design supports
a stronger statement.


## Cross-runtime canonicalization boundary

The v1 reference canonicalizer recursively NFC-normalizes configuration strings and keys, rejects
normalized-key collisions, limits integers to the interoperable safe-integer range, and serializes
with the AVRONE Python reference encoder (`json.dumps`, sorted keys, compact separators,
`ensure_ascii=False`, `allow_nan=False`). This produces stable identities in AVRONE and for
clients that use the same reference rules. It is **not** a declaration that every language's
default JSON encoder emits identical numeric bytes: for example, a runtime may serialize
`1.0`, `1e-7`, or negative zero differently. Non-Python producers must use a reviewed v1
compatibility implementation or send configuration through an AVRONE producer; do not calculate
the config digest with default `JSON.stringify`. A future language-neutral numeric canonicalizer
requires a new, versioned contract and shared test vectors.
