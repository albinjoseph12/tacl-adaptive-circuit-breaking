# TACL open-loop re-run: results (campaign finished 2026-11-20 13:10 UTC)

## Campaign
- **Runs:** 190 of 190 completed (7 policies × 4 scenarios chain + 5 policies × 2 scenarios split, × 5 reps), with
  0 retries.
- **Platform:** GCP free-trial credit only. Both VMs were deleted at about 13:22 UTC on 2026-11-20 after the data was copied
  and verified (sha256 matched). No disks, addresses or snapshots remain in project `tacl-rerun`.
- **Missing requests:** the generator's hard deadline cut off at most 6 requests of a run (4 spike runs; scheduled
  minus issued = 1–6). The original campaign did not log these counts, but its row counts show up to 227 missing in a
  run.
- **Offered-load parity:** the largest spread in requests offered to policies within a scenario and rep is
  **0.004 %**, against 0.24 % in the original.
- **Same traces:** reps 0–2 reuse the original seeds. Row counts per run match the original's same-seed run within
  0.24 %; every difference is a request the original lost to its deadline.

**Data**
- Raw runs: `results_openloop_rerun/` (raw runs in the separate archive `tacl_openloop_rerun_raw_2026-11-20.tar.gz`) (2.2 GB). The archive copies are in `data/`:
  - `bench_results.tgz` (sha256 d2b69445…08eb6)
  - `loadgen_rerun.tgz` (sha256 174fd61a…39c24)
- Derived tables: `results_openloop_rerun/analysis/`, written by the testbed's own `analysis/analyze_openloop.py`.

## 1. A bug in the paper's open-loop analysis (found by this re-run; fixed)
The open-loop generator runs on the load-generator VM and starts its schedule several seconds before the benchmark
VM's telemetry clock:
- a median of 8.0 s in the original (range −13 to +11 s);
- 15.7 s in the re-run.

`analyze_openloop.py` mixed the two clocks:

| Scenario | Event timed by | Window as originally analysed | Effect |
|---|---|---|---|
| Flash crowd | Generator, at 60–80 s of its own schedule | `window()` measured 60–80 s on the telemetry clock | On that clock the flash actually ran at about 52–72 s in the original and 45–65 s in the re-run, so the published flash-crowd event latencies came partly from after the flash |
| Faults (lock, gc, hang) | Benchmark VM, at 60 s on the telemetry clock | The issue-time cohort set t = 0 at the first *completed request*, which is the generator's clock | The fault windows were shifted by the same offset. For the 20 s hang, the published cohort covered only a few seconds of the fault |

**Fix.** Each window now uses the clock of its own event (`load_run_aligned`, `event_t0`):
- flash crowd: t = 0 at the generator's first issue;
- faults: t = 0 at the first telemetry sample, which `window()` already used.

The issue-cohort rate now divides by the number of reps present; it used to assume 3.

The closed-loop campaign is **not** affected: Locust's first request lands 0.5 s after the telemetry t0, and its flash
is visibly at 60–80 s.

**Checks**
- With the fix switched off, my independent script (`tools/analyze_rerun.py`) reproduces the published open-loop
  tables exactly (difference 0).
- With it on, it matches the patched `analyze_openloop.py` exactly, on both campaigns.

## 2. Eventual success of requests issued during the event (req/s)

| Scenario / topology | Policy | Published (orig., misaligned) | Original, aligned | Re-run, aligned (5 reps) |
|---|---|---|---|---|
| Flash crowd | none | 2,466 | 2,457 | 2,469 |
|  | TACL | 652 | 652 | 785 |
| Row lock | none | 591 | 596 | 590 |
|  | TACL | 530 | 522 | 534 |
| GC | none | 601 | 602 | 603 |
|  | TACL | 349 | 303 | 354 |
| Hang | none | 571 | 595 | 596 |
|  | TACL | **242** | **9** | **4** |
| Row lock, split | none | 491 | 519 | 502 |
|  | TACL | 504 | 495 | 512 |
| GC, split | none | 596 | 596 | 600 |
|  | TACL | 346 | 298 | 374 |

**Conclusions**
- **The paper's headline holds, in both campaigns:** in every single-service scenario, "none" eventually satisfies
  more of the load issued during the event than TACL does.
- **Hang:** the published TACL value of 242 req/s was an artefact of the misaligned window. Aligned, TACL rejects
  98 % of what is issued during the hang (9 and 4 req/s).
- **Split topology under row-lock contention:** the published "exception" (TACL slightly ahead) does not survive. The
  sign differs between the two aligned campaigns (−5 % and +2 %), so the paper now reports no difference.

## 3. Where the unprotected policy's latency is spent (new: `sent_ts_ms`)
`issued → sent` is time waiting for one of the generator's 496 in-flight slots (16 shards × 31). `sent → completed`
is network plus server time.

| Event window | none: share of latency queued in generator | none: server-side p50 | none: end-to-end p50 | TACL: queued share | TACL: server p50 |
|---|---|---|---|---|---|
| Row lock (chain) | 92 % | 1,011 ms | 13,027 ms | 0.1 % | 6.0 ms |
| GC (chain) | 92 % | 1,168 ms | 16,071 ms | 0 % | 28.8 ms |
| Flash crowd | 95 % | 623 ms | 12,265 ms | 18 % (one rep) | 41 ms |
| Pre-fault, lock | 95 % | 672 ms | 13,385 ms | 0 % | 8.0 ms |

Under "none", the server holds the maximum 503–504 requests in flight, while the issued-but-incomplete backlog peaks
at 9,000–12,700. This resolves the open caveat in FINAL_REPORT and the paper:
- "none"'s multi-second latencies are mostly client-side queueing;
- server-side, the comparison is about 1 s against a few milliseconds.

## 4. Other cells (aligned; re-run vs original)
- **Row lock, TACL:** p50 6.1 ms vs 8.4 ms; p99 121 ms vs 105 ms.
- **Static breakers' pre-fault rejection:** 79–83 % vs 79–84 %, reproduced.
- **Pre-fault shedding, TACL / concurrency-limits:** 6 / 5 % vs 8 / 7 %.
- **Flash crowd, TACL rep 0** is an outlier:
  - It rejected 58 % instead of the other reps' 70–71 %, and its admitted requests queued in the generator (89 % of
    their latency).
  - Event p50 was 1,869 ms, against 15.5–16.1 ms in the other four re-run reps and 16.2–17.1 ms in the original's
    three.
  - It is reported in the paper, not excluded. Its cause was not identified.
- **Differences in "none":** pre-fault goodput is higher in the re-run (it drains its backlog at a different pace),
  and its end-to-end latencies are 20–30 % lower. Both are dominated by the generator queue, which is
  platform-sensitive.

## 5. Paper changes (corrected_acb-testbed/paper; backups `*.pre_rerun`, `_superseded_pre_rerun/`)
- **Section V-I rewritten.**
  - Both campaigns are reported, with the event-clock alignment described.
  - Table V now shows the 5-rep re-run, plus a first-campaign column and a server-p50 column.
  - The caveat paragraph is replaced by the measured latency split.
  - The split-topology exception is removed, and spike rep 0 is disclosed.
- **Other passages updated:** abstract (248 words), run counts, the split-topology paragraph earlier in Section V, the
  discussion, the conclusion, and data availability.
- **Rendering:** 681 placeholders, 0 unresolved. 31 pages.
