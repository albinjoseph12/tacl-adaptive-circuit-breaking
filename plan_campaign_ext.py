#!/usr/bin/env python3
"""
Incremental extension to the pre-registered 108-run campaign (campaign_plan.json, already executed
in full -- see campaign.log). Adds the two new real-library comparators (resilience4j,
concurrency-limits) across the same four chain scenarios as every other policy in the main matrix,
and three ablations of the adaptive policy (one per mechanism named in AdaptivePolicy's class
javadoc), each on the "lock" scenario to match how the existing sensitivity sweep (SENS in
plan_campaign.py) already isolates one factor at a time on that same scenario.

A separate plan/runner (rather than folding into campaign_plan.json and re-running run_campaign.sh)
because run_campaign.sh's "skip if already done" check looks for results/<...>/requests.csv, which
this testbed never produces (it writes locust_stats.csv instead) -- so re-running the original
campaign runner against a regenerated plan would silently redo all 108 already-completed jobs.
"""
import json, random

SEED = 20260913
CHAIN_SCEN = ["lock", "gc", "spike", "hang"]
NEW_COMPARATORS = ["resilience4j", "concurrency-limits"]
ABLATIONS = {
    "no-route-aware": {"ACB_ROUTE_AWARE": "false"},
    "no-limit-adaptive": {"ACB_LIMIT_ADAPTIVE": "false"},
    "no-theta-adaptive": {"ACB_THETA_ADAPTIVE": "false"},
}
DUR = 160

jobs = []
for scen in CHAIN_SCEN:
    for pol in NEW_COMPARATORS:
        for rep in (1, 2, 3):
            jobs.append({"kind": "chain", "policy": pol, "scenario": scen, "rep": rep,
                         "topology": "chain", "tag": "", "env": {},
                         "out": f"results/{scen}/{pol}/rep{rep}"})
for tag, env in ABLATIONS.items():
    for rep in (1, 2):
        jobs.append({"kind": "ablation", "policy": "adaptive", "scenario": "lock", "rep": rep,
                     "topology": "chain", "tag": tag, "env": env,
                     "out": f"results/lock__{tag}/adaptive/rep{rep}"})

random.Random(SEED).shuffle(jobs)
for i, j in enumerate(jobs):
    j["order"] = i
json.dump({"seed": SEED, "duration_s": DUR, "n": len(jobs), "jobs": jobs},
          open("campaign_plan_ext.json", "w"), indent=1)
print(f"{len(jobs)} runs planned, seed {SEED}")
