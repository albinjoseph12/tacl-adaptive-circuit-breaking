#!/usr/bin/env bash
# Execute the randomised campaign plan, skipping any run already present.
set -uo pipefail
cd "$(dirname "$0")"
DUR=$(python3 -c "import json;print(json.load(open('campaign_plan.json'))['duration_s'])")
N=$(python3 -c "import json;print(json.load(open('campaign_plan.json'))['n'])")
for i in $(seq 0 $((N-1))); do
  read -r kind policy scenario rep topology tag out envs <<< "$(python3 - "$i" <<'PY'
import json, sys
j = json.load(open("campaign_plan.json"))["jobs"][int(sys.argv[1])]
env = " ".join(f"{k}={v}" for k, v in j["env"].items()) or "-"
print(j["kind"], j["policy"], j["scenario"], j["rep"], j["topology"], j["tag"] or "-", j["out"], env)
PY
)"
  [ -s "$out/requests.csv" ] && continue
  EXTRA=""; [ "$envs" != "-" ] && EXTRA="$envs"
  TAGENV=""; [ "$tag" != "-" ] && TAGENV="RUN_TAG=$tag"
  env $EXTRA $TAGENV TOPOLOGY=$topology ./run_experiment.sh "$policy" "$scenario" "$rep" "$DUR" >> campaign.log 2>&1
  echo "[$(date +%T)] $((i+1))/$N $kind $scenario${tag:+/$tag} $policy rep$rep -> $out" | tee -a campaign.log
done
echo "[$(date +%T)] CAMPAIGN COMPLETE" | tee -a campaign.log
