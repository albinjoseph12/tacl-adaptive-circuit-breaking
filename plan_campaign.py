#!/usr/bin/env python3
"""
Build the campaign plan: every run of every condition, in a randomised order with a published seed.

Randomising the execution order is the control for uncontrolled drift. The database is restored and
vacuumed before every run, which removes the drift we measured and fixed; randomisation additionally
guarantees that whatever residual drift remains is spread evenly across policies and scenarios
instead of being confounded with them, so it inflates the confidence intervals rather than biasing
any comparison.
"""
import json, random, sys

SEED = 20260907
CHAIN_SCEN = ["lock", "gc", "spike", "hang"]
POLICIES = ["none", "static", "static-aggr", "gradient", "adaptive"]
SPLIT_SCEN = ["gc", "lock"]
SENS = {
    "phigh070": {"ACB_P_HIGH": "0.70"},
    "phigh090": {"ACB_P_HIGH": "0.90"},
    "plow040": {"ACB_P_LOW": "0.40"},
    "ai2": {"ACB_AI": "2"},
    "ai8": {"ACB_AI": "8"},
    "beta03": {"ACB_BETA": "0.3"},
    "beta07": {"ACB_BETA": "0.7"},
    "theta2030": {"ACB_THETA_BASE": "20", "ACB_THETA_SPAN": "30"},
    "theta4555": {"ACB_THETA_BASE": "45", "ACB_THETA_SPAN": "55"},
}
DUR = 160

jobs = []
for scen in CHAIN_SCEN:
    for pol in POLICIES:
        for rep in (1, 2, 3):
            jobs.append({"kind": "chain", "policy": pol, "scenario": scen, "rep": rep,
                         "topology": "chain", "tag": "", "env": {},
                         "out": f"results/{scen}/{pol}/rep{rep}"})
for scen in SPLIT_SCEN:
    for pol in POLICIES:
        for rep in (1, 2, 3):
            jobs.append({"kind": "split", "policy": pol, "scenario": scen, "rep": rep,
                         "topology": "split", "tag": "", "env": {},
                         "out": f"results/{scen}-split/{pol}/rep{rep}"})
for tag, env in SENS.items():
    for rep in (1, 2):
        jobs.append({"kind": "sens", "policy": "adaptive", "scenario": "lock", "rep": rep,
                     "topology": "chain", "tag": tag, "env": env,
                     "out": f"results/lock__{tag}/adaptive/rep{rep}"})

random.Random(SEED).shuffle(jobs)
for i, j in enumerate(jobs):
    j["order"] = i
json.dump({"seed": SEED, "duration_s": DUR, "n": len(jobs), "jobs": jobs},
          open("campaign_plan.json", "w"), indent=1)
print(f"{len(jobs)} runs planned, seed {SEED}")
