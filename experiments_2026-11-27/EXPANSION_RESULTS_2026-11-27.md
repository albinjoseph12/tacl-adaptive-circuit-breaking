# Paper 2 (TACL) — Experimental Expansions Summary (2026-11-27)

Executed in CPU-pinned Docker containers (`postgres:16` 2,000,000-row `orders` table, `eclipse-temurin:21-jre`, 4-shard open-loop Poisson generator `open_loop_gen.py`, fresh JVMs + DB `DELETE`/`VACUUM ANALYZE` reset per run). Raw logs in `results_expansion_2026-11-27/v2_4shard/`.

## 1. Higher-Power Replication (`n = 5`) with Bounded `[0, 100%]` Bootstrap 95% CIs
Scenario: `row_lock`, `800 req/s` (4 shards), 5 distinct Poisson seeds (`202611271–202611275`), 18 s fault window `[6, 24 s]`, 10,000-resample percentile bootstrap 95% CIs:

| Policy | Goodput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | Rejected (%) | Failed (%) |
|---|---|---|---|---|---|---|
| `none` | `792.2 [788.0, 796.3]` | `11,955.5 [7,043.8, 16,867.3]` | `22,705.6 [10,811.9, 34,599.3]` | `25,316.7 [12,312.5, 38,321.0]` | `0.0 [0.0, 0.0]` | `1.2 [0.8, 1.7]` |
| `static` (defaults) | `240.8 [113.9, 398.1]` | `7,987.1 [1,812.9, 14,465.6]` | `11,992.5 [3,129.0, 20,959.9]` | `12,842.4 [3,369.1, 22,315.6]` | `69.4 [49.8, 85.5]` | `0.5 [0.3, 0.8]` |
| `adaptive` (TACL) | `619.9 [519.9, 697.7]` | `3,902.8 [2.6, 10,018.5]` | `5,124.0 [12.8, 12,924.8]` | `5,905.4 [409.1, 14,164.7]` | `10.5 [3.9, 18.8]` | `12.2 [9.1, 16.3]` |

## 2. Offered-Rate Load Sweep (`200–1,200 req/s`, steady state `[4, 20 s]`, no fault)

| Rate | Policy | Goodput (req/s) | p50 (ms) | p95 (ms) | p99 (ms) | Rej % | `<= 100 ms` % | `<= 250 ms` % |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 200 | `none` / `TACL` | 196.8 / 196.8 | 3.2 / 3.5 | 8.6 / 9.9 | 13.1 / 16.2 | 0.0 / 0.0 | 100.0 / 100.0 | 100.0 / 100.0 |
| 600 | `none` / `TACL` | 603.8 / 603.8 | 2.3 / 2.4 | 7.9 / 8.0 | 16.1 / 14.4 | 0.0 / 0.0 | 100.0 / 100.0 | 100.0 / 100.0 |
| 1000 | `none` / `TACL` | 992.6 / 989.9 | 2.9 / 2.8 | 25.3 / 28.7 | 61.4 / 63.6 | 0.0 / 0.0 | 99.6 / 99.9 | 100.0 / 100.0 |
| **1200** | **`none`** | **1,196.4** | **5,894.5** | **7,973.7** | **8,670.6** | **0.0** | **0.0** | **0.5** |
| **1200** | **`TACL`** | **1,175.9** | **3.8** | **45.3** | **102.5** | **0.5** | **99.0** | **99.8** |

## 3. Two-Replica Round-Robin Fleet (`1,000 req/s`, 4 shards, `n = 3`, 10k bootstrap 95% CIs)

### 3a. Single-Replica GC Storm (Replica 2 only)
- `none`: goodput `1,003.4 [995.7, 1,010.3]`, p50 `2,098.4`, p99 `2,943.2 [38.0, 8,695.3]` ms
- `TACL (max-agg)`: goodput `887.9 [867.3, 901.4]`, p50 `4.0 [3.9, 4.2]`, p99 `37.4 [22.8, 44.9]` ms (**78.7× lower p99**), `3.1%` rejected
- `TACL (mean-agg)`: goodput `1,003.4 [995.7, 1,010.3]` (**0.0% rejected**), p50 `5.3 [4.7, 6.1]`, p99 `77.8 [56.6, 93.8]` ms (**37.8× lower p99**)

### 3b. Shared-PostgreSQL Row-Lock Contention (Both Replicas)
- `none`: goodput `984.0 [976.9, 992.3]`, p50 `1,742.2`, p99 `8,812.2 [7,778.3, 9,482.6]` ms
- `TACL (max-agg)`: goodput `916.5 [911.2, 925.2]`, p50 `2.7 [2.2, 3.0]`, p99 `64.4 [43.4, 83.8]` ms (**136.8× lower p99**)
- `TACL (mean-agg)`: goodput `939.0 [926.4, 949.5]`, p50 `3.2 [2.7, 3.8]`, p99 `215.8 [167.2, 273.9]` ms (**40.8× lower p99**)
