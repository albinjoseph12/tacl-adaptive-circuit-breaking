#!/usr/bin/env python3
"""
Extended analysis for the revised paper: the second topology, the parameter-sensitivity sweep and
confidence intervals over the enlarged repetition count.

It reuses the loaders and window definitions of analyze.py verbatim, so a metric computed here is
the same metric computed there; nothing is recomputed by a second definition.

Outputs (all under results/analysis/):
  split_per_run.csv, split_summary.md     the split topology, per run and aggregated
  sensitivity_per_run.csv, sensitivity.md the one-factor-at-a-time sweep on the lock scenario
  ci_table.md                             mean +/- 95% CI half-width for the headline metrics
  extended_numbers.json                   every number the revised sections quote
"""
import glob, json, os, sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import (POLICIES, LABELS, SCEN, ROOT, OUT, load_run, per_second, window,
                     recovery_time, tel_window, breaker_stats)

HEAD = ["event_goodput_rps", "event_p99_ms", "event_success_pct", "event_reject_pct", "event_fail_pct",
        "event_wasted_rps", "recovery_s", "breaker_opens", "premature_opens"]


def ci95(xs):
    xs = [x for x in xs if x == x]
    if len(xs) == 0:
        return (np.nan, np.nan, 0)
    if len(xs) == 1:
        return (float(xs[0]), 0.0, 1)
    m = float(np.mean(xs)); sd = float(np.std(xs, ddof=1))
    return (m, 1.96 * sd / np.sqrt(len(xs)), len(xs))


def metrics_for(d, scen):
    """One run directory -> the same metric row analyze.py would produce."""
    _, ev, rec = SCEN[scen]
    lr = load_run(d)
    if lr is None:
        return None
    r, t = lr
    if r.t.max() < 140:
        return None
    ps = per_second(r)
    base, evw, recw = window(r, 20, 58), window(r, *ev), window(r, *rec)
    row = {"rep": os.path.basename(d)}
    row.update({f"base_{k}": v for k, v in base.items()})
    row.update({f"event_{k}": v for k, v in evw.items()})
    row.update({f"rec_{k}": v for k, v in recw.items()})
    row["recovery_s"] = recovery_time(ps, base["goodput_rps"], ev[1])
    row.update({f"evtel_{k}": v for k, v in tel_window(t, *ev).items()})
    row.update(breaker_stats(t))
    # time from the end of the event to the first successful request: for the hang scenario this is
    # what separates a breaker that reopens on evidence from one that waits out a timer
    after = r[(r.t >= ev[1]) & r.good]
    row["first_success_after_event_s"] = float(after.t.min() - ev[1]) if len(after) else float("nan")
    # split topology only: the pressure of each tier, if the poller recorded them
    for c in ("gw_pressure_a", "gw_pressure_b"):
        if c in t.columns:
            w = t[(t.t >= ev[0]) & (t.t < ev[1])]
            row[f"evtel_{c}_mean"] = pd.to_numeric(w[c], errors="coerce").mean()
    return row


def collect(subdir, scen, policies=POLICIES):
    rows = []
    for pol in policies:
        for d in sorted(glob.glob(f"{ROOT}/{subdir}/{pol}/rep[0-9]*")):
            m = metrics_for(d, scen)
            if m is None:
                continue
            m.update({"scenario": scen, "subdir": subdir, "policy": pol})
            rows.append(m)
    return rows


def md_table(df, group, cols, path, title):
    lines = [f"\n### {title}\n", "| " + group + " | n | " + " | ".join(cols) + " |",
             "|" + "---|" * (len(cols) + 2)]
    for key, g in df.groupby(group, sort=False):
        cells = []
        for c in cols:
            m, h, n = ci95(list(g[c]))
            cells.append("—" if n == 0 else (f"{m:.1f} ± {h:.1f}" if h > 0 else f"{m:.1f}"))
        lines.append(f"| {key} | {len(g)} | " + " | ".join(cells) + " |")
    open(path, "a").write("\n".join(lines) + "\n")
    return lines


def main():
    numbers = {}

    # ------------------------------------------------------------------ 1. split topology
    split_rows = []
    for scen in ("gc", "lock"):
        split_rows += collect(f"{scen}-split", scen)
    if split_rows:
        sdf = pd.DataFrame(split_rows)
        sdf.to_csv(f"{OUT}/split_per_run.csv", index=False)
        open(f"{OUT}/split_summary.md", "w").write("# Split topology (reads -> service A, writes -> service B)\n")
        for scen in ("gc", "lock"):
            sub = sdf[sdf.scenario == scen]
            if len(sub):
                sub = sub.set_index("policy").loc[[p for p in POLICIES if p in set(sub.policy)]].reset_index()
                md_table(sub, "policy", HEAD, f"{OUT}/split_summary.md", f"{scen} (split topology)")
        numbers["split"] = {
            scen: {pol: {c: ci95(list(sdf[(sdf.scenario == scen) & (sdf.policy == pol)][c])) for c in HEAD}
                   for pol in POLICIES if len(sdf[(sdf.scenario == scen) & (sdf.policy == pol)])}
            for scen in ("gc", "lock")}

    # ------------------------------------------------------------------ 2. parameter sensitivity
    sens_rows = []
    for d in sorted(glob.glob(f"{ROOT}/lock__*")):
        tag = os.path.basename(d).split("__", 1)[1]
        for rd in sorted(glob.glob(f"{d}/adaptive/rep[0-9]*")):
            m = metrics_for(rd, "lock")
            if m is None:
                continue
            m.update({"config": tag, "policy": "adaptive", "scenario": "lock"})
            cfg = os.path.join(rd, "config.env")
            m["env"] = " ".join(l.strip() for l in open(cfg)) if os.path.exists(cfg) else ""
            sens_rows.append(m)
    # the default configuration is the chain-topology lock runs already in the corpus
    for rd in sorted(glob.glob(f"{ROOT}/lock/adaptive/rep[0-9]*")):
        m = metrics_for(rd, "lock")
        if m is None:
            continue
        m.update({"config": "default", "policy": "adaptive", "scenario": "lock"})
        sens_rows.append(m)
    if sens_rows:
        cdf = pd.DataFrame(sens_rows)
        cdf.to_csv(f"{OUT}/sensitivity_per_run.csv", index=False)
        order = ["default"] + sorted(set(cdf.config) - {"default"})
        cdf["config"] = pd.Categorical(cdf.config, order, ordered=True)
        cdf = cdf.sort_values("config")
        open(f"{OUT}/sensitivity.md", "w").write(
            "# Parameter sensitivity, adaptive policy, lock scenario, one factor at a time\n")
        md_table(cdf, "config", HEAD, f"{OUT}/sensitivity.md", "lock, adaptive")
        numbers["sensitivity"] = {c: {k: ci95(list(g[k])) for k in HEAD}
                                  for c, g in cdf.groupby("config", observed=True)}
        # how far the worst configuration falls from the default on each headline metric
        dflt = numbers["sensitivity"].get("default", {})
        spread = {}
        for k in HEAD:
            vals = [(c, v[k][0]) for c, v in numbers["sensitivity"].items() if v[k][2] > 0]
            if not vals or k not in dflt or dflt[k][2] == 0:
                continue
            lo = min(vals, key=lambda x: x[1]); hi = max(vals, key=lambda x: x[1])
            spread[k] = {"default": dflt[k][0], "min": lo, "max": hi,
                         "rel_range_pct": 100 * (hi[1] - lo[1]) / max(1e-9, abs(dflt[k][0]))}
        numbers["sensitivity_spread"] = spread

    # ------------------------------------------------------------------ 3. confidence intervals, chain topology
    ci_rows = []
    for scen in SCEN:
        for r in collect(scen, scen):
            r["scenario"] = scen
            ci_rows.append(r)
    if ci_rows:
        ddf = pd.DataFrame(ci_rows)
        ddf.to_csv(f"{OUT}/ci_per_run.csv", index=False)
        open(f"{OUT}/ci_table.md", "w").write("# Chain topology, mean ± 95% CI half-width\n")
        for scen in SCEN:
            sub = ddf[ddf.scenario == scen]
            if len(sub) == 0:
                continue
            sub = sub.set_index("policy").loc[[p for p in POLICIES if p in set(sub.policy)]].reset_index()
            md_table(sub, "policy", HEAD, f"{OUT}/ci_table.md", scen)
        numbers["chain_ci"] = {
            scen: {pol: {c: ci95(list(ddf[(ddf.scenario == scen) & (ddf.policy == pol)][c])) for c in HEAD}
                   for pol in POLICIES if len(ddf[(ddf.scenario == scen) & (ddf.policy == pol)])}
            for scen in SCEN}

    # ------------------------------------------------------------------ 4. per-route behaviour
    # The route caps are the isolating mechanism, so the paper states what each route saw during the
    # lock event; these are the only per-route numbers it quotes.
    route_rows = []
    for pol in POLICIES:
        for d in sorted(glob.glob(f"{ROOT}/lock/{pol}/rep[0-9]*")):
            lr = load_run(d)
            if lr is None:
                continue
            r, _ = lr
            ev = SCEN["lock"][1]
            w = r[(r.t >= ev[0]) & (r.t < ev[1])]
            dur = ev[1] - ev[0]
            for name, g in w.groupby("name"):
                ok = g[g.good]
                route_rows.append({"policy": pol, "rep": os.path.basename(d), "route": name,
                                   "shed_rps": g.rej.sum() / dur, "rps": len(g) / dur,
                                   "p50_ms": ok.latency_ms.median() if len(ok) else float("nan"),
                                   "p99_ms": ok.latency_ms.quantile(.99) if len(ok) else float("nan"),
                                   "share_of_rejects": g.rej.sum() / max(1, w.rej.sum())})
    if route_rows:
        rr = pd.DataFrame(route_rows)
        rr.to_csv(f"{OUT}/lock_routes_per_run.csv", index=False)
        numbers["lock_routes"] = {
            pol: {route: {c: ci95(list(g[c])) for c in ("shed_rps", "rps", "p50_ms", "p99_ms", "share_of_rejects")}
                  for route, g in rr[rr.policy == pol].groupby("route")}
            for pol in POLICIES if len(rr[rr.policy == pol])}

    json.dump(numbers, open(f"{OUT}/extended_numbers.json", "w"), indent=1, default=float)
    print(json.dumps({k: (len(v) if isinstance(v, dict) else v) for k, v in numbers.items()}, indent=1))
    for f in ("split_summary.md", "sensitivity.md", "ci_table.md"):
        p = f"{OUT}/{f}"
        if os.path.exists(p):
            print("\n" + "=" * 70 + f"\n{f}\n" + open(p).read())


if __name__ == "__main__":
    main()
