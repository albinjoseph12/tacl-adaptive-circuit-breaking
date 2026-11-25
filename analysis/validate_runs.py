#!/usr/bin/env python3
"""
Flag runs that are artefacts of the harness rather than measurements of a policy.

A run is rejected when more than 2% of its requests failed at the client with status 0 (a connection
error, which means a service was not yet listening or had died), or when it is shorter than the
measured window. Rejected runs are listed so they can be repeated; they are never analysed.
"""
import glob, json, os, sys
import pandas as pd

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
bad = []
for d in sorted(glob.glob(f"{ROOT}/*/*/rep[0-9]*")):
    f = os.path.join(d, "requests.csv")
    if not os.path.exists(f) or os.path.getsize(f) < 1000:
        bad.append((d, "missing or empty requests.csv")); continue
    try:
        r = pd.read_csv(f)
    except Exception as e:
        bad.append((d, f"unreadable: {e}")); continue
    if len(r) == 0:
        bad.append((d, "no requests")); continue
    ce = (r.get("outcome", pd.Series(dtype=str)) == "client_error").mean()
    dur = (r.ts_ms.max() - r.ts_ms.min()) / 1000.0
    if ce > 0.02:
        bad.append((d, f"client_error {100*ce:.1f}% (service not reachable)"))
    elif dur < 140:
        bad.append((d, f"short run {dur:.0f}s"))
# Campaign drift check.
#
# The defect this check exists to catch is a systematic trend across the campaign rather than noise in
# one run: the workload inserts rows, and if they are not removed the downstream tier slows down as the
# campaign proceeds, which makes runs from different points non-exchangeable. The detector is therefore
# a trend test, not a per-run outlier test: pre-fault goodput of the unprotected policy (which has no
# admission control and therefore no policy-driven variance) is regressed on the run's start time.
rows = []
for d in sorted(glob.glob(f"{ROOT}/*/*/rep[0-9]*")):
    f = os.path.join(d, "requests.csv")
    if not os.path.exists(f) or os.path.getsize(f) < 1000:
        continue
    try:
        r = pd.read_csv(f, usecols=["ts_ms", "outcome"])
    except Exception:
        continue
    t0 = r.ts_ms.min()
    r["t"] = (r.ts_ms - t0) / 1000.0
    w = r[(r.t >= 20) & (r.t < 58)]
    rows.append({"dir": os.path.relpath(d, ROOT), "cell": os.path.relpath(os.path.dirname(d), ROOT),
                 "policy": os.path.basename(os.path.dirname(d)),
                 "start_ms": int(t0),
                 "base_goodput": (w.outcome.isin(["ok", "not_found"])).sum() / 38.0})
if rows:
    df = pd.DataFrame(rows).sort_values("start_ms")
    df["hours"] = (df.start_ms - df.start_ms.min()) / 3.6e6
    df.to_csv(f"{ROOT}/analysis/baseline_stability.csv", index=False)
    sub = df[df.policy == "none"]
    if len(sub) > 3:
        x = sub.hours.to_numpy(); y = sub.base_goodput.to_numpy()
        n = len(x)
        sxx = ((x - x.mean()) ** 2).sum()
        slope = ((x - x.mean()) * (y - y.mean())).sum() / sxx
        resid = y - (y.mean() + slope * (x - x.mean()))
        se = (resid.var(ddof=2) / sxx) ** 0.5 if n > 2 else float("nan")
        tstat = slope / se if se and se == se else float("nan")
        drift = {"n": n, "slope_rps_per_hour": round(float(slope), 3),
                 "t": round(float(tstat), 2), "span_hours": round(float(x.max()), 2),
                 "first": round(float(y[:3].mean()), 1), "last": round(float(y[-3:].mean()), 1)}
        json.dump(drift, open(f"{ROOT}/analysis/campaign_drift.json", "w"), indent=1)
        print("campaign drift (unprotected policy):", json.dumps(drift))
        if abs(tstat) > 3:
            bad.append(("<campaign>", f"baseline goodput trends {slope:+.1f} req/s per hour (t={tstat:.1f})"))
    # per-cell coefficient of variation, reported for the record
    cv = df.groupby("cell").base_goodput.agg(["mean", "std", "count"])
    cv["cv_pct"] = 100 * cv["std"] / cv["mean"]
    cv.sort_values("cv_pct", ascending=False).to_csv(f"{ROOT}/analysis/baseline_cv.csv")

print(f"{len(bad)} suspect run(s)")
for d, why in bad:
    print(" ", os.path.relpath(d, ROOT), "->", why)
json.dump([os.path.relpath(d, ROOT) for d, _ in bad], open(f"{ROOT}/analysis/suspect_runs.json", "w"), indent=1)
