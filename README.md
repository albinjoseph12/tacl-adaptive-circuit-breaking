# TACL testbed — Telemetry-driven Adaptive Circuit breaking and Load shedding

Reproducible benchmarking testbed used for the paper *"Adaptive Circuit Breaking and Load Shedding in Reactive
Microservices Using JVM Runtime Telemetry"*.

```
acb-testbed/
├── docker-compose.yml          PostgreSQL 16 + catalog-service + gateway + Locust + telemetry logger (CPU-pinned)
├── run_experiment.sh           end-to-end benchmark loop (native mode; what produced the paper's data)
├── services/
│   ├── catalog-service/        downstream OLTP service: embedded Tomcat 10.1 + HikariCP + PostgreSQL JDBC, /telemetry endpoint
│   ├── gateway/                reactive edge service: Netty 4.1 + JDK async HttpClient, four pluggable resilience policies
│   ├── lib/                    runtime jars (see services/build.sh for the list)
│   └── build.sh                javac build (JDK 21)
├── seed/seed_db.py             2,000,000-row synthetic `orders` table (deterministic, heavy-tailed customers/products)
├── load/locustfile.py          500 concurrent users, mixed OLTP profile, per-request outcome log, scenario shapes
├── load/warmup.py              15 s JIT/pool warm-up before each measured run
├── telemetry/poll_telemetry.py 100 ms poller: pg_stat_activity, pg_locks, JVM telemetry, controller state -> CSV
├── telemetry/inject_fault.py   row-lock contention (SELECT ... FOR UPDATE on hot rows) and GC-storm fault injectors
├── analysis/analyze.py         per-run metrics, summary tables and figures from the raw CSVs
└── results/<scenario>/<policy>/rep<k>/   requests.csv, telemetry.csv, markers.csv, locust_*.csv, logs
```

## Policies (`ACB_POLICY`)

| name          | description |
|---------------|-------------|
| `none`        | no protection (3 s call timeout only) |
| `static`      | count-based sliding-window circuit breaker with Resilience4j default semantics (window 100, min calls 20, failure rate 50 %, slow-call rate 50 % at 1 s, 10 s open, 10 half-open probes) |
| `static-aggr` | same breaker, SLO-tight tuning (slow call = 250 ms, 5 s open) |
| `gradient`    | latency-only adaptive concurrency limit (Netflix Gradient2 style) |
| `adaptive`    | **TACL**: pressure index from downstream JVM telemetry (GC pause window, Tomcat thread saturation, HikariCP saturation/pending/acquire wait) → AIMD concurrency limit, arrival-share adaptive bulkheads, pressure-modulated breaker threshold, telemetry-gated recovery |

## Scenarios (`ACB_SCENARIO`)

`spike` (flash crowd: 500 users switch to 0.1–0.3 s think time for 20 s ≈ 2.5× capacity), `lock` (a batch transaction holds
row locks on the hot id range for 45 s), `gc` (allocation storm in the downstream JVM for 45 s), `steady`.

## Running

Native (what the paper used; needs JDK 21, PostgreSQL 16, Python 3.11 with locust/psycopg2/pandas/matplotlib):

```
services/build.sh
python3 seed/seed_db.py
for rep in 0 1 2; do for s in spike lock gc; do for p in none static static-aggr gradient adaptive; do
  ./run_experiment.sh $p $s $rep; done; done; done
python3 analysis/analyze.py
```

Docker: see the header of `docker-compose.yml`.

## Revision (this version)

New experiments
* **Second topology** (`TOPOLOGY=split`): the gateway fans reads to service A and writes to service B,
  both over one PostgreSQL instance, B with half the connection pool. The controller folds the two
  per-tier pressure indices with a maximum. Results under `results/{gc,lock}-split/`.
* **Parameter sensitivity**: one factor at a time over P_high, P_low, additive increase, β and the
  dead-band pair, on the lock scenario. Results under `results/lock__<tag>/`.

Experimental controls added in this revision
* The database is restored (`DELETE` of inserted rows, then `VACUUM ANALYZE`) before every run. Without
  this the table grew across a campaign and the downstream tier's saturation throughput drifted; both
  the row-count growth and the heap bloat that survived the row-count fix are documented in the paper.
* The campaign is executed in a randomised order fixed by seed 20260907 (`plan_campaign.py`,
  `run_campaign.sh`), so that residual drift cannot be confounded with policy or scenario.
* `analysis/validate_runs.py` runs the drift test (pre-fault goodput of the unprotected policy
  regressed on run start time) and flags artefact runs.
* Two earlier campaigns are kept for reference: `results_drifted/` (no database restore) and
  `results_drift2/` (row-count restore only, still bloating). Neither is used for any reported number.

Reproducing
    ./plan_campaign.py && ./run_campaign.sh
    python3 analysis/analyze.py && python3 analysis/detection.py && python3 analysis/extended.py
    python3 analysis/validate_runs.py
    python3 paper/render.py paper/paper_full.md.tmpl paper/paper_rendered.md

The manuscript is rendered from the measurements: every number in `paper/paper_full.md.tmpl` is a
placeholder resolved by `paper/render.py` from the analysis output, so the text cannot disagree with
the data. Re-running the analysis and re-rendering regenerates the paper.

---

## Data availability

This repository holds the code, configuration, analysis scripts and figures. The complete raw measurements
(per-run logs and result files) are too large for GitHub and are archived separately on Zenodo with a DOI:
**DOI: to be added on deposit**. Download the archive and extract its `results/` directories into this folder to
re-run the analysis scripts exactly as used for the paper.

## How to cite

The paper is under review. Until it is published, please cite this repository using `CITATION.cff`
(GitHub's "Cite this repository" button).

## License

Code: MIT (see `LICENSE`). Data on Zenodo: CC BY 4.0.
