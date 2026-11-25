#!/usr/bin/env python3
"""
Render the manuscript from a template whose numbers are references into the analysis output.

A placeholder has the form

    {{ source | key | format }}

  source  agg      results/analysis/summary_metrics.csv, keyed "scenario/policy/metric"
          ci       results/analysis/extended_numbers.json -> chain_ci, "scenario/policy/metric"
          split    extended_numbers.json -> split,        "scenario/policy/metric"
          sens     extended_numbers.json -> sensitivity,  "config/metric"
          det      results/analysis/detection.json,       "scenario/policy/metric"
          n        number of repetitions,                 "scenario/policy"
  format  mean     mean only, 3 significant figures unless a precision is appended (mean:1)
          ci       mean ± 95% CI half-width  (ci:1 for one decimal)
          sd       mean ± sample standard deviation
          int      mean rounded to an integer
          pct      mean as a percentage with one decimal

Because every number in the rendered manuscript is a lookup, the text cannot disagree with the
measurements; changing the data and re-rendering changes the paper.
"""
import json, math, os, re, sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AN = os.path.join(ROOT, "results", "analysis")

_agg = None
_ext = None
_det = None
_per = None


def agg():
    global _agg, _per
    if _agg is None:
        _agg = pd.read_csv(os.path.join(AN, "summary_metrics.csv"))
        _per = pd.read_csv(os.path.join(AN, "per_run_metrics.csv"))
    return _agg


def ext():
    global _ext
    if _ext is None:
        _ext = json.load(open(os.path.join(AN, "extended_numbers.json")))
    return _ext


def det():
    """Per-run detection latencies, from detection_per_run.csv."""
    global _det
    if _det is None:
        _det = pd.read_csv(os.path.join(AN, "detection_per_run.csv"))
    return _det



def _tcrit(n):
    """Two-sided 95% Student-t critical value for n observations (n-1 degrees of freedom)."""
    if n is None or n < 2:
        return float("nan")
    try:
        from scipy.stats import t as _t
        return float(_t.ppf(0.975, n - 1))
    except Exception:
        pass
    tbl = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
           8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
           15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
           24: 2.064, 29: 2.045, 39: 2.023, 59: 2.000, 99: 1.984}
    df = n - 1
    for k in sorted(tbl):
        if df <= k:
            return tbl[k]
    return 1.96


def ci95(xs):
    xs = [float(x) for x in xs if x == x]
    if not xs:
        return (float("nan"), float("nan"), 0)
    if len(xs) == 1:
        return (xs[0], 0.0, 1)
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
    n = len(xs)
    return (m, _tcrit(n) * sd / math.sqrt(n), n)


_ol = None


def lookup(source, key):
    """Return (mean, half_width, sd, n)."""
    parts = key.split("/")
    if source == "ol":
        # open-loop re-run (Section V-J): "scenario/topology/policy/metric" from
        # openloop_per_run_metrics.csv, produced by analysis/analyze_openloop.py against
        # results/<scenario>[-split]__openloop/<policy>/rep*/ -- never the closed-loop directories
        global _ol
        if _ol is None:
            _ol = pd.read_csv(os.path.join(AN, "openloop_per_run_metrics.csv"))
        scen, topo, pol, metric = parts
        xs = list(pd.to_numeric(_ol[(_ol.scenario == scen) & (_ol.topology == topo) & (_ol.policy == pol)][metric],
                                 errors="coerce").dropna())
        m, h, n = ci95(xs)
        return (m, h, 0.0, n)
    if source == "cix":
        # a metric from extended.py's per-run table (ci_per_run.csv): "scenario/policy/metric"
        global _cix
        try:
            _cix
        except NameError:
            _cix = None
        if _cix is None:
            _cix = pd.read_csv(os.path.join(AN, "ci_per_run.csv"))
        scen, pol, metric = parts
        xs = list(pd.to_numeric(_cix[(_cix.scenario == scen) & (_cix.policy == pol)][metric], errors="coerce").dropna())
        m, h, n = ci95(xs)
        return (m, h, 0.0, n)
    if source in ("agg", "n"):
        scen, pol, metric = (parts + ["", "", ""])[:3]
        agg()
        rows = _per[(_per.scenario == scen) & (_per.policy == pol)]
        if source == "n":
            return (len(rows), 0.0, 0.0, len(rows))
        xs = list(rows[metric])
        m, h, n = ci95(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) if n > 1 else 0.0
        return (m, h, sd, n)
    if source == "pctdiffsplit":
        e = ext(); scen, a, b, metric = parts
        ma = e["split"][scen][a][metric][0]; mb = e["split"][scen][b][metric][0]
        return (100.0 * (ma - mb) / mb, 0.0, 0.0, 0)
    if source == "pctlosssplit":
        e = ext(); scen, worse, better, metric = parts
        mw = e["split"][scen][worse][metric][0]; mb = e["split"][scen][better][metric][0]
        return (100.0 * (mb - mw) / mb, 0.0, 0.0, 0)
    if source == "ratiosplit":
        e = ext(); scen, a, b, metric = parts
        return (e["split"][scen][a][metric][0] / e["split"][scen][b][metric][0], 0.0, 0.0, 0)
    if source == "sensrange":
        e = ext(); metric = parts[0]
        # parameter perturbations only: the "no-*" entries are mechanism ablations (Table IX), not perturbations
        vals = [v[metric][0] for k, v in e["sensitivity"].items() if v[metric][2] > 0 and not k.startswith("no-")]
        return (min(vals), max(vals), 0.0, len(vals))
    if source in ("routeshare", "routecap"):
        # the share of event-window arrivals a route received, and the route cap that share implies
        # under the mean concurrency limit of the run (C_r = max(2, ceil(L * s_r)))
        e = ext()
        pol, route = parts[0], "/".join(parts[1:])
        routes = e["lock_routes"][pol]
        total_rps = sum(v["rps"][0] for v in routes.values())
        share = routes[route]["rps"][0] / total_rps
        if source == "routeshare":
            return (share, 0.0, 0.0, 0)
        agg()
        L = _per[(_per.scenario == "lock") & (_per.policy == pol)]["evtel_limit_mean"].mean()
        return (max(2.0, math.ceil(L * share)), 0.0, 0.0, 0)
    if source == "routerng":
        # min and max of one metric across routes: "policy/metric" over all routes, or
        # "policy/metric/others" over every route except the one the policy sheds most
        e = ext()
        pol, metric = parts[0], parts[1]
        routes = e["lock_routes"][pol]
        if len(parts) > 2 and parts[2] == "others":
            top = max(routes, key=lambda r: routes[r]["share_of_rejects"][0])
            routes = {k: v for k, v in routes.items() if k != top}
        vals = [v[metric][0] for v in routes.values() if v[metric][0] == v[metric][0]]
        return (min(vals), max(vals), 0.0, len(vals))
    if source == "route":
        # per-route behaviour during the lock event: "policy/route/metric"
        e = ext()
        pol, route, metric = parts[0], "/".join(parts[1:-1]), parts[-1]
        m, h, n = e["lock_routes"][pol][route][metric]
        if n and n > 1:
            h = h * _tcrit(n) / 1.96
        return (m, h, 0.0, n)
    if source in ("ci", "split", "sens"):
        e = ext()
        table = {"ci": "chain_ci", "split": "split", "sens": "sensitivity"}[source]
        node = e[table]
        for p in parts[:-1]:
            node = node[p]
        m, h, n = node[parts[-1]]
        if n and n > 1:
            h = h * _tcrit(n) / 1.96
        return (m, h, 0.0, n)
    if source == "pctloss":
        # how much of the reference policy's metric the other policy forfeits, as a positive percentage
        scen, worse, better, metric = parts
        agg()
        mw = _per[(_per.scenario == scen) & (_per.policy == worse)][metric].mean()
        mb = _per[(_per.scenario == scen) & (_per.policy == better)][metric].mean()
        return (100.0 * (mb - mw) / mb, 0.0, 0.0, 0)
    if source == "ratio":
        # ratio of one policy's metric to another's, same scenario
        scen, a, b, metric = parts
        agg()
        ma = _per[(_per.scenario == scen) & (_per.policy == a)][metric].mean()
        mb = _per[(_per.scenario == scen) & (_per.policy == b)][metric].mean()
        return (ma / mb, 0.0, 0.0, 0)
    if source == "pctdiff":
        # percentage by which policy A exceeds policy B on one metric of one scenario
        scen, a, b, metric = parts
        agg()
        ma = _per[(_per.scenario == scen) & (_per.policy == a)][metric].mean()
        mb = _per[(_per.scenario == scen) & (_per.policy == b)][metric].mean()
        return (100.0 * (ma - mb) / mb, 0.0, 0.0, 0)
    if source == "olratio":
        # open-loop table: ratio of one policy's metric to another's, "scenario/topology/a/b/metric"
        scen, topo, a, b, metric = parts
        if _ol is None:
            lookup("ol", f"{scen}/{topo}/{a}/base_goodput_rps")  # populate _ol via the normal path
        ma = _ol[(_ol.scenario == scen) & (_ol.topology == topo) & (_ol.policy == a)][metric].mean()
        mb = _ol[(_ol.scenario == scen) & (_ol.topology == topo) & (_ol.policy == b)][metric].mean()
        return (ma / mb, 0.0, 0.0, 0)
    if source == "olpctdiff":
        # open-loop table: percentage by which policy A exceeds policy B, "scenario/topology/a/b/metric"
        scen, topo, a, b, metric = parts
        if _ol is None:
            lookup("ol", f"{scen}/{topo}/{a}/base_goodput_rps")
        ma = _ol[(_ol.scenario == scen) & (_ol.topology == topo) & (_ol.policy == a)][metric].mean()
        mb = _ol[(_ol.scenario == scen) & (_ol.topology == topo) & (_ol.policy == b)][metric].mean()
        return (100.0 * (ma - mb) / mb, 0.0, 0.0, 0)
    if source == "olpctloss":
        # open-loop table: how much of the better policy's metric the worse one forfeits, "scenario/topology/worse/better/metric"
        scen, topo, worse, better, metric = parts
        if _ol is None:
            lookup("ol", f"{scen}/{topo}/{worse}/base_goodput_rps")
        mw = _ol[(_ol.scenario == scen) & (_ol.topology == topo) & (_ol.policy == worse)][metric].mean()
        mb = _ol[(_ol.scenario == scen) & (_ol.topology == topo) & (_ol.policy == better)][metric].mean()
        return (100.0 * (mb - mw) / mb, 0.0, 0.0, 0)
    if source == "olscalar":
        d = json.load(open(os.path.join(AN, "openloop_scalars.json")))
        return (float(d[parts[0]]), 0.0, 0.0, 0)
    if source == "olcohort":
        # issue-time cohort table (corrects the completion-time-window bug): "scenario/topology/policy/metric"
        global _olc
        try:
            _olc
        except NameError:
            _olc = None
        if _olc is None:
            _olc = pd.read_csv(os.path.join(AN, "openloop_issue_cohort.csv"))
        scen, topo, pol, metric = parts
        row = _olc[(_olc.scenario == scen) & (_olc.topology == topo) & (_olc.policy == pol)]
        return (float(row[metric].values[0]), 0.0, 0.0, 0) if len(row) else (float("nan"), 0.0, 0.0, 0)
    if source == "olcohortpct":
        # an issue-time cohort count as a percentage of the requests issued in that cell
        scen, topo, pol, metric = parts
        lookup("olcohort", f"{scen}/{topo}/{pol}/issued")
        row = _olc[(_olc.scenario == scen) & (_olc.topology == topo) & (_olc.policy == pol)]
        return (100.0 * float(row[metric].values[0]) / float(row["issued"].values[0]), 0.0, 0.0, 0)
    if source == "pctof":
        # one metric as a percentage of another, same scenario and policy
        scen, pol, num, den = parts
        agg()
        rows = _per[(_per.scenario == scen) & (_per.policy == pol)]
        return (100.0 * rows[num].mean() / rows[den].mean(), 0.0, 0.0, 0)
    if source == "drift":
        d = json.load(open(os.path.join(AN, "campaign_drift.json")))
        return (float(d[key]), 0.0, 0.0, int(d.get("n", 0)))
    if source == "campaign":
        # every CLOSED-LOOP run (marked by locust_stats.csv) -- this is the scope the abstract,
        # contributions list and "run order is randomised" paragraph all describe (that campaign's
        # own seed, its own drift test), so this must stay closed-loop-only even though the archive
        # as a whole now also contains the open-loop campaign (see "campaignall" for that total).
        import glob as _g
        n = len([d for d in _g.glob(os.path.join(ROOT, "results", "*", "*", "rep[0-9]*"))
                 if os.path.exists(os.path.join(d, "locust_stats.csv"))])
        return (n, 0.0, 0.0, n)
    if source == "campaignall":
        # closed-loop + open-loop runs combined -- the true total the release contains, used only
        # by the data-availability paragraph. An external review caught that paragraph citing
        # {{ campaign | all }} (138, closed-loop only), silently omitting the entire 114-run
        # open-loop campaign from the archive's own stated contents.
        import glob as _g
        closed = [d for d in _g.glob(os.path.join(ROOT, "results", "*", "*", "rep[0-9]*"))
                  if os.path.exists(os.path.join(d, "locust_stats.csv"))]
        openloop = [d for d in _g.glob(os.path.join(ROOT, "results", "*__openloop", "*", "rep[0-9]*"))
                    if os.path.exists(os.path.join(d, "requests.csv"))]
        n = len(closed) + len(openloop)
        return (n, 0.0, 0.0, n)
    if source == "total":
        # total number of analysed runs, optionally restricted to a scenario
        agg()
        rows = _per if key in ("", "all") else _per[_per.scenario == key]
        return (len(rows), 0.0, 0.0, len(rows))
    if source == "rng":
        # min and max of the per-policy means for one scenario/metric, returned as (lo, hi, 0, n_pol)
        scen, metric = parts
        agg()
        means = [g[metric].mean() for _, g in _per[_per.scenario == scen].groupby("policy") if len(g)]
        means = [m for m in means if m == m]
        return (min(means), max(means), 0.0, len(means))
    if source == "det":
        # median over all runs of a scenario, as the detection table reports
        scen, metric = parts
        d = det()
        xs = pd.to_numeric(d[d.scenario == scen][metric], errors="coerce").dropna()
        if len(xs) == 0:
            return (float("nan"), float("nan"), 0.0, 0)
        return (float(xs.median()), 0.0, 0.0, len(xs))
    if source.startswith("rol") or source == "olrange":
        # The 5-repetition instrumented open-loop re-run (2026-11-20), analysed by the same
        # analysis/analyze_openloop.py into results_openloop_rerun/analysis/. Keys as for ol/olcohort.
        ran = os.path.join(ROOT, "results_openloop_rerun", "analysis")
        if source == "rolscalar":
            d = json.load(open(os.path.join(ran, "openloop_scalars.json")))
            return (float(d[parts[0]]), 0.0, 0.0, 0)
        if source in ("rol", "rolrep", "rolrange", "olrange"):
            pr = pd.read_csv(os.path.join(AN if source == "olrange" else ran, "openloop_per_run_metrics.csv"))
            if source == "rolrep":
                scen, topo, pol, rep, metric = parts
                row = pr[(pr.scenario == scen) & (pr.topology == topo) & (pr.policy == pol) & (pr.rep == rep)]
                return (float(row[metric].values[0]), 0.0, 0.0, 1)
            if source in ("rolrange", "olrange"):
                # min and max over repetitions, optionally excluding one rep: "scen/topo/pol/metric[/exclude_rep]"
                scen, topo, pol, metric = parts[:4]
                sub = pr[(pr.scenario == scen) & (pr.topology == topo) & (pr.policy == pol)]
                if len(parts) > 4:
                    sub = sub[sub.rep != parts[4]]
                xs = pd.to_numeric(sub[metric], errors="coerce").dropna()
                return (float(xs.min()), float(xs.max()), 0.0, len(xs))
            scen, topo, pol, metric = parts
            xs = list(pd.to_numeric(pr[(pr.scenario == scen) & (pr.topology == topo) & (pr.policy == pol)][metric],
                                    errors="coerce").dropna())
            m, h, n = ci95(xs)
            return (m, h, 0.0, n)
        if source in ("rolcohort", "rolcohortpct"):
            c = pd.read_csv(os.path.join(ran, "openloop_issue_cohort.csv"))
            scen, topo, pol, metric = parts
            row = c[(c.scenario == scen) & (c.topology == topo) & (c.policy == pol)]
            if source == "rolcohortpct":
                return (100.0 * float(row[metric].values[0]) / float(row["issued"].values[0]), 0.0, 0.0, 0)
            return (float(row[metric].values[0]), 0.0, 0.0, int(row["reps"].values[0]))
        if source == "rolsplitrep":
            s = pd.read_csv(os.path.join(ran, "openloop_latency_split_per_run.csv"))
            scen, topo, pol, rep, win, metric = parts
            row = s[(s.scenario == scen) & (s.topology == topo) & (s.policy == pol) & (s.rep == rep) & (s.window == win)]
            return (float(row[metric].values[0]), 0.0, 0.0, 1)
        if source == "rolsplit":
            # latency split of successful requests: "scen/topo/pol/window/metric", window base|event
            s = pd.read_csv(os.path.join(ran, "openloop_latency_split.csv"))
            scen, topo, pol, win, metric = parts
            row = s[(s.scenario == scen) & (s.topology == topo) & (s.policy == pol) & (s.window == win)]
            return (float(row[metric].values[0]), 0.0, 0.0, int(row["reps"].values[0]))
    raise KeyError(source)


def sig(x, digits=3):
    if x != x:
        return "—"
    if x == 0:
        return "0"
    d = max(0, digits - 1 - int(math.floor(math.log10(abs(x)))))
    return f"{x:.{d}f}"


def fmt(val, spec):
    m, h, sd, n = val
    name, _, prec = spec.partition(":")
    p = int(prec) if prec else None
    if name == "mean":
        return sig(m) if p is None else f"{m:.{p}f}"
    if name == "int":
        return f"{round(m):d}"
    if name == "pct":
        return f"{m:.1f}" if p is None else f"{m:.{p}f}"
    if name == "ci":
        s = (lambda x: sig(x)) if p is None else (lambda x: f"{x:.{p}f}")
        return f"{s(m)} ± {s(h)}"
    if name == "sd":
        s = (lambda x: sig(x)) if p is None else (lambda x: f"{x:.{p}f}")
        return f"{s(m)} ± {s(sd)}"
    if name == "n":
        return str(n)
    if name == "pctshare":
        return f"{100 * m:.0f}"
    if name == "range":
        s_ = (lambda x: sig(x)) if p is None else (lambda x: f"{x:.{p}f}")
        return f"{s_(m)}–{s_(h)}"
    raise KeyError(spec)


PAT = re.compile(r"\{\{\s*([a-z]+)\s*\|\s*(.+?)\s*\|\s*([a-z0-9]+(?::\d+)?)\s*\}\}")


def render(text):
    misses = []

    def sub(m):
        src, key, spec = m.group(1), m.group(2), m.group(3)
        try:
            return fmt(lookup(src, key), spec)
        except Exception as e:
            misses.append(f"{m.group(0)}  -> {type(e).__name__}: {e}")
            return m.group(0)

    out = PAT.sub(sub, text)
    return out, misses


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    out, misses = render(open(src).read())
    open(dst, "w").write(out)
    print(f"rendered {src} -> {dst}; {len(PAT.findall(open(src).read()))} placeholders, {len(misses)} unresolved")
    for m in misses:
        print("  UNRESOLVED", m)
    sys.exit(1 if misses else 0)
