# Fusion integration analysis

Branch base: `8ffbcd655c9b4e0065d6934fa8469cb0f7a283b1` (v0.123.0) on `avrone-daemon`.

Reviewed, not copied:

| Repo | What was read | SHA |
| --- | --- | --- |
| [Avrone PR #20](https://github.com/doitupsolutions810-crow/Avrone/pull/20) | `GET`/`POST /api/blackboard`, `boot` on `/api/status`, 4096-byte cap before auth, previous-token grace | `8cca4083eb94da32240a17a5a7ce87db519cdf7b` |
| [CycleKernal PR #14](https://github.com/doitupsolutions810-crow/CycleKernal/pull/14) | `POST /v1/events`, mood still on `GET /v1/current_mood`, `boot` on `GET /health` | `97c9df6887a42242031d0afe1c5a43139509510c` |
| [GreatScale-Avrone-Grok-Fusion-Bridge](https://github.com/doitupsolutions810-crow/GreatScale-Avrone-Grok-Fusion-Bridge) `cursor/fusion-bridge-contracts-5b39` | `CONTRACT.md` (blob `51ada0ae5b631a36143fbc7bcba2e5eaa1f8d4bd`) and the relay in `bridge/avrone_cyclekernal_link.py` | `43050a281f85a98e06ec6f5fe54b79ac7a50f523` |

## Top finding

The relay already polls `GET /api/blackboard`, forwards each new post with `POST /v1/events` and `Idempotency-Key` set to that post's id, clips the UTF-8 JSON to 4096 bytes, and resends only when a peer's `boot` changes. Avrone PR #20 is that peer for the Avrone repo. CycleKernal PR #14 is the events intake. This daemon is the process that holds the squad bridges, and before this branch it exposed neither route nor a `boot` id. A relay pointed at it could see `GET /api/status` return 200 and still could not read a squad conclusion or tell that the process had restarted. That is the hop this branch adds. The watchdog is the part the relay does not have: one cycle that reports a dead hop, a bad token, an oversized body, a duplicate, or a peer restart, and does not raise.

## What was added

Nothing in `avrone/bridge.py` was rewritten. Squad posts stay on the per-run bridge.

- `GET /api/status` gains `boot`, a 32-hex id fixed for the life of the process. The fields that were already there are unchanged.
- `GET /api/blackboard` and `POST /api/blackboard` fan those bridges into the contract the relay speaks. Auth is the admin token, compared with the existing constant-time helper. `AVRONE_ADMIN_TOKEN_PREVIOUS` is accepted on this route only, with no whitespace trimming. Loopback and `AVRONE_PUBLIC_ACCESS=all` do not open it. A body over 4096 bytes is 413 `{"detail":"request body too large (max 4096 bytes)"}` with `Connection: close`, before the token is checked. The process-wide 16 MB cap is unchanged. An id that is stored, or already evicted from a ring, returns `duplicate: true` and is not rewritten. `since` follows `Bridge.export`: unknown returns the ring; a known id returns posts strictly after it. The first snapshot is oldest-timestamp first; later posts append, so a cursor cannot skip a post that arrives later with an older timestamp.
- `POST /api/fusion/watch` runs one watchdog cycle only when `FUSION_BOARD_URL`, `FUSION_EVENTS_URL`, and `FUSION_HEALTH_URL` are all set. Otherwise it returns `{"watched": false, "reason": "unset"}` and does not dial. It sends `Idempotency-Key` on `POST /v1/events`. It does not read or write the relay's `FUSION_STATE_PATH` (that file is version 2). Its own file is `AVRONE_FUSION_WATCH_PATH`.
- `GET /api/fusion` returns this process's `boot` and the last watch report. It does not dial.

Ids are remembered only after a 2xx or a permanent 4xx (400, 413, other non-auth 4xx). Timeouts, connection failures, 401/403, 404/405, and 5xx are retried. The first `boot` value does not resend. A later change forgets what was delivered to that peer. A missing `boot` leaves the saved set alone. Redirects are not followed, so the admin token is not sent to `Location`.

## Tests that were run

`python3 -m pytest -q tests/test_fusion_peer.py --tb=line`

23 passed in 4.76s. The file covers:

- bridge down (closed port; health down while the board is up; a directory used as the state path)
- duplicates (same id does not overwrite; an evicted id is not resurrected; a peer `duplicate: true` is not posted again; `Idempotency-Key` is not a second dedup key on the blackboard)
- oversized (4097 bytes is 413 before auth, including when the admin token is unset; exactly 4096 is accepted; 4096 bytes of junk is 400; 3000 `é` is 413 before the 4000-character clip; a body that cannot be shrunk is not dialed; a body that can be shrunk is cut with `...` and stays ≤ 4096; a peer 413 is remembered)
- bad auth (missing, wrong, trailing space, previous-token grace on the blackboard only, previous token rejected by `/api/terminal`, route stays 403 when the current token is unset, events 401 is not remembered and a corrected token is retried, a 302 is not followed)
- restart (`boot` stable across `/api/status` and `/api/blackboard` in one process; a new process prints a different id; a CycleKernal `boot` change, including an integer epoch, resends; a missing `boot` does not; a reloaded state file does not resend; a corrupt state file is discarded)

`python3 -m pytest -q -m "not integration" --tb=line`

This run was on the tree before `MASTER_PROMPT.md` was regenerated. Finished 11:43 PM America/Detroit on Nov 9, 2026. Result: **18 failed, 5963 passed, 2 skipped, 362 deselected**, 614.15s.

17 of those failures are `bash: line 1: python: command not found` in `tests/test_evolve.py` (2) and `tests/test_evolve_propose_robust.py` (15). This sandbox has `python3` and no `python`. With `python` on `PATH` pointing at `python3`, a rerun of `tests/test_master_prompt.py`, those two evolve tests, and `tests/test_evolve_propose_robust.py` was **49 passed** in 91.92s.

The 18th failure was `tests/test_master_prompt.py::test_committed_prompt_is_current`, because the generator lists every module and `fusion_peer.py` was new. `python3 tools/make_master_prompt.py` added that line, the new test-file count (298), and `AVRONE_ADMIN_TOKEN_PREVIOUS` / `AVRONE_FUSION_WATCH_PATH` to the env list. That test is included in the 49 passed above.

`python3 -m pytest -q tests/test_fusion_peer.py tests/test_robust_v032_body.py tests/test_core.py tests/test_tool_exposure.py tests/test_tool_hostview.py` was **50 passed** in 9.08s before the watch-path rename. The rename is covered by the later 23.

A non-ASCII token was not sent through HTTP. `httpx` refuses to encode `café` as an ASCII header. `token_eq("café", ...)` returns false for the real token and true for itself, and does not raise. That is the compare the route uses.

## Hardening pass

`python3 -m pytest -q tests/test_fusion_peer.py tests/test_fusion_peer_edges.py tests/test_master_prompt.py --tb=line`

**39 passed** in 6.82s. That is the original 23, 14 new edge tests, and the two prompt checks. The new cases, and the code changes they forced:

- A symlink journal, a symlink order file, and a FIFO order file do not block the fan-in and are not replaced. A symlink, FIFO, over-cap, or foreign-version state file is not followed and not overwritten. A version-2 state file keeps `version`, `cursor`, and `seen`.
- `408` and `429` are retried. A `404`, a non-JSON `200`, and a response larger than the read cap are not remembered. HTTP `413` still is.
- A token or URL containing CR, LF, or NUL is not dialed. `file://` is not opened.
- `NaN` timeout and non-finite `FUSION_TIMEOUT` fall back instead of hanging. Whitespace-only URLs count as unset. `GET /api/fusion` does not dial.
- A 400-digit `ts`, `true`, and `1e999` do not 500. More than 20 refs are clipped. A `run_id` of `../../etc/passwd` stays under `data_dir/bridge`. A 2000-deep JSON array is 400. A chunked body over 4096 bytes is 413 before auth. Eight threads posting one id store one post.
- An Avrone `boot` change drops `ck-*` ids and does not resend findings. A float, a bool, and a newline in `boot` are not a restart.
- The fan-in no longer raises when `bridge/` is a file. Accepting a post in that case raises `BridgeError` instead of an uncaught `NotADirectoryError`.
- One cycle still forwards at most `MAX_FORWARD` posts; the next cycle takes the rest. Echoes whose agent is `CycleKernal:Health`, or whose text embeds `"agent": "cyclekernal:mood"`, are not forwarded.

## Remaining gaps

- The watchdog is one shot. It does not fingerprint mood, open a circuit breaker, or enforce the relay's cycle deadline. One cycle forwards at most 200 new posts. Disk fan-in reads at most 256 journals.
- It does not start with the process, and this repo's compose file does not start the relay. Both URLs have to be set by the operator.
- `Idempotency-Key` is sent to CycleKernal. The blackboard dedupes on the post id and ignores the header, which is what the contract says PR #14 does.
- `POST /v1/events` was read from CycleKernal PR #14. It was not re-checked against that repo's default branch in this run.
- Nothing here produces `source: "greatscale"` or `kind: "evidence"`.
- The watchdog does not open `/ws/events` or `/ws/hud`.
- The watch route does not read `FUSION_STATE_PATH`. If `AVRONE_FUSION_WATCH_PATH` points at a version-2 file, that file is updated in place and its `cursor` is kept. A version other than 1 or 2 is left untouched.
