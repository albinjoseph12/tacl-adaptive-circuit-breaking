"""
Locust workload for the adaptive circuit-breaking testbed.

500 concurrent users execute a mixed OLTP profile against the gateway:
    45%  GET  /api/orders/{id}                point read
    25%  GET  /api/customers/{id}/orders      index range read
    15%  POST /api/orders                     insert
    15%  PUT  /api/orders/{id}/status         update; 20% of updates target a "hot" id range [1, HOT_ROWS]
                                              so that a long-running transaction holding row locks on that range
                                              (the lock-contention fault) blocks a realistic share of writes.

Every request outcome is appended to a per-request CSV (ts_ms, name, method, status, outcome, latency_ms, size)
via the request event hook, so the analysis can compute goodput, error and shed rates on any window.
The user count over time is controlled by LoadTestShape (scenario selected with ACB_SCENARIO).
"""
import csv, json, os, random, time
from locust import FastHttpUser, task, between, events, LoadTestShape

ORDER_ROWS = int(os.environ.get("ACB_ORDER_ROWS", "2000000"))
HOT_ROWS = int(os.environ.get("ACB_HOT_ROWS", "5000"))
OUT = os.environ.get("ACB_REQ_LOG", "requests.csv")
SCENARIO = os.environ.get("ACB_SCENARIO", "spike")
USERS = int(os.environ.get("ACB_USERS", "500"))
BASE_USERS = int(os.environ.get("ACB_BASE_USERS", "150"))
DURATION = int(os.environ.get("ACB_DURATION", "180"))

_f = open(OUT, "w", buffering=1 << 16, newline="")
_w = csv.writer(_f)
_w.writerow(["ts_ms", "name", "method", "status", "outcome", "latency_ms", "size"])
_T0 = time.time()


@events.request.add_listener
def on_request(request_type, name, response_time, response_length, response, context, exception, **kw):
    status = getattr(response, "status_code", 0) if response is not None else 0
    outcome = ""
    try:
        outcome = (response.headers or {}).get("X-ACB-Outcome", "") if response is not None else ""
    except Exception:
        pass
    if exception is not None and not outcome:
        outcome = "client_error"
    _w.writerow([int(time.time() * 1000), name, request_type, status, outcome, f"{response_time:.2f}", response_length or 0])


@events.quitting.add_listener
def on_quit(**kw):
    _f.flush(); _f.close()


# Think-time regimes (seconds). NORMAL: 500 users offer ~600 req/s (about 60-65% of the measured ~950 req/s capacity
# of the single-vCPU downstream tier). FLASH: the same 500 users re-issue requests every 0.1-0.3 s (a flash crowd /
# refresh storm) and offer ~2,500 req/s, i.e. a ~2.5x overload of the downstream.
NORMAL_THINK = (0.4, 1.2)
FLASH_THINK = (0.1, 0.3)
_think = {"range": NORMAL_THINK}


class OltpUser(FastHttpUser):
    def wait_time(self):
        lo, hi = _think["range"]
        return random.uniform(lo, hi)

    connection_timeout = 5.0
    network_timeout = 5.0

    def _ok(self, resp, name):
        # 2xx = success; 404 on random ids is a legitimate answer; everything else is a failure for Locust stats
        if resp.status_code in (200, 201, 404):
            resp.success()
        else:
            resp.failure(f"{resp.status_code} {(resp.headers or {}).get('X-ACB-Outcome', '')}")

    @task(45)
    def get_order(self):
        oid = random.randint(1, ORDER_ROWS)
        with self.client.get(f"/api/orders/{oid}", name="GET /api/orders/{id}", catch_response=True) as r:
            self._ok(r, "get")

    @task(25)
    def customer_orders(self):
        cid = int(200_000 ** (random.random() ** 1.07)) or 1
        with self.client.get(f"/api/customers/{cid}/orders", name="GET /api/customers/{id}/orders", catch_response=True) as r:
            self._ok(r, "cust")

    @task(15)
    def create_order(self):
        body = {"customer_id": random.randint(1, 200_000), "product_id": random.randint(1, 50_000),
                "quantity": random.choice([1, 1, 1, 2, 3]), "unit_price_cents": random.randint(299, 19999)}
        with self.client.post("/api/orders", json=body, name="POST /api/orders", catch_response=True) as r:
            self._ok(r, "post")

    @task(15)
    def update_status(self):
        oid = random.randint(1, HOT_ROWS) if random.random() < 0.20 else random.randint(1, ORDER_ROWS)
        with self.client.put(f"/api/orders/{oid}/status", json={"status": random.choice(["PAID", "SHIPPED", "DELIVERED"])},
                             name="PUT /api/orders/{id}/status", catch_response=True) as r:
            self._ok(r, "put")


class Shape(LoadTestShape):
    """
    All scenarios run USERS (500) concurrent users for the whole run.
    spike     : flash crowd - think time switches from NORMAL to FLASH between 60 s and 80 s (transient 2.5x overload)
    lock      : constant NORMAL load; DB row-lock contention fault injected externally at 60 s for 45 s
    gc        : constant NORMAL load; GC allocation storm injected externally at 60 s for 45 s
    steady    : constant load, no fault (capacity characterisation; ACB_THINK=flash for the overload point)
    """
    def tick(self):
        t = self.get_run_time()
        if t > DURATION:
            return None
        if SCENARIO == "spike":
            _think["range"] = FLASH_THINK if 60 <= t < 80 else NORMAL_THINK
        elif SCENARIO == "steady" and os.environ.get("ACB_THINK", "normal") == "flash":
            _think["range"] = FLASH_THINK
        return (USERS, 1000)
