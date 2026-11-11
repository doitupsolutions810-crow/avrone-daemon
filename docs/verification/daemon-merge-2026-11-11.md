# Daemon integration reconciliation — 2026-11-11

This candidate merges the reviewed integration branch with the fetched main line
while retaining both sets of persistence, redaction, bridge and evidence changes.

| Role | Exact source |
| --- | --- |
| Integration parent | [49f476e0e9dc555e37da6fb32bbafd52b8469c6c](https://github.com/doitupsolutions810-crow/avrone-daemon/commit/49f476e0e9dc555e37da6fb32bbafd52b8469c6c) |
| Main-line parent | [c1369c34f58fabff2095e2f5a5e846d9056c7f86](https://github.com/doitupsolutions810-crow/avrone-daemon/commit/c1369c34f58fabff2095e2f5a5e846d9056c7f86) |

Four merge conflicts required review: the generated handoff, provenance pooling,
loopback identity and plane-response classification.

## Contract decisions

- Provenance still rejects control characters, bidi markers and lone surrogates
  before normalization. Main's safe NFKC, invisible-format removal,
  case-insensitive source grouping, globally unique canonical trial IDs, raw
  length limits and hostile-Mapping checks remain. Newline/tab/bidi inputs
  cannot become newly accepted trials.
- Peer host strings containing whitespace remain refused. Plain ASCII byte
  host inputs from main are supported; malformed brackets, non-IPv6 zones and
  non-ASCII host spellings remain refused. Main's guarded default-port handling,
  trailing-root-dot rules for the separate broad-loopback check, and
  unambiguous remote IPv6 keys remain.
- HTTPS keeps the integration branch's explicit scheme namespace, including
  when HTTP and HTTPS use the same non-default port. The remote hostname
  `loopback` cannot collide with actual loopback peers. Omitted HTTP(S) ports
  now match explicit 80/443, respectively.
- Plane success requires exact string status fields and a non-contradictory
  body. Errors use stable `reason: "error"` plus bounded sanitized `detail`;
  untrusted error text never becomes a success-like reason code. Foreign
  equality hooks are not invoked during error classification.
- Main's job queue validation, secret scrubbing, terminal escaping, log safety,
  numeric argument parsing and additional regression suites are retained.
  The existing Runtime bridge, giant-integer log handling, evolution
  interpreter selection and newer branch fixes are retained.

Eight inherited test expectations conflicted with these combined contracts.
Tests were updated precisely: unsafe normalized variants are refused before
deduplication, whitespace peers move from the allowed to the refused set,
omitted HTTP ports use 80, and HTTPS keys retain their scheme namespace.
All other existing guards remain.

## Validation

- Before expectation reconciliation: **646 passed, 8 failed**; all eight
  failures matched the contract differences above.
- After reconciliation and eleven combined-boundary regressions:
  **667 passed**, one existing Starlette/httpx deprecation warning, 10.34 seconds.
- Python compile checks and staged whitespace checks passed.
- The complete merged host suite is a separate check; a focused result does
  not substitute for that full run or for deployment verification.

Focused command:

```sh
python -m pytest -q tests/test_evidence_pool*.py tests/test_fusion*.py \
  tests/test_merge_security_boundaries.py tests/test_master_prompt.py
```

No production configuration, credentials, deployment or main-branch publication
is changed by this candidate.

## Pinned main-line refresh and portable offline fixture

A second bounded refresh includes main commit
[096f562841e797c50b4117387511d735016270b4](https://github.com/doitupsolutions810-crow/avrone-daemon/commit/096f562841e797c50b4117387511d735016270b4)
on top of published integration commit
[592ae044fd7b7dcb43a65abf1f8a2a15a2144bb3](https://github.com/doitupsolutions810-crow/avrone-daemon/commit/592ae044fd7b7dcb43a65abf1f8a2a15a2144bb3).
It adds the newer search/lesson-forget hardening and associated tests. The
scanner conflict preserves the existing zero/negative-budget no-I/O boundary
and exclusion of symlink directories while retaining strict typed limits,
newline-only line numbers, BOM/CRLF handling and bounded previews.

The initial merged-tree full run completed with **8,269 passed, one failed,
two skipped**, 69 warnings, in 823.46 seconds. The sole failure was
`test_offline_rerun_with_deps_installed_continues`: the test's fake virtual
environment used a nested symlink to the running interpreter. That lost its
actual virtual-environment identity and selected the base interpreter without
pytest, so the production dependency preflight correctly refused to continue.

The test fixture now uses a shell-quoted exec wrapper for the actual
`sys.executable`. Production offline dependency checking is unchanged.
The offline-positive, missing-dependency-negative, broken-environment rebuild,
new-main scanner/lesson-forget and handoff checks passed together:
**110 passed**, zero failures, 16.91 seconds.

These refreshed sources have focused verification; the earlier full run is
recorded with its one fixture failure and is not relabeled as a clean full pass.
