#!/usr/bin/env python3
"""
100 ms telemetry logger.

Every tick it records, into a single wide CSV row:
  * PostgreSQL:  pg_stat_activity counts (active / idle-in-transaction / waiting on locks), pg_locks granted vs waiting,
                 xact_commit + tup_fetched deltas from pg_stat_database, and the longest running query age;
  * downstream:  the catalog-service /telemetry JVM snapshot (GC pause window, Tomcat busy threads, Hikari active/pending,
                 acquire-wait EWMA, heap);
  * gateway:     /control/state (pressure index, concurrency limit, breaker state, cumulative outcome counters).
The DB queries run on a dedicated connection outside the pool under test and are cheap catalog reads.
"""
import argparse, csv, json, os, sys, time, urllib.request
import psycopg2

PG_SQL = """
SELECT
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'active' AND pid <> pg_backend_pid())            AS pg_active,
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND state = 'idle in transaction')                            AS pg_idle_in_xact,
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock')                                 AS pg_waiting_lock,
  (SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND backend_type = 'client backend')                          AS pg_backends,
  (SELECT count(*) FROM pg_locks WHERE granted)                                                                                            AS locks_granted,
  (SELECT count(*) FROM pg_locks WHERE NOT granted)                                                                                        AS locks_waiting,
  (SELECT xact_commit FROM pg_stat_database WHERE datname = current_database())                                                            AS xact_commit,
  (SELECT tup_fetched FROM pg_stat_database WHERE datname = current_database())                                                            AS tup_fetched,
  (SELECT coalesce(max(extract(epoch FROM (now() - query_start))),0) FROM pg_stat_activity
      WHERE datname = current_database() AND state='active' AND pid <> pg_backend_pid())                                                   AS longest_query_s
"""

DS_KEYS = ["gc_pause_1s_ms", "gc_count_total", "gc_time_total_ms", "tomcat_busy", "tomcat_max", "thread_saturation",
           "hikari_active", "hikari_pending", "hikari_max", "pool_saturation", "pool_pending_ratio", "acquire_wait_mean_ms",
           "acquire_timeouts_total", "requests_total", "errors_total", "heap_used_mb", "heap_utilisation", "gc_storm"]
GW_KEYS = ["policy", "inflight", "breaker_state", "limit", "theta_f", "pressure", "pressure_raw", "failure_rate", "slow_call_rate",
           "open_transitions", "shed_total", "shed_route_total", "routes", "rejected_open_total", "recoveries_telemetry", "recoveries_timer",
           "gw_total", "gw_ok", "gw_timeouts", "gw_errors", "gw_rejected", "telemetry_unreachable",
           # split topology only: the per-tier pressure indices the controller folds with a maximum
           "pressure_a", "pressure_b", "dsb_thread_sat", "dsb_pool_sat", "dsb_pool_pending",
           "dsb_acquire_wait_ms", "dsb_gc_pause_1s_ms"]


def fetch(url, timeout=0.09):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration", type=float, default=180)
    ap.add_argument("--period", type=float, default=0.1)
    ap.add_argument("--dsn", default=os.environ.get("ACB_DSN", "dbname=acb user=acb password=acb host=localhost port=5432"))
    ap.add_argument("--downstream", default="http://localhost:8081")
    ap.add_argument("--gateway", default="http://localhost:8080")
    ap.add_argument("--marker-file", default=None, help="file whose lines (ts_ms,label) are merged as fault markers")
    args = ap.parse_args()

    conn = psycopg2.connect(args.dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SET statement_timeout = 80")
    pg_cols = ["pg_active", "pg_idle_in_xact", "pg_waiting_lock", "pg_backends", "locks_granted", "locks_waiting", "xact_commit", "tup_fetched", "longest_query_s"]

    f = open(args.out, "w", newline="", buffering=1 << 16)
    w = csv.writer(f)
    w.writerow(["ts_ms", "t_s"] + pg_cols + ["ds_" + k for k in DS_KEYS] + ["gw_" + k for k in GW_KEYS] + ["poll_ms"])
    t0 = time.time()
    next_t = t0
    n = 0
    while time.time() - t0 < args.duration:
        tick0 = time.time()
        try:
            cur.execute(PG_SQL)
            pg = list(cur.fetchone())
        except Exception:
            pg = [""] * len(pg_cols)
            try:
                conn.rollback()
            except Exception:
                pass
        ds = fetch(args.downstream + "/telemetry")
        gw = fetch(args.gateway + "/control/state")
        row = [int(tick0 * 1000), f"{tick0 - t0:.3f}"] + pg + [ds.get(k, "") for k in DS_KEYS] + [gw.get(k, "") for k in GW_KEYS]
        row.append(f"{(time.time() - tick0) * 1000:.1f}")
        w.writerow(row)
        n += 1
        next_t += args.period
        sleep = next_t - time.time()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_t = time.time()
    f.close()
    print(f"telemetry: {n} samples -> {args.out}")


if __name__ == "__main__":
    main()
