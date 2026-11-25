#!/usr/bin/env python3
"""Per-second timeline summary of one run (used for pilot inspection)."""
import sys, pandas as pd, numpy as np
d = sys.argv[1]
r = pd.read_csv(f"{d}/requests.csv"); t = pd.read_csv(f"{d}/telemetry.csv")
try:
    m = pd.read_csv(f"{d}/markers.csv", header=None, names=["ts_ms", "label"])
except Exception:
    m = pd.DataFrame(columns=["ts_ms", "label"])
t0 = t.ts_ms.min()
r["s"] = ((r.ts_ms - t0) // 1000).astype(int); t["s"] = ((t.ts_ms - t0) // 1000).astype(int)
r["good"] = r.outcome.isin(["ok", "not_found"])
g = r.groupby("s").agg(req=("good", "size"), good=("good", "sum"), p50=("latency_ms", "median"), p99=("latency_ms", lambda x: x.quantile(.99)))
for o in ["timeout", "downstream_error", "shed", "shed_route", "breaker_open", "breaker_half_open"]:
    g[o] = r[r.outcome == o].groupby("s").size()
g = g.fillna(0)
tt = t.groupby("s").agg(P=("gw_pressure", "mean"), L=("gw_limit", "mean"), busy=("ds_tomcat_busy", "mean"), act=("ds_hikari_active", "mean"),
                        pend=("ds_hikari_pending", "mean"), wait=("ds_acquire_wait_mean_ms", "mean"), gc=("ds_gc_pause_1s_ms", "mean"),
                        pg_act=("pg_active", "mean"), pg_lock=("pg_waiting_lock", "mean"), st=("gw_breaker_state", "last"))
x = g.join(tt)
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 500)
print(x.round(1).to_string())
print("markers:", [(int((a - t0) / 1000), b) for a, b in zip(m.ts_ms, m.label)])
