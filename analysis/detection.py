#!/usr/bin/env python3
"""
RQ1: how quickly does each signal detect the fault?
For every run we measure, relative to the fault marker (lock_start / gc_start / hang_start / spike start at 60 s):
  * t_P80        first 100 ms tick at which the gateway's pressure index P (EWMA) exceeds P_high = 0.80
  * t_P55        first tick at which P exceeds P_low = 0.55
  * t_fail1      first client-observed failure (timeout or downstream error) - what a failure-rate breaker sees
  * t_slow1s     first successful call slower than 1 s (Resilience4j slow-call default) - what a slow-call breaker sees
  * t_slow250    first successful call slower than 250 ms (SLO-tight slow-call threshold)
  * t_pend       first tick with >= 1 thread awaiting a pooled connection (the raw pool symptom)
The pressure index is computed by the gateway in every policy (it is only *acted on* by TACL), so all runs contribute.
"""
import glob, os, numpy as np, pandas as pd
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
rows = []
for scen in ["spike", "lock", "gc", "hang"]:
    for d in sorted(glob.glob(f"{ROOT}/{scen}/*/rep[0-9]*")):
        try:
            r = pd.read_csv(f"{d}/requests.csv"); t = pd.read_csv(f"{d}/telemetry.csv")
        except Exception:
            continue
        if len(t) == 0 or (r.ts_ms.max() - t.ts_ms.min()) < 140_000:
            continue
        t0 = t.ts_ms.min()
        try:
            m = pd.read_csv(f"{d}/markers.csv", header=None, names=["ts_ms", "label"])
            start = m[m.label.str.endswith("_start")].ts_ms.iloc[0]
        except Exception:
            start = t0 + 60_000 + 1000   # spike: Locust shape switches at its own t=60 s, Locust starts ~1 s after the poller
        pol = d.split("/")[-2]
        P = pd.to_numeric(t.gw_pressure, errors="coerce")
        pend = pd.to_numeric(t.ds_hikari_pending, errors="coerce")
        after = t.ts_ms >= start
        def first(mask):
            idx = np.where(mask & after)[0]
            return (t.ts_ms.iloc[idx[0]] - start) / 1000 if len(idx) else np.nan
        ra = r[r.ts_ms >= start]
        def firstr(mask):
            x = ra[mask]
            return (x.ts_ms.min() - start) / 1000 if len(x) else np.nan
        rows.append({"scenario": scen, "policy": pol, "rep": os.path.basename(d),
                     "t_P80": first(P > 0.80), "t_P55": first(P > 0.55), "t_pend": first(pend >= 1),
                     "t_fail1": firstr(ra.outcome.isin(["timeout", "downstream_error"])),
                     "t_slow1s": firstr(ra.outcome.isin(["ok", "not_found"]) & (ra.latency_ms >= 1000)),
                     "t_slow250": firstr(ra.outcome.isin(["ok", "not_found"]) & (ra.latency_ms >= 250))})
df = pd.DataFrame(rows)
df.to_csv(f"{ROOT}/analysis/detection_per_run.csv", index=False)
g = df.groupby("scenario")[["t_P55", "t_P80", "t_pend", "t_slow250", "t_slow1s", "t_fail1"]].agg(["median", "mean", "count"]).round(2)
pd.set_option("display.width", 250)
print(g.to_string())
g.to_csv(f"{ROOT}/analysis/detection_summary.csv")
