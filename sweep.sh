#!/usr/bin/env bash
# Stage 1: the split topology.  Stage 2: one-factor-at-a-time parameter sensitivity.
# Stage 3: additional repetitions of the chain-topology matrix.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"; cd "$ROOT"
log() { echo "[$(date +%T)] $*" | tee -a sweep.log; }

stage_split() {
  for scen in gc lock; do
    for pol in none static static-aggr gradient adaptive; do
      for rep in 1 2 3; do
        [ -s "results/${scen}-split/$pol/rep$rep/requests.csv" ] && continue
        TOPOLOGY=split ./run_experiment.sh "$pol" "$scen" "$rep" 160 >> sweep.log 2>&1
        log "split $scen/$pol/rep$rep done"
      done
    done
  done
}

stage_sensitivity() {
  # one factor at a time around the default configuration (P_HIGH .80, AI 4, BETA .5, THETA 30/40)
  declare -A CFG=(
    [phigh070]="ACB_P_HIGH=0.70"
    [phigh090]="ACB_P_HIGH=0.90"
    [ai2]="ACB_AI=2"
    [ai8]="ACB_AI=8"
    [beta03]="ACB_BETA=0.3"
    [beta07]="ACB_BETA=0.7"
    [theta2030]="ACB_THETA_BASE=20 ACB_THETA_SPAN=30"
    [theta4555]="ACB_THETA_BASE=45 ACB_THETA_SPAN=55"
    [plow040]="ACB_P_LOW=0.40"
  )
  for tag in "${!CFG[@]}"; do
    for rep in 1 2; do
      [ -s "results/lock__$tag/adaptive/rep$rep/requests.csv" ] && continue
      env ${CFG[$tag]} RUN_TAG="$tag" ./run_experiment.sh adaptive lock "$rep" 160 >> sweep.log 2>&1
      log "sensitivity $tag/rep$rep done"
    done
  done
}

stage_chain() {
  # the main matrix, re-run under the database-restoring protocol
  for scen in lock gc spike hang; do
    for pol in none static static-aggr gradient adaptive; do
      for rep in 1 2 3; do
        [ -s "results/$scen/$pol/rep$rep/requests.csv" ] && continue
        ./run_experiment.sh "$pol" "$scen" "$rep" 160 >> sweep.log 2>&1
        log "chain $scen/$pol/rep$rep done"
      done
    done
  done
}

case "${1:-all}" in
  split) stage_split ;;
  sens)  stage_sensitivity ;;
  chain) stage_chain ;;
  *) stage_chain; stage_split; stage_sensitivity ;;
esac
log "ALL DONE"
