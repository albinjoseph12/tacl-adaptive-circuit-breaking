#!/usr/bin/env bash
# Full experiment matrix: 3 scenarios x 5 policies x N repetitions (rep-major order so every condition gets early coverage)
cd "$(cd "$(dirname "$0")" && pwd)"
REPS=${1:-3}; START=${2:-0}
for rep in $(seq $START $((START+REPS-1))); do
  for s in spike lock gc; do
    for p in none static static-aggr gradient adaptive; do
      ./run_experiment.sh $p $s $rep 150 2>&1 | grep -v JAVA_TOOL | grep -E "done|start"
    done
  done
done
echo "MATRIX COMPLETE"
