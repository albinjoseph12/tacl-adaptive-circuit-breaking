#!/usr/bin/env bash
set -u
ROOT=/Users/albinjoseph/tacl-research/corrected_acb-testbed
EXP_BASE=$ROOT/results_expansion_2026-11-27
EXP=$EXP_BASE/v2_4shard
DSN="dbname=acb user=acb password=acb host=acb-exp-pg port=5432"

reset_db() {
  docker exec acb-exp-pg psql -q -U acb -d acb -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='acb-fault-batch';" >/dev/null 2>&1 || true
  docker exec acb-exp-pg psql -q -U acb -d acb -c "DELETE FROM orders WHERE id > 2000000;" >/dev/null
  docker exec acb-exp-pg psql -q -U acb -d acb -c "VACUUM ANALYZE orders;" >/dev/null
}

stop_jvms() {
  docker rm -f acb-exp-gw acb-exp-cat1 acb-exp-cat2 >/dev/null 2>&1 || true
}

start_stack() {
  local mode=$1 policy=$2 agg=${3:-max}
  stop_jvms
  docker run -d --name acb-exp-cat1 --network acb-exp-net --cpuset-cpus 1 \
    -v "$ROOT/services:/svc:ro" -v "$EXP_BASE/cat_build:/cat_build:ro" \
    -e DB_URL="jdbc:postgresql://acb-exp-pg:5432/acb" -e DB_USER=acb -e DB_PASSWORD=acb \
    -e CATALOG_PORT=8081 -e CATALOG_MGMT_PORT=8091 -e TOMCAT_MAX_THREADS=48 -e HIKARI_MAX_POOL=16 -e HIKARI_CONN_TIMEOUT_MS=2000 \
    eclipse-temurin:21-jre java -Xms256m -Xmx256m -XX:+UseG1GC -XX:MaxGCPauseMillis=50 \
    -cp "/cat_build:/svc/lib/*" io.acb.catalog.CatalogService >/dev/null

  local gw_extra=()
  if [ "$mode" = "2rep" ]; then
    docker run -d --name acb-exp-cat2 --network acb-exp-net --cpuset-cpus 2 \
      -v "$ROOT/services:/svc:ro" -v "$EXP_BASE/cat_build:/cat_build:ro" \
      -e DB_URL="jdbc:postgresql://acb-exp-pg:5432/acb" -e DB_USER=acb -e DB_PASSWORD=acb \
      -e CATALOG_PORT=8082 -e CATALOG_MGMT_PORT=8092 -e TOMCAT_MAX_THREADS=48 -e HIKARI_MAX_POOL=16 -e HIKARI_CONN_TIMEOUT_MS=2000 \
      eclipse-temurin:21-jre java -Xms256m -Xmx256m -XX:+UseG1GC -XX:MaxGCPauseMillis=50 \
      -cp "/cat_build:/svc/lib/*" io.acb.catalog.CatalogService >/dev/null
    gw_extra+=(-e DOWNSTREAM_URL_B=http://acb-exp-cat2:8082 -e TELEMETRY_URL_B=http://acb-exp-cat2:8092 -e ACB_LB_MODE=rr -e "ACB_FLEET_AGG=$agg")
  fi

  local pol_env=()
  case "$policy" in
    none)     pol_env+=(-e ACB_POLICY=none) ;;
    static)   pol_env+=(-e ACB_POLICY=static -e CB_FAILURE_RATE=50 -e CB_SLOW_RATE=50 -e CB_SLOW_MS=1000 -e CB_WAIT_OPEN_MS=10000) ;;
    adaptive) pol_env+=(-e ACB_POLICY=adaptive) ;;
  esac

  sleep 3
  docker run -d --name acb-exp-gw --network acb-exp-net --cpuset-cpus 0 \
    -v "$ROOT/services:/svc:ro" -v "$EXP_BASE/gw_build:/gw_build:ro" \
    "${pol_env[@]}" ${gw_extra[@]+"${gw_extra[@]}"} \
    -e DOWNSTREAM_URL=http://acb-exp-cat1:8081 -e TELEMETRY_URL=http://acb-exp-cat1:8091 \
    -e GATEWAY_PORT=8080 -e CALL_TIMEOUT_MS=3000 \
    eclipse-temurin:21-jre java -Xms256m -Xmx256m -XX:+UseG1GC \
    -cp "/gw_build:/svc/lib/*" io.acb.gateway.Gateway >/dev/null

  for i in $(seq 1 30); do
    docker exec acb-exp-py python -c "import httpx; assert httpx.get('http://acb-exp-gw:8080/api/orders/1',timeout=2).status_code==200" >/dev/null 2>&1 && break
    sleep 1
  done
  # 5s warm-up at 150 req/s
  docker exec acb-exp-py python /testbed/load/open_loop_gen.py --host http://acb-exp-gw:8080 \
    --rate 150 --duration 5 --seed 9999 --concurrency 64 --out /tmp/warmup.csv >/dev/null 2>&1 || true
}

run_one() {
  local tag=$1 mode=$2 policy=$3 agg=$4 rate=$5 dur=$6 fault=$7 ftarget=$8 seed=$9
  local out_rel="results_expansion_2026-11-27/v2_4shard/$tag"
  local out_host="$ROOT/$out_rel"
  mkdir -p "$out_host"
  if [ -s "$out_host/req_s0.csv" ]; then echo "skip $tag"; return; fi

  local t0=$(date +%s)
  reset_db
  start_stack "$mode" "$policy" "$agg"

  if [ "$fault" != "none" ]; then
    docker exec -d acb-exp-py python /testbed/telemetry/inject_fault.py \
      --kind "$fault" --start 6 --hold 18 --hot-rows 5000 \
      --dsn "$DSN" --downstream "$ftarget" --marker "/testbed/$out_rel/markers.csv" >/dev/null 2>&1
  fi

  for sh in 0 1 2 3; do
    docker exec acb-exp-py python /testbed/load/open_loop_gen.py \
      --host http://acb-exp-gw:8080 --rate "$rate" --duration "$dur" --seed "$seed" \
      --shard-count 4 --shard-index $sh --concurrency 128 --timeout 3.5 \
      --out "/testbed/$out_rel/req_s${sh}.csv" > "$out_host/gen_s${sh}.log" 2>&1 &
  done
  wait

  docker exec acb-exp-py python -c "import httpx; open('/testbed/$out_rel/final_state.json','w').write(httpx.get('http://acb-exp-gw:8080/control/state',timeout=3).text)" >/dev/null 2>&1 || true
  stop_jvms
  echo "$(date +%H:%M:%S) done $tag wall=$(( $(date +%s)-t0 ))s"
}

echo "=== EXPANSION 1: n=5 REPLICATION (row_lock, 800 req/s, 4 shards, 5 seeds) ==="
seeds=(202611271 202611272 202611273 202611274 202611275)
for rep in 0 1 2 3 4; do
  s=${seeds[$rep]}
  for pol in none static adaptive; do
    run_one "exp1_n5_lock/${pol}/rep${rep}" "1rep" "$pol" "max" 800 30 "lock" "http://acb-exp-cat1:8091" "$s"
  done
done

echo "=== EXPANSION 2: OFFERED-RATE LOAD SWEEP (200..1200 req/s, 4 shards) ==="
for rate in 200 400 600 800 1000 1200; do
  for pol in none adaptive; do
    run_one "exp2_load_sweep/r${rate}/${pol}" "1rep" "$pol" "max" "$rate" 20 "none" "http://acb-exp-cat1:8091" 202611280
  done
done

echo "=== EXPANSION 3: 2-REPLICA ROUND-ROBIN FLEET (1000 req/s, 4 shards) ==="
for rep in 0 1 2; do
  s=${seeds[$rep]}
  run_one "exp3_2rep_gc/none/rep${rep}"          "2rep" "none"     "max"  1000 30 "gc"   "http://acb-exp-cat2:8092" "$s"
  run_one "exp3_2rep_gc/adaptive_max/rep${rep}"  "2rep" "adaptive" "max"  1000 30 "gc"   "http://acb-exp-cat2:8092" "$s"
  run_one "exp3_2rep_gc/adaptive_mean/rep${rep}" "2rep" "adaptive" "mean" 1000 30 "gc"   "http://acb-exp-cat2:8092" "$s"

  run_one "exp3_2rep_lock/none/rep${rep}"          "2rep" "none"     "max"  1000 30 "lock" "http://acb-exp-cat1:8091" "$s"
  run_one "exp3_2rep_lock/adaptive_max/rep${rep}"  "2rep" "adaptive" "max"  1000 30 "lock" "http://acb-exp-cat1:8091" "$s"
  run_one "exp3_2rep_lock/adaptive_mean/rep${rep}" "2rep" "adaptive" "mean" 1000 30 "lock" "http://acb-exp-cat1:8091" "$s"
done

echo "ALL 3 TACL v2 4-SHARD EXPANSIONS COMPLETE"
