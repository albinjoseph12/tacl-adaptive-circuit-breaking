#!/usr/bin/env bash
cd "$(cd "$(dirname "$0")" && pwd)"
for rep in 0 1 2; do for p in none static static-aggr gradient adaptive; do
  ./run_experiment.sh $p hang $rep 150 2>&1 | grep -v JAVA_TOOL | grep -E "done|start"
done; done
echo "HANG COMPLETE"
