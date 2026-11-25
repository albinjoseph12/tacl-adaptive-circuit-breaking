#!/usr/bin/env python3
"""
Aggregate all runs under results/<scenario>/<policy>/rep*/ into per-run and per-condition metrics, and produce
publication figures from the measured data. Nothing here is synthetic: every number is derived from requests.csv
(per-request log from Locust) and telemetry.csv (100 ms poller).

Windows (seconds relative to the first telemetry sample; Locust starts ~1 s later):
  baseline : [20, 58)     steady state before the fault / spike
  event    : [60, 80)     spike scenario  (flash crowd)            | [60, 105) lock and gc scenarios (fault held)
  recovery : [80, 120)    spike           | [105, 145) lock and gc
Recovery time = seconds after the event end until 1-s goodput >= 90% of baseline goodput for 3 consecutive seconds.
"""
import glob, json, os, sys
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "results")
OUT = os.path.join(ROOT, "analysis")
os.makedirs(OUT, exist_ok=True)

POLICIES = ["none", "static", "static-aggr", "resilience4j", "gradient", "concurrency-limits", "adaptive"]
LABELS = {"none": "No protection", "static": "Static CB (R4j defaults)", "static-aggr": "Static CB (SLO-tight)",
          "resilience4j": "Static CB (real Resilience4j)", "gradient": "Gradient limiter",
          "concurrency-limits": "Gradient2 (real concurrency-limits)", "adaptive": "TACL (proposed)"}
COLORS = {"none": "#52514e", "static": "#2a78d6", "static-aggr": "#eda100", "resilience4j": "#6fa8e0",
          "gradient": "#1baf7a", "concurrency-limits": "#7ad9b5", "adaptive": "#eb6834"}
SCEN = {"spike": ("Flash crowd (2.5x offered load, 20 s)", (60, 80), (80, 120)),
        "lock": ("Row-lock contention in PostgreSQL (45 s)", (60, 105), (105, 145)),
        "gc": ("GC pressure in downstream JVM (45 s)", (60, 105), (105, 145)),
        "hang": ("Downstream JVM hang (20 s)", (60, 80), (80, 120))}
GOOD = {"ok", "not_found"}
REJECT = {"shed", "shed_route", "breaker_open", "breaker_half_open"}
FAIL = {"timeout", "downstream_error", "client_error"}


def load_run(d):
    try:
        r = pd.read_csv(f"{d}/requests.csv")
        t = pd.read_csv(f"{d}/telemetry.csv")
    except Exception:
        return None
    if len(r) == 0 or len(t) == 0:
        return None
    t0 = t.ts_ms.min()
    r["t"] = (r.ts_ms - t0) / 1000.0
    t["t"] = (t.ts_ms - t0) / 1000.0
    r["good"] = r.outcome.isin(GOOD)
    r["rej"] = r.outcome.isin(REJECT)
    r["fail"] = r.outcome.isin(FAIL)
    return r, t


def per_second(r, tmax=150):
    s = r.assign(s=r.t.astype(int)).groupby("s")
    g = pd.DataFrame({"req": s.size(), "good": s.good.sum(), "rej": s.rej.sum(), "fail": s.fail.sum()})
    ok = r[r.good].assign(s=lambda x: x.t.astype(int)).groupby("s").latency_ms
    g["p50"] = ok.median(); g["p99"] = ok.quantile(.99)
    return g.reindex(range(0, tmax)).fillna(0)


def window(r, a, b):
    w = r[(r.t >= a) & (r.t < b)]
    ok = w[w.good]
    dur = b - a
    return {"offered_rps": len(w) / dur, "goodput_rps": len(ok) / dur,
            "p50_ms": ok.latency_ms.median() if len(ok) else np.nan,
            "p95_ms": ok.latency_ms.quantile(.95) if len(ok) else np.nan,
            "p99_ms": ok.latency_ms.quantile(.99) if len(ok) else np.nan,
            "success_pct": 100 * len(ok) / max(1, len(w)),
            "reject_pct": 100 * w.rej.sum() / max(1, len(w)),
            "fail_pct": 100 * w.fail.sum() / max(1, len(w)),
            "timeouts": int((w.outcome == "timeout").sum()),
            "wasted_rps": w.fail.sum() / dur}


def recovery_time(ps, base_good, t_end, horizon=45):
    thr = 0.9 * base_good
    seq = 0
    for s in range(int(t_end), int(t_end) + horizon):
        if s in ps.index and ps.loc[s, "good"] >= thr:
            seq += 1
            if seq >= 3:
                return s - 2 - t_end
        else:
            seq = 0
    return float(horizon)


def tel_window(t, a, b):
    w = t[(t.t >= a) & (t.t < b)]
    num = lambda c: pd.to_numeric(w[c], errors="coerce")
    return {"ds_busy_mean": num("ds_tomcat_busy").mean(), "pool_active_mean": num("ds_hikari_active").mean(),
            "pool_pending_mean": num("ds_hikari_pending").mean(), "acq_wait_mean_ms": num("ds_acquire_wait_mean_ms").mean(),
            "gc_pause_1s_mean_ms": num("ds_gc_pause_1s_ms").mean(), "pg_active_mean": num("pg_active").mean(),
            "pg_lock_wait_max": num("pg_waiting_lock").max(), "pressure_mean": num("gw_pressure").mean(),
            "limit_mean": num("gw_limit").mean()}


def breaker_stats(t):
    """Breaker transition counts and open-seconds over whatever slice of the telemetry `t` is
    passed in. Callers that want the event-window figures reported in "event-window statistics"
    tables must pass an already-windowed slice (see tel_window's a/b windowing) -- passing the
    whole run here silently produces an open_seconds that can exceed the event window's own
    length, which is exactly the reconciliation error this docstring exists to prevent."""
    st = t.gw_breaker_state.astype(str)
    opens = ((st == "OPEN") & (st.shift(1) != "OPEN")).sum()
    open_s = (st == "OPEN").sum() * 0.1
    p = pd.to_numeric(t.gw_pressure, errors="coerce")
    premature = (((st == "OPEN") & (st.shift(1) != "OPEN")) & (p.shift(1) < 0.5)).sum()
    return {"breaker_opens": int(opens), "open_seconds": float(open_s), "premature_opens": int(premature)}


def main():
    rows = []
    series = {}
    for scen, (title, ev, rec) in SCEN.items():
        for pol in POLICIES:
            for d in sorted(glob.glob(f"{ROOT}/{scen}/{pol}/rep[0-9]*")):
                lr = load_run(d)
                if lr is None:
                    continue
                r, t = lr
                if r.t.max() < 140:
                    print("skipping short run", d); continue
                ps = per_second(r)
                base = window(r, 20, 58)
                evw = window(r, *ev)
                recw = window(r, *rec)
                row = {"scenario": scen, "policy": pol, "rep": os.path.basename(d)}
                row.update({f"base_{k}": v for k, v in base.items()})
                row.update({f"event_{k}": v for k, v in evw.items()})
                row.update({f"rec_{k}": v for k, v in recw.items()})
                row["recovery_s"] = recovery_time(ps, base["goodput_rps"], ev[1])
                row.update({f"evtel_{k}": v for k, v in tel_window(t, *ev).items()})
                row.update({f"basetel_{k}": v for k, v in tel_window(t, 20, 58).items()})
                # event-window-scoped, consistent with the table these feed into being titled
                # "event-window statistics" -- open_seconds must not be able to exceed (ev[1]-ev[0])
                row.update(breaker_stats(t[(t.t >= ev[0]) & (t.t < ev[1])]))
                # whole-run figures kept separately (e.g. a breaker whose last open interval
                # outlasted the event, discussed in prose) -- never conflated with the table above
                row.update({f"run_{k}": v for k, v in breaker_stats(t).items()})
                rows.append(row)
                series.setdefault((scen, pol), []).append((ps, t))
    df = pd.DataFrame(rows)
    df.to_csv(f"{OUT}/per_run_metrics.csv", index=False)
    agg = df.drop(columns=["rep"]).groupby(["scenario", "policy"]).agg(["mean", "std", "count"])
    agg.columns = ["_".join(c) for c in agg.columns]
    agg.reset_index().to_csv(f"{OUT}/summary_metrics.csv", index=False)

    # ------------------------------------------------------------------ summary table (markdown) for the paper
    cols = ["base_goodput_rps", "base_p99_ms", "event_goodput_rps", "event_p50_ms", "event_p99_ms", "event_success_pct",
            "event_reject_pct", "event_fail_pct", "event_wasted_rps", "rec_goodput_rps", "recovery_s",
            "evtel_pool_pending_mean", "evtel_pg_lock_wait_max", "evtel_gc_pause_1s_mean_ms", "breaker_opens", "premature_opens", "open_seconds"]
    with open(f"{OUT}/summary_table.md", "w") as f:
        for scen in SCEN:
            f.write(f"\n### {scen}\n\n| policy | n | " + " | ".join(cols) + " |\n|" + "---|" * (len(cols) + 2) + "\n")
            for pol in POLICIES:
                g = df[(df.scenario == scen) & (df.policy == pol)]
                if len(g) == 0:
                    continue
                cells = []
                for c in cols:
                    m, s = g[c].mean(), g[c].std()
                    cells.append(f"{m:.1f} ± {0 if np.isnan(s) else s:.1f}")
                f.write(f"| {pol} | {len(g)} | " + " | ".join(cells) + " |\n")

        # ---------------------------------------------------------- ablation table (lock scenario only)
        # Each ablation disables one of the three mechanisms named in AdaptivePolicy's own class
        # javadoc (admission control, adaptive breaker, adaptive bulkheads); "adaptive" itself (all
        # three active) is the baseline row. Rows come from results/lock__<tag>/adaptive/rep[0-9]* --
        # a different path shape (scenario__tag, not scenario/policy) than the main grid above, so
        # they need their own load loop rather than reusing `rows`/`df`.
        ABLATIONS = {"adaptive": "lock", "no-route-aware": "lock__no-route-aware",
                     "no-limit-adaptive": "lock__no-limit-adaptive", "no-theta-adaptive": "lock__no-theta-adaptive"}
        ev, rec = SCEN["lock"][1], SCEN["lock"][2]
        ab_rows = []
        for tag, subdir in ABLATIONS.items():
            for d in sorted(glob.glob(f"{ROOT}/{subdir}/adaptive/rep[0-9]*")):
                lr = load_run(d)
                if lr is None:
                    continue
                r, t = lr
                if r.t.max() < 140:
                    print("skipping short run", d); continue
                base = window(r, 20, 58); evw = window(r, *ev); recw = window(r, *rec)
                row = {"ablation": tag, "rep": os.path.basename(d)}
                row.update({f"base_{k}": v for k, v in base.items()})
                row.update({f"event_{k}": v for k, v in evw.items()})
                row.update({f"rec_{k}": v for k, v in recw.items()})
                row["recovery_s"] = recovery_time(per_second(r), base["goodput_rps"], ev[1])
                ab_rows.append(row)
        if ab_rows:
            abdf = pd.DataFrame(ab_rows)
            abdf.to_csv(f"{OUT}/ablation_metrics.csv", index=False)
            f.write("\n### ablations (lock scenario, adaptive policy)\n\n| ablation | n | " + " | ".join(cols) + " |\n|" + "---|" * (len(cols) + 2) + "\n")
            for tag in ABLATIONS:
                g = abdf[abdf.ablation == tag]
                if len(g) == 0:
                    continue
                cells = []
                for c in cols:
                    if c not in g.columns:
                        cells.append("n/a"); continue
                    m, s = g[c].mean(), g[c].std()
                    cells.append(f"{m:.1f} ± {0 if np.isnan(s) else s:.1f}")
                f.write(f"| {tag} | {len(g)} | " + " | ".join(cells) + " |\n")
    print(open(f"{OUT}/summary_table.md").read())

    # ------------------------------------------------------------------ figures
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9, "axes.spines.top": False, "axes.spines.right": False,
                         "axes.grid": True, "grid.color": "#e6e6e3", "grid.linewidth": 0.6, "axes.edgecolor": "#9a9a97",
                         "figure.dpi": 200, "savefig.dpi": 300, "legend.frameon": False})

    def mean_series(scen, pol, col, src="ps"):
        runs = series.get((scen, pol), [])
        if not runs:
            return None
        if src == "ps":
            m = np.vstack([ps[col].values for ps, _ in runs]); return np.arange(m.shape[1]), m.mean(0)
        else:
            xs = np.arange(0, 150, 0.5)
            m = []
            for _, t in runs:
                v = pd.to_numeric(t[col], errors="coerce")
                m.append(np.interp(xs, t.t.values, v.ffill().fillna(0).values))
            return xs, np.vstack(m).mean(0)

    # Fig: goodput + p99 timelines per scenario (rows) x (goodput, p99, rejections) columns
    fig, axes = plt.subplots(len(SCEN), 3, figsize=(11, 2.7 * len(SCEN)), sharex=True)
    for i, (scen, (title, ev, rec)) in enumerate(SCEN.items()):
        for j, (col, ylab) in enumerate([("good", "Goodput (successful req/s)"), ("p99", "p99 latency of successes (ms)"), ("rej", "Rejected / shed (req/s)")]):
            ax = axes[i, j]
            for pol in POLICIES:
                ms = mean_series(scen, pol, col)
                if ms is None:
                    continue
                x, y = ms
                ax.plot(x, y, color=COLORS[pol], lw=1.4, label=LABELS[pol])
            ax.axvspan(ev[0], ev[1], color="#000", alpha=0.06, lw=0)
            if j == 0:
                ax.set_ylabel(ylab); ax.set_title(title, loc="left", fontsize=9, fontweight="bold")
            else:
                ax.set_ylabel(ylab)
            if j == 1:
                ax.set_yscale("log")
            ax.set_xlim(15, 150)
    for ax in axes[-1]:
        ax.set_xlabel("time (s)")
    h, l = axes[0, 0].get_legend_handles_labels()
    fig.legend(h, l, loc="lower center", ncol=5, fontsize=8, bbox_to_anchor=(0.5, -0.005))
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(f"{OUT}/fig_timelines.png"); plt.close(fig)

    # Fig: controller internals for adaptive policy, each scenario: P, L, pool active/pending, breaker state
    fig, axes = plt.subplots(len(SCEN), 3, figsize=(11, 2.4 * len(SCEN)), sharex=True)
    for i, (scen, (title, ev, rec)) in enumerate(SCEN.items()):
        for j, (col, ylab, src) in enumerate([("gw_pressure", "Pressure index P", "t"), ("gw_limit", "Concurrency limit L", "t"), ("ds_hikari_pending", "Threads waiting for a connection", "t")]):
            ax = axes[i, j]
            ms = mean_series(scen, "adaptive", col, "t")
            if ms is not None:
                ax.plot(*ms, color=COLORS["adaptive"], lw=1.2, label="TACL")
            if col == "ds_hikari_pending":
                for pol in ["none", "static"]:
                    ms2 = mean_series(scen, pol, col, "t")
                    if ms2 is not None:
                        ax.plot(*ms2, color=COLORS[pol], lw=1.0, alpha=0.9, label=LABELS[pol])
                ax.legend(fontsize=6.5, loc="upper right")
            if col == "gw_pressure":
                ax.axhline(0.8, color="#9a9a97", ls="--", lw=0.8); ax.axhline(0.55, color="#9a9a97", ls=":", lw=0.8)
                ax.set_ylim(0, 1.05)
            if col == "gw_limit":
                ax.set_yscale("log")
            ax.axvspan(ev[0], ev[1], color="#000", alpha=0.06, lw=0)
            ax.set_ylabel(ylab)
            if j == 0:
                ax.set_title(title, loc="left", fontsize=9, fontweight="bold")
            ax.set_xlim(15, 150)
    for ax in axes[-1]:
        ax.set_xlabel("time (s)")
    fig.tight_layout(); fig.savefig(f"{OUT}/fig_controller.png"); plt.close(fig)

    # Fig: bar summary: event goodput, event p99, wasted work, recovery time per scenario
    metrics = [("event_goodput_rps", "Goodput during event (req/s)"), ("event_p50_ms", "Median latency of successes (ms)"),
               ("event_p99_ms", "p99 latency of successes (ms)"), ("event_wasted_rps", "Failed after reaching downstream (req/s)")]
    SCEN_B = {k: v for k, v in SCEN.items() if k != "hang"}   # the hang has no successful requests during the event
    fig, axes = plt.subplots(len(SCEN_B), len(metrics), figsize=(11, 2.4 * len(SCEN_B)))
    for i, scen in enumerate(SCEN_B):
        for j, (m, lab) in enumerate(metrics):
            ax = axes[i, j]
            g = df[df.scenario == scen].groupby("policy")[m].agg(["mean", "std"]).reindex(POLICIES).dropna(how="all")
            ax.bar(range(len(g)), g["mean"], yerr=g["std"].fillna(0), color=[COLORS[p] for p in g.index], width=0.62, capsize=2, error_kw={"lw": 0.8})
            ax.set_xticks(range(len(g)))
            short = {"static-aggr": "static\n(tight)", "adaptive": "TACL", "resilience4j": "R4j\n(real)",
                     "concurrency-limits": "Grad2\n(real)", "gradient": "gradient"}
            ax.set_xticklabels([short.get(p, p) for p in g.index], fontsize=6.5, rotation=45, ha="right")
            if i == 0: ax.set_title(lab, fontsize=8.5)
            if j == 0: ax.set_ylabel(SCEN[scen][0].split(" (")[0], fontsize=8.5)
            ax.set_ylim(bottom=0)
            for k, v in enumerate(g["mean"]):
                ax.text(k, v, f"{v:.0f}", ha="center", va="bottom", fontsize=6.5, color="#0b0b0b")
    fig.tight_layout(); fig.savefig(f"{OUT}/fig_summary_bars.png"); plt.close(fig)

    # Fig: latency CDF during the event, per scenario
    fig, axes = plt.subplots(1, len(SCEN_B), figsize=(11, 3.0))
    for i, (scen, (title, ev, rec)) in enumerate(SCEN_B.items()):
        ax = axes[i]
        for pol in POLICIES:
            lat = []
            for d in sorted(glob.glob(f"{ROOT}/{scen}/{pol}/rep[0-9]*")):
                lr = load_run(d)
                if lr is None: continue
                r, _ = lr
                w = r[(r.t >= ev[0]) & (r.t < ev[1]) & r.good]
                lat.append(w.latency_ms.values)
            if not lat: continue
            lat = np.sort(np.concatenate(lat))
            ax.plot(lat, np.linspace(0, 1, len(lat)), color=COLORS[pol], lw=1.3, label=LABELS[pol])
        ax.set_xscale("log"); ax.set_xlabel("latency of successful requests during event (ms)"); ax.set_title(title, fontsize=8.5, loc="left")
        if i == 0: ax.set_ylabel("CDF"); ax.legend(fontsize=7)
    fig.tight_layout(); fig.savefig(f"{OUT}/fig_latency_cdf.png"); plt.close(fig)
    print("figures written to", OUT)


if __name__ == "__main__":
    main()
