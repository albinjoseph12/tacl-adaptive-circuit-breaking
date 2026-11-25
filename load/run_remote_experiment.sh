#!/usr/bin/env bash
# Runs the load-generation half of one experiment FROM the load-generator host, against a
# benchmark host's private IP, satisfying Stage 6's "keep the load generator off the measured
# service host." Meant to be invoked while run_experiment.sh is running on the benchmark host
# with SKIP_LOCAL_LOCUST=1 set (see orchestrate_split_experiment.sh, which drives both halves).
#
#   run_remote_experiment.sh <benchmark_host> <scenario> <users> <base_users> <duration_s> <out_dir>
set -euo pipefail
HOST=${1:?benchmark host}; SCENARIO=${2:?scenario}; USERS=${3:?users}; BASE=${4:?base_users}
DURATION=${5:?duration}; OUT=${6:?out_dir}
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$OUT"
# a fixed startup delay lets the benchmark host's services + 15s warmup complete before load starts;
# orchestrate_split_experiment.sh already waits for this on its own side, this is a defensive floor
for i in $(seq 1 60); do curl -sf "http://$HOST:8080/api/orders/1" >/dev/null && break; sleep 1; done
ACB_SCENARIO=$SCENARIO ACB_USERS=$USERS ACB_BASE_USERS=$BASE ACB_DURATION=$DURATION ACB_REQ_LOG="$OUT/requests.csv" \
  locust -f "$ROOT/load/locustfile.py" --headless -H "http://$HOST:8080" --csv "$OUT/locust" --only-summary > "$OUT/locust.log" 2>&1 || true
echo "[$(date +%T)] remote load done -> $OUT"
