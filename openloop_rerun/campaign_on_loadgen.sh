#!/usr/bin/env bash
# TACL open-loop campaign, 5 repetitions, re-run 2026-11-19 on a fresh GCP project (same machine types, zone,
# CPU platform, image and package versions as the original). Runs ON tacl-loadgen-vm.
#
# Identical to ../run_openloop_campaign.sh in every experimental parameter (scenarios, rates, flash window,
# seeds = 20260907 + scenario offset + topology offset + rep, 160 s, 16 shards, 500/16 in-flight per shard,
# the server half = corrected_acb-testbed/run_experiment.sh unchanged). Differences, all operational:
#   * the driver runs on the load-generator VM and reaches the benchmark VM by plain SSH over the private
#     network (the original drove both from a laptop through IAP tunnels, several seconds per call);
#   * readiness is polled every 2 s exactly as before, but each poll takes ~0.2 s instead of several s, so the
#     generator starts a few seconds earlier relative to the server's 15 s warm-up;
#   * the server-finished wait uses a non-self-matching pattern (pgrep 'run_[e]xperiment');
#   * order is rep-major (every cell's rep k before any rep k+1) instead of scenario/policy/rep;
#   * the generator is the instrumented copy (adds sent_ts_ms; see overlay/load/open_loop_gen.py).
#
#   ./campaign_on_loadgen.sh            full campaign (190 runs)
#   ./campaign_on_loadgen.sh --smoke    one 100 s spike/none run tagged "smoke" (checks 2,500 req/s delivery)
set -uo pipefail
BENCH=10.128.0.2
SEED_BASE=20260907
DUR=160
SHARDS=16
PER_SHARD_CONCURRENCY=$((500 / SHARDS))
TAG=openloop
LOG=/opt/rerun/campaign.log
mkdir -p /opt/rerun
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }
SSH_B() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$BENCH" "$1" </dev/null; }

scenario_params() {
  case "$1" in
    spike) echo "600 2500 60 80 0" ;;
    lock)  echo "600 0 60 80 1000" ;;
    gc)    echo "600 0 60 80 2000" ;;
    hang)  echo "600 0 60 80 3000" ;;
    *) echo "unknown scenario $1" >&2; return 1 ;;
  esac
}

run_one() {
  local policy=$1 scenario=$2 rep=$3 topology=$4 dur=$5
  read -r rate flash_rate flash_start flash_end sc_off <<< "$(scenario_params "$scenario")"
  local topo_off=0; [ "$topology" = "split" ] && topo_off=10000
  local seed=$((SEED_BASE + sc_off + topo_off + rep))
  local sub="$scenario"; [ "$topology" = "split" ] && sub="${scenario}-split"
  sub="${sub}__${TAG}"
  local bench_out="/opt/acb-testbed/results/$sub/$policy/rep$rep"
  if SSH_B "test -s $bench_out/requests.csv && echo EXISTS" | grep -q EXISTS; then
    log "SKIP $sub/$policy/rep$rep (already has requests.csv)"; return 0
  fi
  log "=== $sub/$policy/rep$rep starting (seed=$seed dur=${dur}s) ==="
  SSH_B "fuser -k -TERM 8080/tcp 8081/tcp 8082/tcp >/dev/null 2>&1; sleep 1; cd /opt/acb-testbed && export PATH=/opt/acb_venv/bin:\$PATH && \
    nohup env SKIP_LOCAL_LOCUST=1 RUN_TAG=$TAG TOPOLOGY=$topology ./run_experiment.sh $policy $scenario $rep $dur \
    < /dev/null > /tmp/server_${sub}_${policy}_${rep}.log 2>&1 & echo LAUNCHED" >/dev/null 2>&1 &
  # the launching ssh is backgrounded locally (as in the original driver): sshd keeps the session open while the
  # server-side run is alive, so a foreground call would only return when the run has finished
  sleep 2
  local ready=0
  for _ in $(seq 1 60); do
    if SSH_B "curl -sf http://localhost:8080/api/orders/1 >/dev/null && echo READY" | grep -q READY; then ready=1; break; fi
    sleep 2
  done
  [ "$ready" = 1 ] || { log "gateway not ready after 120 s: $sub/$policy/rep$rep"; return 1; }
  local out="/opt/rerun/loadgen_out/$sub/$policy/rep$rep"; rm -rf "$out"; mkdir -p "$out"
  local flash_arg=""; [ "$flash_rate" != "0" ] && flash_arg="--flash-rate $flash_rate --flash-start $flash_start --flash-end $flash_end"
  for i in $(seq 0 $((SHARDS-1))); do
    /opt/loadgen_venv/bin/python3 /opt/acb-testbed/load/open_loop_gen.py \
      --host http://$BENCH:8080 --rate $rate $flash_arg --duration $dur --seed $seed \
      --shard-count $SHARDS --shard-index $i --concurrency $PER_SHARD_CONCURRENCY --out $out/shard$i.csv \
      > $out/shard$i.log 2>&1 &
  done
  wait
  { head -1 $out/shard0.csv; for i in $(seq 0 $((SHARDS-1))); do tail -n +2 $out/shard$i.csv; done; } > $out/requests.csv
  local sched=0 iss=0; for i in $(seq 0 $((SHARDS-1))); do
    l=$(tail -1 $out/shard$i.log); s=$(echo "$l" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['scheduled'],d['issued'])" 2>/dev/null || echo "0 0")
    sched=$((sched + ${s% *})); iss=$((iss + ${s#* })); done
  for _ in $(seq 1 60); do SSH_B "pgrep -f 'run_[e]xperiment' >/dev/null && echo BUSY" | grep -q BUSY || break; sleep 3; done
  SSH_B "mkdir -p $bench_out" && scp -q -o BatchMode=yes $out/requests.csv $out/shard*.log "$BENCH:$bench_out/" || { log "copy FAILED $sub/$policy/rep$rep"; return 1; }
  log "=== $sub/$policy/rep$rep done: scheduled=$sched issued=$iss rows=$(($(wc -l < $out/requests.csv) - 1)) ==="
  [ "$iss" -gt 0 ]
}

if [ "${1:-}" = "--smoke" ]; then
  TAG=smoke; log "SMOKE: spike/none/rep0/chain, 100 s"; run_one none spike 0 chain 100; log "SMOKE COMPLETE"; exit 0
fi

CHAIN_POLICIES="none static static-aggr gradient adaptive resilience4j concurrency-limits"
CHAIN_SCENARIOS="spike lock gc hang"
SPLIT_POLICIES="none static static-aggr gradient adaptive"
SPLIT_SCENARIOS="lock gc"
total=$(( (7*4 + 5*2) * 5 )); k=0
log "campaign: $total runs"
for rep in 0 1 2 3 4; do
  for scenario in $CHAIN_SCENARIOS; do for policy in $CHAIN_POLICIES; do
    k=$((k+1)); log "($k/$total)"
    for attempt in 1 2 3; do run_one "$policy" "$scenario" "$rep" chain "$DUR" && break; log "retry $attempt"; sleep 10; done
  done; done
  for scenario in $SPLIT_SCENARIOS; do for policy in $SPLIT_POLICIES; do
    k=$((k+1)); log "($k/$total)"
    for attempt in 1 2 3; do run_one "$policy" "$scenario" "$rep" split "$DUR" && break; log "retry $attempt"; sleep 10; done
  done; done
done
log "OPEN-LOOP CAMPAIGN COMPLETE"
