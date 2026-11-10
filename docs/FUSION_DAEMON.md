# Fusion seam on avrone-daemon

AVRONE stays the orchestration process. The Fusion Bridge repository is a
separate relay and is not written by this change. Nothing here starts a
background poller or a container.

## What this process now serves

| Method | Path | Auth | Cap |
| --- | --- | --- | --- |
| GET | `/api/status` | public | unchanged, plus `boot` |
| GET | `/api/blackboard` | `X-Admin-Token`, current or `AVRONE_ADMIN_TOKEN_PREVIOUS` | none |
| POST | `/api/blackboard` | same, checked after the body cap | 4096 bytes before auth |

`boot` is a 32-hex id created when the process imports `avrone.server`. It
changes when the process starts again. A missing boot on an older peer is not
invented by clients of this field.

Post dedup on the blackboard is the post id. The first write is kept. An
evicted id is a duplicate and is not put back on the ring. `since` walks that
fan-in in accept order. The first snapshot of posts already on disk is oldest
timestamp first; later posts append, so an older timestamp accepted after the
cursor is still returned.

The process-wide body cap stays `AVRONE_MAX_BODY_BYTES` (16 MB default). Only
`/api/blackboard` uses 4096.

## What this process can send

`avrone.fusion_deliver.Ledger` posts pending events to `CK_BASE` +
`CK_EVENTS_PATH` (default `http://127.0.0.1:8090/v1/events`) with
`Idempotency-Key` set to the event id. The caller invokes `pump`. The server
does not.

Unique keys are `(source, id)`. A second source with the same id is a second
event. A second payload for the same key is ignored.

    A = D + P + F

A is keys admitted to the ledger, D is HTTP 2xx (including `duplicate: true`),
P is still pending, F is an explicit permanent failure. Retries add to
`attempts` and do not change A. Echoes (`fusion-bridge`, `cyclekernal-relay`,
`ck-mood-` / `ck-health-` / `ck-evidence-`) are suppressed and are not part of
A.

429 and 500 use the same exponential delay from `FUSION_BACKOFF_BASE`
(0.5 s) to `FUSION_BACKOFF_MAX` (8 s). A 429 with `Retry-After` of a
delta-seconds value in (0, 30] sleeps that long instead. `0`, words, signs,
HTTP-dates, and numbers above 30 are ignored, and a computed delay of 0 is
raised to 0.05 s. Neither status opens the circuit. 404 and 405 wait
`FUSION_INTERVAL`. A timeout or a refused connection opens the breaker
(`FUSION_BREAKER_THRESHOLD`, default 1) for `FUSION_BREAKER_COOLDOWN` (30 s).
401 and 403 leave the key pending. Each cycle stops at
`FUSION_CYCLE_DEADLINE` (10 s) and will not start a call with under 50 ms
left. `pump` stops after `max_cycles` (default 8).

`heat` and `cone_angle` must be finite JSON numbers. `cone`, when present, is
held to the same rule. `beta_t` may be a finite number or a string without
Cc/Cf/Cs characters; a string that is itself a JSON object or array is
rejected. A bool is not a number. An oversized value is an explicit failure
and is not stored. These are syntax checks. They are not a temperature, an
angle, or the Bernoulli identity.

A CycleKernal `boot` change moves delivered `avrone` and `greatscale` keys
back to pending. A health body with no `boot` changes nothing. Version-1
ledgers (`seen` as a list of ids) still load.

`127.0.0.1`, `::1`, `localhost`, and `::ffff:127.0.0.1` share one breaker
bucket per port. `127.0.0.2` stays remote for the HTTP access policy. The
doctor and the proxy bypass still treat the whole 127.0.0.0/8 block as
loopback; that wider check now unwraps the mapped form too.

## What was inspected and not wired as execution

CycleKernal PR #14 is merged on `main` as `642bd646` (2026-11-10 04:23 UTC).
That main serves `POST /v1/events` and `POST /events`. capability-authority
`main` (`012dc7d5`) still answers `/v1/execute` with `result: executed` and
no runner, and its default port is 8090, which is CognitiveBridge's port.
PR #17 (`ee13c32d`, open, not merged) uses port 8095 and returns
`NOT_IMPLEMENTED` except for `Execute:artifact.write_demo:POST`. This daemon
does not call `/v1/execute` and does not treat the unscoped stub as a delivery.

`avrone.fusion_plane.classify_execute` is the classifier for those shapes.
It does not perform an action.
