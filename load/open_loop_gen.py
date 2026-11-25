#!/usr/bin/env python3
"""
Open-loop, fixed-rate request generator for the TACL testbed.

locustfile.py's closed-loop design (500 users, each looping request -> think-time -> request)
couples the offered rate to how fast the service under test answers: a policy that responds
faster completes more request cycles per second and is therefore offered *more* traffic than a
slower policy in the same wall-clock window, which confounds any goodput/capacity comparison
across policies. This generator removes that confound: every policy is driven by the identical,
pre-generated arrival trace (fixed seed), issued strictly on schedule regardless of how long the
previous request took to answer -- a slow or overloaded policy shows up as timeouts/rejections in
the log, never as a silently smaller offered rate.

Workload (identical distribution and target selection to locustfile.py's OltpUser):
    45%  GET  /api/orders/{id}
    25%  GET  /api/customers/{id}/orders   (Zipf-like popularity, exponent 1.07)
    15%  POST /api/orders
    15%  PUT  /api/orders/{id}/status      (20% of these target the hot-row range [1, HOT_ROWS])

Rate profile:
    spike scenario  : BASE_RATE for [0, flash_start), FLASH_RATE for [flash_start, flash_end),
                       BASE_RATE for the remainder -- matching the ~600 -> ~2,500 req/s regimes
                       locustfile.py's NORMAL_THINK/FLASH_THINK were documented to offer.
    lock/gc/hang    : constant BASE_RATE for the whole run; the fault is injected externally by
                       telemetry/inject_fault.py exactly as before, independent of this generator.

Usage (run from the load-generator host, against the benchmark host's private IP -- the existing
split-host pattern in orchestrate_split_experiment.sh):
    python3 load/open_loop_gen.py --host http://10.128.0.2:8080 --scenario spike \
        --rate 600 --flash-rate 2500 --flash-start 60 --flash-end 80 \
        --duration 160 --seed 20260907 --rep 0 --out results/.../requests.csv
"""
import argparse, asyncio, csv, os, random, sys, time

import httpx

ORDER_ROWS = int(os.environ.get("ACB_ORDER_ROWS", "2000000"))
HOT_ROWS = int(os.environ.get("ACB_HOT_ROWS", "5000"))

NAMES = {
    "get": ("GET /api/orders/{id}", "GET"),
    "cust": ("GET /api/customers/{id}/orders", "GET"),
    "post": ("POST /api/orders", "POST"),
    "put": ("PUT /api/orders/{id}/status", "PUT"),
}


def zipf_customer(rng):
    return int(200_000 ** (rng.random() ** 1.07)) or 1


def build_trace(seed, base_rate, flash_rate, flash_start, flash_end, duration_s):
    """A fixed, reproducible arrival trace -- identical across every policy run with the same
    seed/rate/duration/scenario window, so no comparator sees an easier or harder trace. Each
    inter-arrival gap is exponential with mean 1/rate(t) (a time-varying Poisson process), rate(t)
    switching to flash_rate only inside [flash_start, flash_end)."""
    rng = random.Random(seed)
    trace = []
    t = 0.0
    while t < duration_s:
        rate = flash_rate if (flash_rate and flash_start <= t < flash_end) else base_rate
        t += rng.expovariate(rate)
        if t >= duration_s:
            break
        r = rng.random()
        if r < 0.45:
            kind = "get"; payload = {"oid": rng.randint(1, ORDER_ROWS)}
        elif r < 0.70:
            kind = "cust"; payload = {"cid": zipf_customer(rng)}
        elif r < 0.85:
            kind = "post"
            payload = {"customer_id": rng.randint(1, 200_000), "product_id": rng.randint(1, 50_000),
                       "quantity": rng.choice([1, 1, 1, 2, 3]), "unit_price_cents": rng.randint(299, 19999)}
        else:
            kind = "put"
            oid = rng.randint(1, HOT_ROWS) if rng.random() < 0.20 else rng.randint(1, ORDER_ROWS)
            payload = {"oid": oid, "status": rng.choice(["PAID", "SHIPPED", "DELIVERED"])}
        trace.append((round(t, 4), kind, payload))
    return trace


async def dispatch_one(client, host, item, out_rows, timeout_s, sem):
    scheduled_offset, kind, payload = item
    name, method = NAMES[kind]
    issued_ts = time.time()
    try:
        # bounds time spent waiting for a free pool connection, not just the request itself -- a
        # plain float passed as httpx's `timeout=` was observed NOT to bound pool-wait time under a
        # connection burst against the real gateway (completions trickled in at ~20-30/s regardless
        # of elapsed time, with no individual PoolTimeout ever raised); the semaphore below is the
        # actual fix (bounds concurrent in-flight requests to something the server can serve), this
        # is a second, independent safety net
        async with sem:
            if kind == "get":
                r = await client.get(f"{host}/api/orders/{payload['oid']}", timeout=timeout_s)
            elif kind == "cust":
                r = await client.get(f"{host}/api/customers/{payload['cid']}/orders", timeout=timeout_s)
            elif kind == "post":
                r = await client.post(f"{host}/api/orders", json=payload, timeout=timeout_s)
            else:
                r = await client.put(f"{host}/api/orders/{payload['oid']}/status",
                                      json={"status": payload["status"]}, timeout=timeout_s)
        completed_ts = time.time()
        outcome = r.headers.get("X-ACB-Outcome", "")
        if not outcome:
            outcome = "ok" if r.status_code in (200, 201, 404) else "downstream_error"
        out_rows.append({"scheduled_offset_s": scheduled_offset, "issued_ts_ms": int(issued_ts * 1000),
                          "completed_ts_ms": int(completed_ts * 1000), "ts_ms": int(completed_ts * 1000),
                          "name": name, "method": method, "status": r.status_code, "outcome": outcome,
                          "latency_ms": round((completed_ts - issued_ts) * 1000, 2),
                          "size": len(r.content or b"")})
    except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
        completed_ts = time.time()
        out_rows.append({"scheduled_offset_s": scheduled_offset, "issued_ts_ms": int(issued_ts * 1000),
                          "completed_ts_ms": "", "ts_ms": int(completed_ts * 1000),
                          "name": name, "method": method, "status": 0, "outcome": "client_error",
                          "latency_ms": round((completed_ts - issued_ts) * 1000, 2), "size": 0})
    except asyncio.CancelledError:
        # the generator's own hard deadline (run(): asyncio.wait_for on the gather) fired -- record
        # this arrival as an honest drop rather than silently omitting it from the output entirely,
        # so a run that hit the deadline is visibly different from one where every arrival completed
        completed_ts = time.time()
        out_rows.append({"scheduled_offset_s": scheduled_offset, "issued_ts_ms": int(issued_ts * 1000),
                          "completed_ts_ms": "", "ts_ms": int(completed_ts * 1000),
                          "name": name, "method": method, "status": 0, "outcome": "generator_timeout",
                          "latency_ms": round((completed_ts - issued_ts) * 1000, 2), "size": 0})


async def wait_ready(host, timeout_s=60):
    async with httpx.AsyncClient() as client:
        for _ in range(timeout_s):
            try:
                r = await client.get(f"{host}/api/orders/1", timeout=2.0)
                if r.status_code in (200, 404):
                    return
            except (httpx.ConnectError, httpx.TimeoutException):
                pass
            await asyncio.sleep(1)
    raise RuntimeError(f"{host} never became ready within {timeout_s}s")


async def run(args):
    await wait_ready(args.host)
    trace = build_trace(args.seed, args.rate, args.flash_rate, args.flash_start, args.flash_end, args.duration)
    if args.shard_count > 1:
        # a single asyncio process tops out well short of the 2,500 rps flash-crowd target on this
        # hardware (measured ~230 req/s ceiling, CPU-bound in Python/httpx overhead, not network or
        # server -- confirmed via the server sitting at ~10% CPU while the client pegs one core) --
        # shard the SAME deterministic full-rate trace round-robin across N cooperating processes
        # (this one handles indices [shard_index::shard_count]), each writing its own output file;
        # run_openloop_campaign.sh launches shard_count copies and concatenates the results, so the
        # merged file is exactly the trace a single infinitely-fast process would have produced.
        trace = trace[args.shard_index::args.shard_count]
    scheduled = len(trace)
    out_rows = []
    limits = httpx.Limits(max_connections=800, max_keepalive_connections=800)
    timeout_obj = httpx.Timeout(connect=args.timeout, read=args.timeout, write=args.timeout, pool=args.timeout)
    # bounds concurrent in-flight requests -- without this, a burst of scheduled arrivals (esp. the
    # flash-rate window) creates thousands of simultaneous tasks all racing for the same handful of
    # connections the server can actually service promptly, and requests queue far longer than any
    # per-request timeout suggests they should
    sem = asyncio.Semaphore(args.concurrency)
    t_start = time.time()

    async def progress_reporter():
        while True:
            await asyncio.sleep(2)
            elapsed = time.time() - t_start
            print(f"[progress] t={elapsed:.1f}s dispatched={len(tasks)}/{scheduled} completed={len(out_rows)}",
                  file=sys.stderr, flush=True)

    async with httpx.AsyncClient(limits=limits, timeout=timeout_obj) as client:
        tasks = []
        reporter = asyncio.create_task(progress_reporter())
        for item in trace:
            wait = (t_start + item[0]) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            tasks.append(asyncio.create_task(dispatch_one(client, args.host, item, out_rows, args.timeout, sem)))
        print(f"[progress] all {scheduled} dispatched at t={time.time()-t_start:.1f}s, awaiting completion",
              file=sys.stderr, flush=True)
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=args.duration + args.timeout + 30)
        except asyncio.TimeoutError:
            print(f"[progress] HARD TIMEOUT waiting for tasks; completed={len(out_rows)}/{scheduled}", file=sys.stderr, flush=True)
        reporter.cancel()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts_ms", "name", "method", "status", "outcome", "latency_ms", "size",
                                          "scheduled_offset_s", "issued_ts_ms", "completed_ts_ms"])
        w.writeheader()
        for row in sorted(out_rows, key=lambda r: r["scheduled_offset_s"]):
            w.writerow(row)

    issued = len(out_rows)
    completed = sum(1 for r in out_rows if r["completed_ts_ms"] != "")
    import json
    print(json.dumps({"scheduled": scheduled, "issued": issued, "completed": completed,
                      "client_error": sum(1 for r in out_rows if r["outcome"] == "client_error"),
                      "ok": sum(1 for r in out_rows if r["outcome"] in ("ok", "not_found"))}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="e.g. http://10.128.0.2:8080")
    ap.add_argument("--rate", type=float, required=True, help="baseline arrivals/second")
    ap.add_argument("--flash-rate", type=float, default=0.0, help="0 disables the flash window (lock/gc/hang)")
    ap.add_argument("--flash-start", type=float, default=60.0)
    ap.add_argument("--flash-end", type=float, default=80.0)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--concurrency", type=int, default=400, help="max concurrent in-flight requests")
    ap.add_argument("--shard-count", type=int, default=1)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    try:
        import uvloop
        uvloop.install()
    except ImportError:
        pass
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
