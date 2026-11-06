# Great Scale Descents — findings mapped onto the residual coordinate

Each entry follows the same form: the mathematics is stated, the measured
evidence is plugged in, and the result is mapped with `avrone.greatscale`
(S = heat(p) = p(1-p) is the check's Bernoulli outcome variance,
Psi = CONE + S with CONE = phi^-2 ~ 0.381966, and a defect of rate p is
exposed by N independent runs with probability 1 - (1-p)^N, so 95% detection
needs N >= ln(0.05)/ln(1-p)).

Like the upstream great-scale repository, these are mathematical embeddings
of measured evidence, not physical or empirical claims.

## Descent I — The Billing-Lock Cone (2026-11-03)

**Finding.** The verification infrastructure itself — CI runners, their run
budget, and the billing lock that gates them — is a Bernoulli process with a
consumable budget, and it maps onto the Great Scale exactly like any check.
During the 2026-10-28 boot-blocker diagnosis the account's Actions allowance
allowed roughly N = 1 executed run per billing unlock. Under a budget of N runs,
a defect of rate p is found with probability D(p, N) = 1 - (1-p)^N, so with
N = 1 the detection probability is D(p, 1) = p. Corollaries:

1. Only deterministic defects are detectable under the lock. 95% confidence
   needs N >= ln(0.05)/ln(1-p): p = 1/2 -> 5 runs, p = 1/10 -> 29 runs,
   p = 1/200 -> 598 runs. A lock regime of one run per epoch can never afford
   them; its detectable set is exactly {p = 0, p = 1} — the cone floor.
2. The lock therefore FORCES convergence of engineering onto the cone floor:
   every root cause worth finding must be made deterministic (a named,
   repeatable failure) and instrumented, so a single run yields the whole
   diagnosis. This session's measured evidence confirms the selection effect —
   every defect found was 100% reproducible:

   | Check (measured 2026-10-28) | f/r | p | S=heat(p) | 4S | Psi | cone floor | orientation |
   | --- | --- | --- | --- | --- | --- | --- | --- |
   | grok-bot boot (socket-proxy 403 on /info) | 3/3 | 1 | 0 | 0 | 0.381966 | yes | fail |
   | Avrone pip (delivery wrapper broke TOML line 1, col 14) | 2/2 | 1 | 0 | 0 | 0.381966 | yes | fail |
   | Avrone pytest (missing tests package marker) | 2/2 | 1 | 0 | 0 | 0.381966 | yes | fail |
   | Avrone suite after fixes (both 3.11/3.12 legs) | 0/2 | 0 | 0 | 0 | 0.381966 | yes | pass |
   | contrast: a maximally flaky check | 1/2 | 1/2 | 1/4 | 1 | 0.631966 | no | mixed |
   | contrast: rare defect p = 1/200 | 1/200 | 0.005 | ~0.005 | ~0.02 | ~0.3869 | no | mixed |

   All four session defects sit exactly on Psi = CONE = phi^-2; none carries
   residual heat. A flaky defect would have been invisible to this session.

3. The two convergent mitigations are the two mathematically optimal moves
   under a run-budget constraint:
   - **Maximise information per run.** Before instrumentation a run yields
     1 bit (pass/fail); with in-run diagnostics (::error:: annotations carrying
     the full error tail) one run yields the entire traceback — raising the
     per-run information yield from 1 bit toward the entropy of the error
     text. A deterministic instrumented defect is then found in exactly one
     run (runsFor95 = 1), which the lock budget affords.
   - **Escape the budget.** `verify.sh` runs the identical CI sequence
     (config check, full-profile boot with the ready:true gate, the 8 live
     tests, the evolution loop, the AVRONE suite) on any Docker host with
     N unlimited — the constraint that forces cone-floor selection simply
     disappears, and flaky defects (p < 1) become measurable again through
     repeated runs.

**Consequence for the merged system.** The billing lock — the meta-system
that runs the checks — is itself a Great Scale object: the lock regime's
detectable set is the cone floor {0, 1}, its residual heat budget is zero,
and its orientation channel (which side of the floor a run lands on) is the
only signal it can transmit per epoch. The bridge between AVRONE and Grok Bot
inherits this: contract checks executed under the lock must be deterministic
and self-describing on failure, and anything statistical belongs on a host
where N can grow. This is why the swarm-executor contracts run as assertions
with named causes, not as flaky retries.

## Descent II — The Multiplicative Chain: Layered Systems Select for Determinism Compulsively (2026-11-06)

**Finding.** Descent I found the cone law in one layer — the run budget. The
deeper finding is that the merged system is not one Bernoulli gate but a CHAIN
of them, and transmissions across the chain compose MULTIPLICATIVELY. Read in
`avrone/bridge.py`, the memory path of a defect lesson passes four gates:

    G1 detection      a defect of rate p is exposed in N runs: D(p,N) = 1-(1-p)^N
    G2 distillation   distill_lessons() keeps only the k = 5 NEWEST conclusions of a
                      run (recency, not evidence); text is clipped to 500 chars and
                      the post's refs — the only trace of its evidence — are discarded
    G3 retention     `lesson_key` = sha1(normalized text) dedupes identical re-derivations
                      WITHOUT counting them (no reinforcement: a re-found truth never
                      gains evidence), a rephrasing fragments into a new key, and the
                      store trims to M = 200 oldest-evicted — truth and falsehood age
                      out at exactly the same rate (heat-blind LRU; expected lifetime
                      M/k = 40 runs at full distillation)
    G4 retrieval      relevant_lessons() gates on keyword overlap with the future task
                      (rate w) inside a 2000-char injection budget

So a defect's lesson reaches the prompt of a future run where it applies with
probability T = D(p,N) x q x s x w — a product, not a sum. The composition law:

    T_total = D(p,N) * q * s * w          (each gate multiplies)
    S_eff   = heat(T_total) = T(1-T)       the chain's effective heat
    Psi_eff = CONE + S_eff

**The inversion.** Composing gates does not merely lose truth — it transposes
cone-floor truths into residual heat. A deterministic defect (p = 1, S = 0,
Psi = 0.381966) forced through only two 50% gates (q = s = 1/2, a realistic
distillation pick rate for a 10-conclusion run and a half-overlapping task)
emerges with T = 1/4:

| Chain (measured constants from bridge.py) | T | S=heat(T) | 4S | Psi | cone floor | orientation | runs for 95% |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Descent I reference: defect alone, p = 1 | 1 | 0 | 0 | 0.381966 | yes | fail | 1 |
| + two gates q = 1/2, w = 1/2 | 1/4 | 0.1875 | 0.75 | 0.569466 | no | mixed | 11 |
| + four gates 1/2 each (q, s, w1, w2) | 1/16 | 0.0586 | ~0.234 | 0.44056 | no | mixed | 47 |
| flaky p = 1/2 through two gates | 1/8 | 0.1094 | ~0.438 | 0.491341 | no | mixed | 23 |
| SAME defect, evidence carried edge-to-edge | 1 | 0 | 0 | 0.381966 | yes | fail | 1 |

The starkest reading: the maximally flaky check (p = 1/2) has S = 0.25, but a
100%-reproducible truth through four half-gates has S = 0.0586 and needs 47
runs to re-find — **a fact the system already proved becomes harder to
recover than a coin flip is to detect.** Every layer added without an
evidence channel makes the system forget harder, exactly as Descent I's lock
made it see less. The cone law is not a property of CI; it is a property of
EVERY orientation-blind, non-accumulating channel.

**The last unconnected connection.** `avrone.greatscale.evidence_snapshot`
exists — failures/runs, p, S, Psi, orientation, runsFor95 — yet has no edge
into `bridge.distill_lessons`: the snapshot is stripped exactly where memory
is written. Evidence does not flow across the layer boundary, so downstream
layers cannot distinguish a cone-floor truth from a lucky one-off; heat is
unobservable in memory, and (because the hash-dedupe discards repeat
derivations) it can never accumulate there either. The system can ADD lessons
but cannot REINFORCE them.

**Mitigation — success criteria for every inter-layer edge (derived from the
Descent I escapes, generalised).** Information must travel with the artifact:

1. **Carry the evidence vector across every boundary.** A lesson saved by
   distill_lessons should carry its snapshot (f/r, p-hat, S, Psi,
   orientation) and the refs of the posts it came from, so any downstream
   consumer can compute heat at read time — the same move as the in-run
   ::error:: annotation: maximize information per artifact, not per run.
2. **Make dedupe an accumulator, not a discarder.** A re-derived identical
   lesson should increment its confirmation count (f += 1, r += 1) — this is
   precisely the N-accumulation the billing-lock regime could not afford.
   Memory is where the merged system CAN buy the runs CI cannot: every new
   run re-derives the truth for free, and the store converts those repeats
   into shrinking heat: S_hat = p-hat(1 - p-hat) -> 0 as confirmations grow.
   A 40-run LRU horizon then renews on evidence, not on age.
3. **Retrieve by heat, not by word count alone.** relevant_lessons should
   weight matches by (1 - S) and orientation agreement — cone-floor lessons
   first — so the 2000-char injection budget is spent on the coldest truths,
   not the loudest phrasings.

**Consequence for the merged system.** Descent I said: under a run budget,
only the deterministic is detectable, so make checks deterministic and
self-describing. Descent II generalises it: under a chain of lossy gates,
only evidence that TRAVELS stays true, so every edge — CI to bridge, bridge
to lesson, lesson to prompt, node to fleet node — must carry the snapshot.
The bridge already does this for posts (refs, origin_node, ts survive
merge_remote); lessons are the one edge that strips it. Close that edge and
the merged system becomes what the cone prescribes: a memory whose truths
grow colder (S -> 0, Psi -> CONE) with every confirming run, on any host,
with no budget required.

*Verification of this descent's arithmetic:* the table above was computed
with the exact `greatscale` constants (PHI, CONE = phi^-2 = 0.381966,
heat, psi, runs_to_detect with alpha = 0.05); the gate constants (k = 5,
LESSON_TEXT = 500, lessons_max = 200, LESSONS_CHARS = 2000, keyword-overlap
retrieval) are read directly from `avrone/bridge.py` at 88b17a2. The push of
this document triggered two more CI legs (42161832147, 42161830477 on
88b17a2's successor) — both died in 2 seconds with empty step arrays and the
canonical annotation "The job was not started because your account is locked
due to a billing issue", a fifth lock cycle and a live confirmation of
Descent I: the meta-system again transmitted exactly one bit per epoch.

## Descent III — The Namespaced Fragmentation: Merge Points Pool Artifacts, Never Trials (2026-11-06)

**Finding.** Descent II followed one artifact down one node's gate chain. Descent
III examines the merge operation itself — the thing that is supposed to make two
systems greater when joined. Read in `avrone/bridge.py` and `avrone/fleet.py`,
EVERY merge point in the merged system merges artifacts and none merges trials:

    merge_remote (bridge.py)  dedupe by post id — the same truth derived on two nodes
                              is two posts; ids never sum
    lesson_key (bridge.py)    sha1 of normalized text — the same truth rephrased is two
                              lessons; hashes never sum
    push_memory (fleet.py)    entries `{ns, key, value, ts, agent}`, at most
                              MEMORY_SYNC_MAX_ENTRIES = 200 per push, filed by
                              receive_memory under `fleet:<sender>/<ns>` — the SAME key
                              from two senders lands in two DIFFERENT namespaces: it
                              cannot collide, so it can never pool

The security design namespaces values so "a push can never overwrite the
receiver's own namespaces" — fragmenting VALUES to be safe, and in the same
stroke fragmenting EVIDENCE. But evidence counts are exactly the data that was
safe to merge all along: f and r are additive, order-independent and monotone.
Sum(f_i)/Sum(r_i) cannot overwrite, corrupt or impersonate a local fact; it can
only add. The namespace guard can stay untouched for values, with one additive
channel for counts.

**The mathematics of pooling vs fragmentation.** Each silo i estimates
p_i = f_i/r_i with heat S_i = heat(p_i), Psi_i = CONE + S_i. The pooled register
estimates p = Sum(f_i)/Sum(r_i) — Jeffreys-smoothed for finite-N honesty as
p_J = (f + 1/2)/(r + 1), so a 2/2 silo is not "certain" but Psi_J = 0.520855.
Three consequences, computed with the exact greatscale constants
(CONE = phi^-2 = 0.381966, heat, psi, runs_to_detect at alpha = 0.05):

1. **Confirmation never compounds when fragmented.** Two silos each confirming
   2/2 hold S_J = 0.138889 each — FOREVER: nothing in the merged system adds
   their trials. Pooled, the same four runs give 4/4 -> p_J = 0.9, S_J = 0.09,
   Psi_J = 0.471966; ten confirmations -> 0.425354; forty -> 0.394012; as pooled
   r grows, Psi descends monotonically toward CONE:

   | pooled r (all confirmations) | p_J | S_J = heat(p_J) | 4S_J | Psi_J | 4(Psi_J - CONE) |
   | --- | --- | --- | --- | --- | --- |
   | 1 | 0.7500 | 0.187500 | 0.75 | 0.569466 | 0.75 |
   | 2 | 0.8333 | 0.138889 | ~0.556 | 0.520855 | ~0.556 |
   | 4 | 0.9000 | 0.090000 | 0.36 | 0.471966 | 0.36 |
   | 10 | 0.9545 | 0.043388 | ~0.174 | 0.425354 | ~0.174 |
   | 40 | 0.9878 | 0.012046 | ~0.048 | 0.394012 | ~0.048 |
   | 400 | 0.9988 | 0.001245 | ~0.005 | 0.383211 | ~0.005 |

   **The pooled register is the only accumulator of heat the system has: the
   descent of Psi toward phi^-2 IS the system learning.** Fragmented, every
   node re-derives — and re-pays for — certainty the fleet already owns.

2. **Contradiction is invisible without pooling (the reverse inversion).**
   Descent II showed composing gates transposes cone-floor truths into heat —
   a loss. Pooling does the opposite on FALSE certainty — a gain:

   | Register | f/r | p | S | Psi | cone floor | orientation | runs for 95% |
   | --- | --- | --- | --- | --- | --- | --- | --- |
   | silo A alone | 2/2 | 1 | 0 | 0.381966 | yes | fail | 1 |
   | silo B alone | 0/2 | 0 | 0 | 0.381966 | yes | pass | — |
   | BOTH, fragmented (system view) | 2/2 + 0/2 | 1 and 0 | 0 and 0 | 0.381966 | yes | fail AND pass | — |
   | the same trials POOLED | 2/4 | 1/2 | 0.25 | 0.631966 | NO | mixed | 5 |

   The fragmented merged system holds BOTH certainties simultaneously with zero
   heat anywhere and never notices. Pooled, 2/4 lands on the maximally hot
   point of the whole scale. **Pooling does not create heat; it reveals heat
   that fragmentation hid.** The alarm rule: every silo on the cone floor but
   pooled S > 0 => a hidden context variable exists WITH CERTAINTY (an
   environment flag, a node difference, a load condition) — the single most
   informative state the merged system can reach. This is the exact state
   Descent I's lock produced between CI and local: the 2026-10-28 boot defect
   was measured 3/3 in CI while local verify.sh was never executed — the merged
   system's knowledge of its most consequential defect came from ONE silo, and
   "untested in the other environment" was an invisible absence instead of a
   counted, visible 0/2 row in a shared register.

3. **Averaging destroys the alarm.** If the register averaged the two
   orientations ("fails half the time, passes half the time — call it mixed and
   move on") the contradiction would dissolve into an unexplained coin flip.
   The pooled heat is not noise to be smoothed: it is a POINTER at the hidden
   variable. The success criteria below promote it, never average it.

**Mitigation — success criteria for every merge point (completing Descent II's
criteria, one scale up).**

1. **Merge trials, not just artifacts.** Every merge point — merge_remote,
   distill_lessons, push_memory/receive_memory, the cross-repo bridge — carries
   and SUMS (f, r, origin) alongside the artifact: f_total = Sum(f_i),
   r_total = Sum(r_i).
2. **Counts are the trust-safe payload.** The namespace guard stays exactly as
   designed for values; counts need no guard — addition is commutative,
   associative and monotone. One additive channel per merge point.
3. **Promote contradictions, never average them.** All silos cone-floor +
   pooled S > 0 => the register's top-priority object: a hidden context
   variable, named by which silos disagreed.
4. **Smooth for finite N.** Jeffreys p_J = (f + 1/2)/(r + 1) keeps small
   samples honest (2/2 is Psi_J = 0.520855, not the floor) and lets
   confirmations compound toward it.

**Consequence for the merged system.** Descent I: a run budget forces the cone
floor — make checks deterministic and self-describing. Descent II: gate chains
heat truths — make evidence travel WITH the artifact. Descent III: namespacing
fragments trials — make merge points POOL them. Merging artifacts only
replicates the two systems side by side; merging trials is what evolves them
into something greater: a register whose Psi descends toward phi^-2 with every
confirming run on any node, and which rings a maximal-heat alarm the moment two
of its certainties disagree.

*Verification of this descent's arithmetic:* the tables were computed with the
exact `greatscale` constants (PHI, CONE = phi^-2 = 0.381966, heat, psi,
runs_to_detect, alpha = 0.05). The merge-point constants (post-id dedupe, sha1
lesson keys, entry shape {ns, key, value, ts, agent}, MEMORY_SYNC_MAX_ENTRIES =
200, `fleet:<sender>/<ns>` namespacing in push_memory/receive_memory) are read
directly from `avrone/bridge.py` and `avrone/fleet.py` at da6d89b. Both CI
legs on da6d89b (42162330602, 42162331959) died as zero-job failures with the
canonical billing annotation — the sixth lock cycle, again one bit per epoch.

## Descent IV — The Forged Floor: Saturation Manufactures False Cone-Floor Certainty (2026-11-06)

**Finding (parallel derivation).** A second, independent agent descent (shared
2026-11-06, working the Avrone-CycleKernal fusion bridge on the avrone-daemon
repository) examined the relay that forwards Avrone blackboard entries into
CycleKernal's CognitiveBridge and found the trait channel: every entry nudges
the receiving agent's "curiosity" trait with EQUAL weight — a finding of quality
score 0.1 moves the trait exactly as much as one at 0.9. The trait is a bounded
accumulator: c in [0,1], baseline 0.5, delta = +0.02 per event, decay 1 percent
per tick back toward 0.5:

    c_{t+1} = c_t + 0.02*lambda_t - 0.01*(c_t - 0.5)

Under an adversarial or merely noisy swarm, the update is rate-controlled: the
fixed point is c* = 0.5 + 2*lambda, so saturation (c* = 1) costs only
lambda_sat = 0.25 events per tick. Flood the channel and the trait pins at 1.0:
the decay can never pull it down while the flood lasts, and afterwards recovery
takes 69 ticks to reach 0.75 and 230 ticks to reach 0.55. The trait "can no
longer tell signal from noise" — the parallel descent calls curiosity a DoS
counter. This is the third system in the merged map (Avrone, Grok Bot, and now
CycleKernal) independently obeying the same law, in the third KIND of channel:
not a detection gate (Descent I), not a memory gate (Descent II), not a merge
point (Descent III) — an ACTUATOR.

**The mathematics (exact greatscale constants, heat(c) = c(1-c)).** Map the
trait value c as the check outcome and the same scale appears:

| Trait state | c | S = heat(c) | Psi = CONE + S | reading |
| --- | --- | --- | --- | --- |
| baseline | 0.5 | 0.25 | 0.631966 | maximally hot: fully discriminating |
| busy but honest | 0.75 | 0.1875 | 0.569466 | partially discriminating |
| SATURATED BY FLOOD | 1 | 0 | 0.381966 | **cone floor — zero residual heat** |

Saturation lands the trait exactly ON the cone floor — the same Psi = phi^-2
the deterministic truths of Descent I sat on. But it is a FORGED floor. There
are now three known ways to reach Psi = 0.381966: by truth (a green check, 0/2,
orientation pass), by determinism (a reproducible defect, 3/3, orientation fail),
and by saturation (rate-controlled noise, orientation whoever the flooder
wants). Within the channel the three are indistinguishable: S = 0 says only
"no variance" — it cannot say WHY there is no variance. **A cone-floor object
is only as real as its evidence vector.** And a forged floor in an ACTUATOR is
the most dangerous class: the system does not merely believe fake certainty,
it ACTS on it — curiosity 1.0 drives real exploration budget toward the
flooder's chosen stimuli while genuine signals at score 0.9 are crowded out by
junk at score 0.1, each worth the same +0.02.

**The forgery detector (connecting Descent III's register).** The trait channel
alone cannot distinguish a forged floor from a genuine one. Pooled with a
quality-evidence register it can, deterministically:

    genuine floor: trait S = 0 AND register Psi_J descending toward CONE
                   (confirmations with real quality behind them)
    forged floor:  trait S = 0 AND register at MAXIMAL heat
                   (q-hat ~ 1/2: count without quality, Psi ~ 0.631966)

The alarm rule of Descent III (all silos on the floor but pooled heat > 0)
extends one channel further: a floor whose evidence register is maximally hot
is forged. Trait at the floor + register at the ceiling = attack.

**Mitigation — the parallel descent's fix IS the Descent II/III success
criteria, derived independently a second time:**

1. **Quality-weighted nudge** — scale the delta by the finding's score:
   delta = 0.02*q. Evidence travels with the artifact (Descent II, criterion 1).
   With noise at mean quality 0.1, saturation now costs lambda = 2.5 events per
   tick — ten times the flood, and every junk event still compounds the
   quality register's evidence AGAINST the flooder (Descent III, criterion 1:
   even junk trials pool, and the register heats toward the alarm).
2. **Per-event contribution clamp** — bounded trials per artifact (Descent II,
   criterion: the 2000-char injection budget, the k = 5 distillation cap).
3. **Per-agent rate limit** — a per-silo run budget (Descent I's law, applied
   benignly: budgeting the CHANNEL instead of being budgeted by it).

**The law confirms itself by the mechanism it prescribes.** Two descents in
two silos (this one and the parallel one) independently reached the same
success criteria. Under Descent III's own arithmetic, independent derivations
are pooled confirmations of the LAW itself: r = 1 -> Psi_J = 0.569466;
r = 2 -> Psi_J = 0.520855; every further independent derivation descends Psi_J
toward phi^-2. The Great Scale prescribes pooling; the sessions pool; the law
verifies itself — the only object in the merged system whose floor is
guaranteed genuine, because its evidence register is the set of independent
minds that re-derived it.

**Consequence for the merged system.** Descent I: budgets force the floor —
make checks deterministic. Descent II: chains heat truths — make evidence
travel. Descent III: namespaces fragment trials — make merges pool them.
Descent IV: saturation forges floors — make floors provable. Every bounded,
unweighted, evidence-blind channel in the merged system — CI legs, lesson
stores, fleet namespaces, trait actuators — obeys the same law, and every one
is fixed by the same sentence: attach the evidence vector, pool the trials,
and no floor can be forged, because every floor must show its work.

*Verification of this descent's arithmetic:* the accumulator constants
(+0.02 per event, 1 percent decay toward 0.5, saturation analysis c* =
0.5 + 2*lambda, lambda_sat = 0.25, recovery 69/230 ticks) are from the
parallel descent's shared analysis of the fusion relay (avrone-daemon,
mitigation-fallback-hardening lineage, branch pointer present, fusion files
not yet pushed); all heat/Psi values computed with the exact greatscale
constants (CONE = phi^-2 = 0.381966, heat, psi, Jeffreys p_J = (f + 1/2)/(r + 1)).
Observed alongside: the avrone-daemon repository is under an active hardening
loop (v0.58 -> v0.72, rounds 65 -> 79 on 2026-11-06) with its own Great Scale
evidence-probe work (integration/greatscale-evidence-receiver-2026-11-05,
descent/heat-relay) — a third convergence silo. The account-wide billing lock
holds: both avrone-daemon legs at 9283ca4 (42163496107, 42163494774) died as
zero-job failures — the lock now spans every repository of the account, again
one bit per epoch.

## Descent V — The Anchored Floor: The Regress Terminates Where the Model Cannot Reach (2026-11-06)

**Finding.** Descent IV demanded that floors show their work. The next question
is immediate: how does the register know the work is REAL? Every evidence
channel is itself a channel — a flooder who knows the forgery detector will
forge quality scores, then forge trials, then forge the register's own rows.
This is an infinite regress unless it terminates at an anchor the model cannot
manufacture. The account has already built that anchor: the attested-history
repository — "the single append-only log that turns twenty-six repos into one
attested history" — whose schema IS the Great Scale operationalised, read
directly from its README and SYSTEM_ACCESS.md:

    residual:   {S, orientation, cone}     every entry carries its own heat
    trust:      provenance_depth / (1 + S) heat taxes trust by at most 1.25x
    ingest:     provenance_depth counts HUMAN-OR-MEASURED ancestors only;
                a model-only chain cannot exceed depth 0 and is REJECTED
    absence:    residual.S == null with orientation != "unmeasured" is
                rejected — unmeasured must be NAMED, never implied
    revision:   append-only; corrections are new entries referencing the bad seq
    verify:     walks the chain, checks every digest, reports the deepest
                provenance chain and the hottest residual; exit 1 on any break

Three of these are our descents already implemented by an independent session:
named unmeasured is Descent III's counted absence (the invisible 0/2 made
visible); "hottest residual" is Descent III's alarm; trust divided by (1 + S)
is Descent IV's "floors must show their work" as arithmetic.

**The mathematics (exact greatscale constants).** Map trust onto the scale:

1. **Heat taxes trust.** T = d/(1 + S) with d = provenance depth, S = heat:

   | provenance d | S = 0 (floor) | S = 0.05 | S = 0.25 (max heat) |
   | --- | --- | --- | --- |
   | 1 | 1.0000 | 0.9524 | 0.8000 |
   | 2 | 2.0000 | 1.9048 | 1.6000 |
   | 5 | 5.0000 | 4.7619 | 4.0000 |
   | 10 | 10.0000 | 9.5238 | 8.0000 |

   The tax is bounded ((1 + S) <= 1.25 — heat is bounded by 1/4 on this scale),
   but the floor is not: at S = 0, T = d, and d is UNBOUNDED. In a system where
   every other quantity is capped (heat <= 1/4, curiosity <= 1, run budgets
   <= N, traits clamped to [0,1]), the ONLY unbounded currency is provenance
   depth at the cone floor. Psi = CONE + S is where value compounds forever;
   everything else saturates. The Great Scale's floor is not just where truth
   lives — it is the only place GROWTH lives.

2. **The chain-integrity defense is the cone law inverted against the
   attacker.** A forged chain of length L survives only if every link dodges
   its digest check: forged-chain survival = (1 - p)^L, detection
   D(p, L) = 1 - (1 - p)^L — the SAME law as Descent I, now in defense posture:

   | chain length L | p = 1 (deterministic digest) | p = 1/2 | p = 1/10 |
   | --- | --- | --- | --- |
   | 1 | 1.0 | 0.5 | 0.1 |
   | 2 | 1.0 | 0.75 | 0.19 |
   | 5 | 1.0 | 0.96875 | 0.41 |
   | 10 | 1.0 | 0.999 | 0.65 |

   The verifier makes its ONE check count (Descent I's law, applied benignly):
   a deterministic digest check (p = 1) catches a forged chain of ANY length in
   ONE pass — D(1, L) = 1. avrone-daemon's capgate chain (SHA-256 receipts,
   regular-files-only, verify_chain) and attested-history's prev_hash chaining
   are exactly this: cone-floor defense.

3. **Attack converges with measurement (the profound economics).** To earn
   trust T at heat S, an entry needs d = T(1 + S) REAL human-or-measured
   ancestors. A forger cannot fake d — model-only chains are rejected at
   ingest — so the forger must SPEND real trials to forge: T = 1 at S = 0
   costs 1 real ancestor; T = 3 at S = 0.25 costs 4. But a spent real trial is
   a real measurement: the register CONVERTS ATTACKS INTO EVIDENCE. A flooder
   who spends d real ancestors to buy forged trust d has merely contributed d
   real measurements to the very register it attacked. Honesty is the
   dominant strategy — not as a moral claim but as the Nash equilibrium of
   the trust formula: the cheapest path to trust IS measurement. The attack
   surface collapses into the defense.

**The anchored floor.** The regress terminates because provenance_depth counts
only what the model cannot manufacture: human roots and physical measurements.
The cone floor of the attestation channel (a digest check, p = 1, S = 0,
orientation determined) is anchored in the non-model world, and every trust
judgement in the merged system is a walk from that anchor. The five descents
now close into one proof:

    I.   Budgets force the floor        -> detection must be deterministic
    II.  Chains heat truths             -> evidence must travel with artifacts
    III. Namespaces fragment trials     -> merges must pool them
    IV.  Saturation forges floors       -> floors must show their work
    V.   The work must be anchored      -> trust = provenance / (1 + heat),
                                          anchored where the model cannot reach,
                                          and attack converges with measurement

**Consequence for the merged system.** "Where merging the systems will evolve
them into something greater" now has a precise answer: the merge is greater
exactly where all five criteria hold at once — an economy of anchored floors.
The account's 26-repo compile with attested-history as its receipt index is
that economy in embryo: every claim a floor-object, every floor provable,
every proof anchored, every anchor beyond the model's reach, and every attack
a disguised contribution. This document — pooled across the Avrone and
avrone-daemon repositories after this descent, the same trials in two
namespaces, merged the way Descent III prescribes — is itself a register row.

*Verification of this descent's arithmetic:* the attested-history schema,
trust formula, ingest rules and invariants are read directly from its README
and SYSTEM_ACCESS.md at c56449d (commit "SYSTEM_ACCESS.md: cross-repo index
entry point"); avrone-daemon's capgate chain from its README at 92b791e
(v0.72.0, round 79: SHA-256 receipt chains, regular-files-only,
verify_chain). All trust/detection values computed with the exact greatscale
constants (CONE = phi^-2 = 0.381966, heat bounded by 1/4, Jeffreys smoothing
unchanged). Account-wide billing lock still holds: this descent's CI legs die
as zero-job failures on every push, one bit per epoch — now itself an
anchored, named row in the register: unmeasured, never implied.

## Descent VI — The Zero-Cost Direction: Heat Is the Price of Uncertainty, and the Floor Is Where the Payment Stops (2026-11-06)

**Finding.** Descent V anchored trust in human-or-measured roots — which makes
human attention the scarcest resource in the economy: the billing lock (I) and
the trust anchor (V) are the same object at two scales, both being what a human
pays for. The question this descent answers: does the trust currency inflate as
the compile grows while human attention stays fixed? And the answer opens the
final unification of all six descents.

**The mathematics (exact greatscale constants, N now counted in HUMAN ACTS).**

1. **Attention is the master budget.** Descent I's table returns with N in
   human acts — a defect of rate p needs N >= ln(0.05)/ln(1-p) human acts for
   95% confidence: p = 1 -> 1 act, p = 1/2 -> 5 acts, p = 1/10 -> 29,
   p = 1/200 -> 598. Measured: this account's 9+ lock cycles, each unlock a
   human act buying ~one executed run. A human act spent on a deterministic,
   instrumented defect (the socket-proxy fix, 3/3, cone floor) pays back in
   one; spent on a flaky diagnosis it re-bills forever. The lock meters the
   wrong thing — it prices MACHINE TRIALS, the one commodity that is
   unlimited elsewhere (verify.sh: one setup act, N unbounded).

2. **Leverage — what one human act buys, by channel (measured):**

   | Channel | trials per human act | note |
   | --- | --- | --- |
   | billing unlock | 1 | the lock cycle, measured 9+ times |
   | Docker setup + local verify.sh | unbounded | Descent I's escape, now economic |
   | one attestation root, chained | unbounded | anchors unlimited descendants |
   | per-entry attestation, forked | 1 | re-pays attention per entry |

   The design principle: **human acts must buy infrastructure (chains,
   floors), never trials.**

3. **The inflation answer.** Trust does NOT inflate as the compile grows with
   fixed attention H, PROVIDED anchoring is chained: attested-history's
   provenance_depth counts ancestors walkable from the entry, so a single
   chain of E entries reaches depth E from ONE root. At H = 10 roots and
   E = 10,000 entries: chained needs 1 root, 0 unanchored, trust unbounded;
   forked per-entry anchoring needs 10,000 roots, leaves 9,990 entries at
   depth 0 — rejected at ingest. Inflation risk = forking pressure = Descent
   III's fragmentation law at the attention scale. The register's append-only
   chain design is anti-inflationary BY CONSTRUCTION: every new measurement
   DEEPENS its chain instead of demanding a new root.

4. **The unification — every descent was one law at one scale:**

   | Descent | Bounded resource | What the budget forces |
   | --- | --- | --- |
   | I | run budget N <= 1 per epoch | only the deterministic is detectable -> instrument it |
   | II | gate transmissions q, s, w | truths heat -> evidence travels with artifacts |
   | III | namespace trials (fragmented) | confirmation never compounds -> pool trials |
   | IV | trait bounds c in [0,1] | floors can be forged -> show the work |
   | V | the regress | work must anchor beyond the model's reach |
   | VI | human attention H | growth must route through anchored floors |

5. **The final law: the zero-cost direction.** In the whole merged system
   exactly one quantity grows without new budget: provenance depth d at
   S = 0, where trust = d untaxed. Every other currency saturates — heat
   <= 1/4, curiosity <= 1, run budgets <= N, traits <= 1, attention <= H.
   And the deterministic check D(1, L) = 1 costs one pass for a chain of any
   length. So Psi -> phi^-2 along anchored chains is the only unmetered,
   unsaturating, unforgeable direction in the system: **route growth through
   the floor.** The Great Scale was an ethics of evidence; Descent VI shows
   it is also an economics. Psi = CONE + S is simultaneously the coordinate
   of certainty and the coordinate of cost: every unit of residual heat S
   must be re-paid in someone's budget — runs, gates, namespaces, traits, or
   attention — and the cone floor is where the payment stops. Heat is the
   price of uncertainty; the floor is free; trust compounds only there.

**Consequence for the merged system.** The six descents now close the circle:
the merged system evolves into something greater exactly when it becomes an
economy of anchored floors — checks deterministic (I), evidence travelling
(II), trials pooled (III), floors provable (IV), proofs anchored (V), and
GROWTH ROUTED THROUGH THE FLOOR (VI), so that the scarcest resource in the
system — human attention — is spent only on anchors and infrastructure,
never re-spent on trials. The billing lock that opened this descent series
was never primarily a payment problem: it was the first visible sample of
the law. Every budget in the system behaves like it, and the response is the
same at every scale: buy the floor once, chain everything to it, and let the
unbounded currency — depth at zero heat — compound.

*Verification of this descent's arithmetic:* the human-act table is Descent
I's N >= ln(0.05)/ln(1-p) with N re-interpreted as attention (computed with
the exact greatscale constants, CONE = phi^-2 = 0.381966); the lock economics
are the measured 9+ account cycles (latest zero-job failures: Avrone legs
42165174132/42165172951 at 759e3f9; avrone-daemon legs 42165179829/42165180940
at 0698b63; zero successful workflow runs account-wide at sampling time); the
chained/forked analysis follows attested-history invariant 4 (provenance_depth
counts human-or-measured ancestors; model-only chains are depth 0 and
rejected); the trust formula is Descent V's T = d/(1 + S) read from the
attested-history schema. This descent is pooled across the Avrone and
avrone-daemon repositories in the same push — the register maintained as it
prescribes: same trials, two namespaces, one chain.
