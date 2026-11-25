# TACL open-loop campaign: 5-repetition re-run on GCP (started 2026-11-20 02:56 UTC)

**Paid with free-trial credit only.** Project `tacl-rerun` is on the free trial: $300 of credit, and no automatic
charges unless the account is upgraded.

## Platform: matched to the original

| | Original (ENVIRONMENT.md) | This re-run |
|---|---|---|
| Benchmark VM | n2-standard-2, Intel Xeon @ 2.80 GHz, 200 GB pd-balanced, us-central1-a, 10.128.0.2 | same; CPU platform pinned to Cascade Lake; same internal IP |
| Load generator | n2-standard-8 (resized for the open-loop run), 40 GB, 10.128.0.3 | same |
| Image | ubuntu-2404-noble-amd64-v20260906 | same (deprecated but still available) |
| OpenJDK / PostgreSQL | 21.0.12 / 16.15 (Ubuntu packages) | same, installed from the Ubuntu archive snapshot of 2026-09-12 |
| Python | 3.11.16 (uv) | **3.11.14** (newest 3.11 that uv 0.9.5 ships) |
| Python packages | psycopg2-binary 2.9.13, Locust 2.46.5; httpx not recorded | same; httpx 0.28.1 (`--exclude-newer 2026-09-12`) |
| PostgreSQL settings | docker-compose.yml / paper | max_connections 200, shared_buffers 1 GB, work_mem 8 MB, effective_cache_size 3 GB, synchronous_commit off, max_wal_size 2 GB (ALTER SYSTEM) |
| Database | 2,000,000 rows, ~303–306 MB | 2,000,000 rows, 306 MB (`seed_db.py`, deterministic) |

**Code.** The testbed is `corrected_acb-testbed` without results or paper (`setup/testbed.tgz`, sha256 in `logs/`).
The server side (`run_experiment.sh`) is unchanged.

## Campaign
`campaign_on_loadgen.sh` runs on the load-generator VM. Its parameters are identical to `../run_openloop_campaign.sh`:
- the same scenarios, rates (600/s; flash crowd 2,500/s at 60–80 s) and seeds;
- 160 s per run, 16 shards, 31 in flight per shard.

Runs: 7 policies × 4 scenarios (chain) + 5 policies × 2 scenarios (split), **× 5 reps = 190 runs**. The original had
3 reps (114 runs).

The operational differences are listed in the script header:
- local driver and plain SSH instead of IAP tunnels;
- a non-self-matching `pgrep`;
- rep-major order.

## Instrumentation (the open item in FINAL_REPORT)
`overlay/load/open_loop_gen.py` is the generator plus one timestamp, `sent_ts_ms`. It is taken when a request
acquires the per-shard in-flight semaphore. Each request's latency can then be split into:
- `issued → sent`: queued in the generator;
- `sent → completed`: network plus server time.

## Smoke test
spike/none, 100 s, tagged `smoke` (not part of the campaign):
- The flash window was scheduled and sent at **2,490 req/s**. Dispatch lag p99 was 484 ms; the original's was 764 ms.
- **Pre-fault window (20–60 s):** median 7.7 s queued in the generator vs 0.61 s at the server.
- **Flash window:** median 21.3 s queued in the generator vs 0.57 s at the server.

This is one smoke run. The full campaign is what will be reported.
