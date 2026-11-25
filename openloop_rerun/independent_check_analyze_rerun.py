#!/usr/bin/env python3
"""analyze_rerun.py <rerun_results_dir> <out_dir>

Analyses the 5-rep open-loop re-run with the paper's OWN analysis code (corrected_acb-testbed/analysis:
analyze.load_run / window / SCEN and analyze_openloop's policy lists, event windows and outcome classes), and
compares it with the original 3-rep campaign in corrected_acb-testbed/results. Nothing here changes a definition;
the only differences from analyze_openloop.py are:
  * the directory read (ROOT) is a parameter;
  * the issue-cohort rate divides by the number of reps actually present (analyze_openloop hard-codes 3);
  * reps 0-2 of the re-run use the same seeds as the original's reps 0-2, so they are also compared on their own;
  * the new per-request `sent_ts_ms` splits each latency into issued->sent (queued in the generator, waiting for an
    in-flight slot) and sent->completed (network + server).
Writes: per_run.csv, summary.csv, cohort.csv, compare.csv, split.csv, scalars.json.
"""
import csv, glob, json, os, sys
import numpy as np, pandas as pd

TB = os.path.expanduser("~/tacl-research/corrected_acb-testbed")
sys.path.insert(0, f"{TB}/analysis")
from analyze import load_run, window, SCEN, GOOD, REJECT, FAIL  # noqa: E402
from analyze_openloop import POLICIES, SPLIT_POLICIES, SPLIT_SCENARIOS, EVENT_WINDOWS  # noqa: E402

NEW, OUT = sys.argv[1], sys.argv[2]
ORIG = f"{TB}/results"
os.makedirs(OUT, exist_ok=True)


ALIGN = os.environ.get("ALIGN", "0") == "1"


def load(d, scen):
    """load_run(), plus (ALIGN=1) the event clock: the server's clock (telemetry t0, as load_run already uses) for
    the server-injected faults, the generator's clock (first issue) for the generator-side flash crowd."""
    lr = load_run(d)
    if lr is None or not ALIGN or scen != "spike":
        return lr
    r, t = lr
    r["t"] = (r.ts_ms - r.issued_ts_ms.min()) / 1000.0
    return r, t


def cohort_t0(rr, d, scen):
    if not ALIGN:
        return min(float(r["ts_ms"]) for r in rr)          # analyze_openloop.py's choice: first completion
    if scen == "spike":
        return min(float(r["issued_ts_ms"]) for r in rr)   # generator start
    return float(pd.read_csv(f"{d}/telemetry.csv", usecols=["ts_ms"]).ts_ms.min())  # server clock, as window()


def topologies(scen):
    yield "chain", POLICIES, ""
    if scen in SPLIT_SCENARIOS:
        yield "split", SPLIT_POLICIES, "-split"


def rep_dirs(root, scen, suffix, pol, reps=None):
    ds = sorted(glob.glob(f"{root}/{scen}{suffix}__openloop/{pol}/rep[0-9]*"))
    return [d for d in ds if reps is None or int(d.rsplit("rep", 1)[1]) in reps]


def per_run(root, label):
    rows = []
    for scen, (_, ev, rec) in SCEN.items():
        for topo, pols, suffix in topologies(scen):
            for pol in pols:
                for d in rep_dirs(root, scen, suffix, pol):
                    lr = load(d, scen)
                    if lr is None:
                        continue
                    r, _ = lr
                    if r.t.max() < 140:
                        print("skipping short run", d); continue
                    row = {"source": label, "scenario": scen, "topology": topo, "policy": pol,
                           "rep": int(d.rsplit("rep", 1)[1]), "rows": len(r)}
                    for p, (a, b) in (("base", (20, 58)), ("event", ev), ("rec", rec)):
                        row.update({f"{p}_{k}": v for k, v in window(r, a, b).items()})
                    rows.append(row)
    return pd.DataFrame(rows)


def cohort(root, label, reps=None):
    rows = []
    for scen, (a, b) in EVENT_WINDOWS.items():
        for topo, pols, suffix in topologies(scen):
            for pol in pols:
                n = good = rej = fail = inc = 0
                ds = rep_dirs(root, scen, suffix, pol, reps)
                for d in ds:
                    rr = list(csv.DictReader(open(f"{d}/requests.csv")))
                    if not rr:
                        continue
                    t0 = cohort_t0(rr, d, scen)
                    c = [r for r in rr if a <= (float(r["issued_ts_ms"]) - t0) / 1000 < b]
                    n += len(c)
                    good += sum(r["outcome"] in GOOD for r in c)
                    rej += sum(r["outcome"] in REJECT for r in c)
                    fail += sum(r["outcome"] in FAIL for r in c)
                    inc += sum(not r["completed_ts_ms"] for r in c)
                if n:
                    rows.append({"source": label, "scenario": scen, "topology": topo, "policy": pol, "reps": len(ds),
                                 "issued": n, "eventual_success": good,
                                 "eventual_success_rps": good / ((b - a) * len(ds)),
                                 "rejected_pct": 100 * rej / n, "failed_pct": 100 * fail / n,
                                 "rejected": rej, "failed": fail, "still_incomplete": inc})
    return pd.DataFrame(rows)


def split(root):
    """Latency split for SUCCESSFUL requests, binned by completion time exactly as window() does."""
    rows = []
    for scen, (_, ev, _) in SCEN.items():
        for topo, pols, suffix in topologies(scen):
            for pol in pols:
                parts = {"base": [], "event": []}
                for d in rep_dirs(root, scen, suffix, pol):
                    lr = load(d, scen)
                    if lr is None:
                        continue
                    r, _ = lr
                    ok = r[r.good & r.sent_ts_ms.notna()].copy()
                    ok["queue_ms"] = ok.sent_ts_ms - ok.issued_ts_ms
                    ok["server_ms"] = ok.completed_ts_ms - ok.sent_ts_ms
                    for p, (a, b) in (("base", (20, 58)), ("event", ev)):
                        w = ok[(ok.t >= a) & (ok.t < b)]
                        parts[p].append(pd.DataFrame({"rep": int(d.rsplit("rep", 1)[1]), "q": w.queue_ms,
                                                      "s": w.server_ms, "l": w.latency_ms}))
                for p, lst in parts.items():
                    if not lst:
                        continue
                    per = [x for x in lst if len(x)]
                    if not per:
                        rows.append({"scenario": scen, "topology": topo, "policy": pol, "window": p, "n_ok": 0}); continue
                    # mean over reps of per-rep percentiles (the paper's convention for latency cells)
                    m = lambda col, q: float(np.mean([x[col].quantile(q) for x in per]))
                    rows.append({"scenario": scen, "topology": topo, "policy": pol, "window": p,
                                 "n_ok": int(sum(len(x) for x in per)), "reps": len(per),
                                 "latency_p50": m("l", .5), "queue_p50": m("q", .5), "server_p50": m("s", .5),
                                 "latency_p99": m("l", .99), "queue_p99": m("q", .99), "server_p99": m("s", .99),
                                 "queue_share_of_mean_pct": float(np.mean([100 * x.q.sum() / x.l.sum() for x in per
                                                                           if x.l.sum() > 0]))})
    return pd.DataFrame(rows)


sys.path.insert(0, f"{TB}/paper")
from render import _tcrit  # noqa: E402  (the paper's own Student-t critical value)


def tci(x):
    x = np.asarray([v for v in x if not pd.isna(v)], float)
    if len(x) < 2:
        return np.nan
    return float(_tcrit(len(x)) * x.std(ddof=1) / np.sqrt(len(x)))


if __name__ == "__main__":
    new = per_run(NEW, "rerun"); orig = per_run(ORIG, "original")
    pr = pd.concat([orig, new]); pr.to_csv(f"{OUT}/per_run.csv", index=False)
    metrics = [c for c in new.columns if c.split("_")[0] in ("base", "event", "rec")]
    summ = []
    for (src, sc, tp, po), g in pd.concat([orig, new.assign(source="rerun"), new[new.rep <= 2].assign(source="rerun_rep0-2")]) \
            .groupby(["source", "scenario", "topology", "policy"]):
        row = {"source": src, "scenario": sc, "topology": tp, "policy": po, "n": len(g)}
        for m in metrics:
            row[f"{m}_mean"] = g[m].mean(); row[f"{m}_ci"] = tci(g[m])
        summ.append(row)
    summ = pd.DataFrame(summ); summ.to_csv(f"{OUT}/summary.csv", index=False)

    co = pd.concat([cohort(ORIG, "original"), cohort(NEW, "rerun"), cohort(NEW, "rerun_rep0-2", {0, 1, 2})])
    co.to_csv(f"{OUT}/cohort.csv", index=False)
    sp = split(NEW); sp.to_csv(f"{OUT}/split.csv", index=False)

    # offered-load parity, same definition as analyze_openloop (max relative spread of row counts across policies)
    worst = 0.0
    for scen in SCEN:
        for topo, pols, suffix in topologies(scen):
            for rep in range(5):
                cnt = [int(new[(new.scenario == scen) & (new.topology == topo) & (new.policy == p) & (new.rep == rep)].rows.iloc[0])
                       for p in pols if len(new[(new.scenario == scen) & (new.topology == topo) & (new.policy == p) & (new.rep == rep)])]
                if len(cnt) > 1:
                    worst = max(worst, (max(cnt) - min(cnt)) / max(cnt))
    # same seeds -> same trace: compare re-run rep k with original rep k row counts
    m = orig.merge(new, on=["scenario", "topology", "policy", "rep"], suffixes=("_o", "_n"))
    trace = {"pairs": len(m), "max_abs_row_diff": int((m.rows_o - m.rows_n).abs().max()),
             "max_rel_row_diff_pct": float(100 * ((m.rows_o - m.rows_n).abs() / m.rows_o).max())}
    # comparison of the cells the paper cites
    cmp = []
    keys = ["event_goodput_rps", "event_p50_ms", "event_p99_ms", "event_reject_pct", "event_fail_pct",
            "base_p99_ms", "base_reject_pct", "base_goodput_rps", "rec_goodput_rps"]
    S = summ.set_index(["source", "scenario", "topology", "policy"])
    for idx in S.loc["original"].index:
        for k in keys:
            o = S.loc[("original",) + idx][f"{k}_mean"]; n_ = S.loc[("rerun",) + idx][f"{k}_mean"]
            n3 = S.loc[("rerun_rep0-2",) + idx][f"{k}_mean"]
            cmp.append({"scenario": idx[0], "topology": idx[1], "policy": idx[2], "metric": k, "original": o,
                        "original_ci": S.loc[("original",) + idx][f"{k}_ci"], "rerun": n_,
                        "rerun_ci": S.loc[("rerun",) + idx][f"{k}_ci"], "rerun_rep0_2": n3,
                        "rel_diff_pct": 100 * (n_ - o) / o if o else (0.0 if n_ == 0 else np.nan)})
    cmp = pd.DataFrame(cmp); cmp.to_csv(f"{OUT}/compare.csv", index=False)
    json.dump({"n_runs_rerun": len(new), "n_runs_original": len(orig), "offered_load_parity_pct": 100 * worst,
               "trace_vs_original": trace}, open(f"{OUT}/scalars.json", "w"), indent=1)
    print(json.dumps(json.load(open(f"{OUT}/scalars.json")), indent=1))
