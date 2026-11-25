#!/usr/bin/env bash
# End-to-end benchmark loop for the adaptive circuit-breaking testbed.
#
#   ./run_experiment.sh <policy> <scenario> <rep> [duration_s]
#       policy   : none | static | static-aggr | gradient | adaptive | resilience4j | concurrency-limits
#       scenario : spike | lock | gc | hang | steady
#
# CPU layout (2 vCPU host): CPU1 = downstream tier (PostgreSQL + catalog-service); CPU0 = gateway, Locust, telemetry.
# For each run: fresh JVMs for catalog-service and gateway (so no run inherits JIT/heap state), a 15 s warm-up,
# then Locust (500 users), the 100 ms telemetry logger and the fault injector run concurrently for <duration_s>.
# All artefacts land in results/<scenario>/<policy>/rep<rep>/ :
#   requests.csv   one row per request  (ts_ms,name,method,status,outcome,latency_ms,size)
#   telemetry.csv  one row per 100 ms    (pg_stat_activity, pg_locks, JVM telemetry, controller state)
#   markers.csv    fault start/end timestamps
#   locust_*.csv   Locust's own aggregate statistics
#   *.log          service logs
set -euo pipefail
POLICY=${1:?policy}; SCENARIO=${2:?scenario}; REP=${3:?rep}; DURATION=${4:-180}
# TOPOLOGY=chain (default): gateway -> one catalog service -> PostgreSQL.
# TOPOLOGY=split:           gateway -> two catalog services sharing one PostgreSQL; reads go to A,
#                           writes to B. The bottleneck is shared, the JVM signals are per service,
#                           and the controller folds the two pressure indices with a maximum.
TOPOLOGY=${TOPOLOGY:-chain}
ROOT="$(cd "$(dirname "$0")" && pwd)"
SUB="$SCENARIO"; [ "$TOPOLOGY" = "split" ] && SUB="${SCENARIO}-split"
# RUN_TAG isolates a parameter-sensitivity configuration into its own results subtree
[ -n "${RUN_TAG:-}" ] && SUB="${SUB}__${RUN_TAG}"
OUT="$ROOT/results/$SUB/$POLICY/rep$REP"; mkdir -p "$OUT"
CP="$(ls "$ROOT"/services/lib/*.jar | tr '\n' ':')"
export ACB_DSN="dbname=acb user=acb password=acb host=localhost port=5432"
export ACB_HOT_ROWS=5000

# ---- policy -> gateway environment
GW_ENV=""
case "$POLICY" in
  none)        GW_ENV="ACB_POLICY=none" ;;
  static)      GW_ENV="ACB_POLICY=static CB_FAILURE_RATE=50 CB_SLOW_RATE=50 CB_SLOW_MS=1000 CB_WAIT_OPEN_MS=10000" ;;   # Resilience4j-style defaults
  static-aggr) GW_ENV="ACB_POLICY=static CB_FAILURE_RATE=50 CB_SLOW_RATE=50 CB_SLOW_MS=250 CB_WAIT_OPEN_MS=5000" ;;     # SLO-aligned "tight" tuning (p99 SLO ~250 ms)
  gradient)    GW_ENV="ACB_POLICY=gradient" ;;
  adaptive)    GW_ENV="ACB_POLICY=adaptive" ;;
  resilience4j)       GW_ENV="ACB_POLICY=resilience4j CB_FAILURE_RATE=50 CB_SLOW_RATE=50 CB_SLOW_MS=1000 CB_WAIT_OPEN_MS=10000" ;;  # real io.github.resilience4j, same config as "static"
  concurrency-limits) GW_ENV="ACB_POLICY=concurrency-limits" ;;                                                                     # real com.netflix.concurrency-limits Gradient2Limit
  *) echo "unknown policy $POLICY"; exit 1 ;;
esac
case "$SCENARIO" in
  spike) FAULT=none; USERS=500; BASE=500 ;;
  lock)  FAULT=lock; USERS=500; BASE=500 ;;
  gc)    FAULT=gc;   USERS=500; BASE=500 ;;
  hang)  FAULT=hang; USERS=500; BASE=500; HOLD=20 ;;
  steady) FAULT=none; USERS=${STEADY_USERS:-500}; BASE=$USERS ;;
  *) echo "unknown scenario $SCENARIO"; exit 1 ;;
esac

cleanup() { pkill -f 'io\.acb\.catalog\.CatalogService$' >/dev/null 2>&1 || true; pkill -f 'io\.acb\.gateway\.Gateway$' >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup; sleep 1

# ---- restore the database to its seeded state.
# Every run inserts orders. Without this step the table grows monotonically across a campaign and the
# saturation throughput of the downstream tier drifts downward, which makes runs from different points
# in the campaign non-exchangeable. Rows above the seeded maximum are removed and the table is
# re-analysed before every run, so every run starts from the same 2,000,000-row table.
SEED_MAX_ID=${SEED_MAX_ID:-2000000}
psql -q -U acb -h localhost -d acb -c "DELETE FROM orders WHERE id > $SEED_MAX_ID" >/dev/null
# the delete leaves dead tuples; without a vacuum the heap keeps growing across a campaign even
# though the live row count is constant, and scans slow down with it. Autovacuum does not fire often
# enough at this rate, so the vacuum is explicit and synchronous.
psql -q -U acb -h localhost -d acb -c "VACUUM ANALYZE orders" >/dev/null
# ---- reset DB-side state that a previous run could leave behind (hot rows status churn is harmless; kill stragglers)
psql -q -U acb -h localhost -d acb -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='acb-fault-batch';" >/dev/null
psql -q -U postgres -h localhost -d acb -c "SELECT pg_stat_reset();" >/dev/null

echo "[$(date +%T)] start $POLICY/$SUB/rep$REP (topology=$TOPOLOGY) -> $OUT"
taskset -c 1 java -Xms256m -Xmx256m -XX:+UseG1GC -XX:MaxGCPauseMillis=50 \
     -cp "$ROOT/services/catalog-service/build:$CP" io.acb.catalog.CatalogService > "$OUT/catalog.log" 2>&1 &
DS_ENV=""
if [ "$TOPOLOGY" = "split" ]; then
  # second downstream: same image, same database, its own JVM, ports 8082/8092, half the pool so the
  # write tier saturates first
  CATALOG_PORT=8082 CATALOG_MGMT_PORT=8092 HIKARI_MAX_POOL=${CATALOG_B_POOL:-8} \
  taskset -c 1 java -Xms256m -Xmx256m -XX:+UseG1GC -XX:MaxGCPauseMillis=50 \
       -cp "$ROOT/services/catalog-service/build:$CP" io.acb.catalog.CatalogService > "$OUT/catalog_b.log" 2>&1 &
  DS_ENV="DOWNSTREAM_URL_B=http://localhost:8082 TELEMETRY_URL_B=http://localhost:8092"
fi
sleep 4
env $GW_ENV $DS_ENV TELEMETRY_URL=http://localhost:8091 taskset -c 0 java -Xms256m -Xmx256m -XX:+UseG1GC \
     -cp "$ROOT/services/gateway/build:$CP" io.acb.gateway.Gateway > "$OUT/gateway.log" 2>&1 &
for i in $(seq 1 30); do curl -sf localhost:8080/api/orders/1 >/dev/null && break; sleep 1; done
# in the split topology the write tier is only reached by write routes, so the gateway can answer a
# read before service B is listening; wait for B explicitly or the first writes fail on connect
if [ "$TOPOLOGY" = "split" ]; then
  for i in $(seq 1 30); do curl -sf localhost:8092/telemetry >/dev/null && break; sleep 1; done
  for i in $(seq 1 30); do curl -sf -X POST -H 'Content-Type: application/json' -d '{"customer_id":1,"total_cents":100}' localhost:8080/api/orders >/dev/null && break; sleep 1; done
fi

# ---- warm-up: 15 s of moderate mixed traffic so JIT and pools are hot before measurement
python3 "$ROOT/load/warmup.py" --seconds 15 --concurrency 32 >/dev/null

# ---- measured phase
: > "$OUT/markers.csv"
taskset -c 0 python3 "$ROOT/telemetry/poll_telemetry.py" --downstream http://localhost:8091 --out "$OUT/telemetry.csv" --duration $((DURATION+5)) > "$OUT/telemetry.log" 2>&1 &
TEL=$!
# In the split topology the JVM-level fault is injected into the write tier (service B), so the
# controller has to detect congestion in a service that serves only part of the traffic.
FAULT_MGMT=8091; [ "$TOPOLOGY" = "split" ] && [ "$FAULT" != "lock" ] && FAULT_MGMT=8092
python3 "$ROOT/telemetry/inject_fault.py" --downstream http://localhost:$FAULT_MGMT --kind $FAULT --start 60 --hold ${HOLD:-45} --marker "$OUT/markers.csv" > "$OUT/fault.log" 2>&1 &
FLT=$!
# SKIP_LOCAL_LOCUST=1 (Stage 6): keep the load generator off the measured service host. The
# server-side experiment still starts every downstream/gateway/telemetry/fault process and holds
# open for the measured duration; a remote driver (run_remote_experiment.sh, run from the
# load-generator host) points its own locust at this host's private IP for the same window and
# scp's requests.csv/locust_*.csv back into $OUT afterward.
if [ -n "${SKIP_LOCAL_LOCUST:-}" ]; then
  echo "[$(date +%T)] SKIP_LOCAL_LOCUST set: waiting ${DURATION}s for the remote load generator" >> "$OUT/locust.log"
  sleep "$DURATION"
else
  ACB_SCENARIO=$SCENARIO ACB_USERS=$USERS ACB_BASE_USERS=$BASE ACB_DURATION=$DURATION ACB_REQ_LOG="$OUT/requests.csv" \
    taskset -c 0 locust -f "$ROOT/load/locustfile.py" --headless -H http://localhost:8080 --csv "$OUT/locust" --only-summary > "$OUT/locust.log" 2>&1 || true
fi
wait $FLT || true
wait $TEL || true
curl -s localhost:8080/control/state > "$OUT/final_state.json" || true
# record the controller configuration actually in force, so a sweep cell is self-describing
env | grep -E '^(ACB_|CB_|CATALOG_B_)' | sort > "$OUT/config.env" || true
if [ "$TOPOLOGY" = "split" ]; then curl -s localhost:8092/telemetry > "$OUT/final_catalog_b.json" || true; fi
echo "[$(date +%T)] done  $POLICY/$SUB/rep$REP"
