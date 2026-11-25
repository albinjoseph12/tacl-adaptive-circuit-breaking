#!/usr/bin/env python3
"""
Fault injector, driven by run_experiment.sh.

  lock  : opens a transaction on its own connection and takes row locks (SELECT ... FOR UPDATE) on the hot id range
          [1, HOT_ROWS] for `--hold` seconds. Every OLTP UPDATE that targets a hot row blocks on the lock, holding its
          HikariCP connection until statement completion or the 3 s gateway timeout. This reproduces the canonical
          production cascade: a long-running transaction (batch job, stuck migration, analytics query) exhausts the
          service's connection pool and stalls unrelated traffic behind it.
  gc    : toggles the allocation-storm in catalog-service for `--hold` seconds.
  hang  : SIGSTOPs the catalog-service JVM for `--hold` seconds (hung process); every in-flight and new call times out.
Writes markers (ts_ms,label) to --marker so analysis can align the timeline.
"""
import argparse, os, time, urllib.request
import psycopg2


def mark(path, label):
    with open(path, "a") as f:
        f.write(f"{int(time.time()*1000)},{label}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["lock", "gc", "hang", "none"], required=True)
    ap.add_argument("--start", type=float, default=60, help="seconds after script start to inject")
    ap.add_argument("--hold", type=float, default=45)
    ap.add_argument("--hot-rows", type=int, default=int(os.environ.get("ACB_HOT_ROWS", "5000")))
    ap.add_argument("--marker", required=True)
    ap.add_argument("--dsn", default=os.environ.get("ACB_DSN", "dbname=acb user=acb password=acb host=localhost port=5432"))
    ap.add_argument("--downstream", default="http://localhost:8081")
    a = ap.parse_args()
    open(a.marker, "a").close()
    time.sleep(a.start)
    if a.kind == "none":
        return
    mark(a.marker, f"{a.kind}_start")
    if a.kind == "lock":
        conn = psycopg2.connect(a.dsn)
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("SET application_name = 'acb-fault-batch'")
        cur.execute("SELECT id FROM orders WHERE id BETWEEN 1 AND %s FOR UPDATE", (a.hot_rows,))
        time.sleep(a.hold)
        conn.rollback()
        conn.close()
    elif a.kind == "hang":
        # freeze the downstream JVM (SIGSTOP) - models a hung process / VM pause / long safepoint; TCP stays open
        import subprocess, signal
        pid = int(subprocess.check_output(["pgrep", "-f", r"io\.acb\.catalog\.CatalogService$"]).split()[0])
        os.kill(pid, signal.SIGSTOP)
        time.sleep(a.hold)
        os.kill(pid, signal.SIGCONT)
    elif a.kind == "gc":
        urllib.request.urlopen(urllib.request.Request(a.downstream + "/fault/gc?on=true&threads=1&retainMb=64", method="POST"), timeout=2).read()
        time.sleep(a.hold)
        urllib.request.urlopen(urllib.request.Request(a.downstream + "/fault/gc?on=false", method="POST"), timeout=2).read()
    mark(a.marker, f"{a.kind}_end")


if __name__ == "__main__":
    main()
