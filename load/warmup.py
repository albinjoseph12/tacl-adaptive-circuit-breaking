#!/usr/bin/env python3
"""Warm-up traffic (JIT, connection pools, OS caches) before the measured phase of a run."""
import argparse, json, random, threading, time, urllib.request

def worker(stop, base):
    while not stop.is_set():
        try:
            r = random.random()
            if r < 0.45:
                urllib.request.urlopen(f"{base}/api/orders/{random.randint(1, 2_000_000)}", timeout=5).read()
            elif r < 0.70:
                urllib.request.urlopen(f"{base}/api/customers/{random.randint(1, 200_000)}/orders", timeout=5).read()
            elif r < 0.85:
                req = urllib.request.Request(f"{base}/api/orders", data=json.dumps({"customer_id": 1, "product_id": 1, "quantity": 1, "unit_price_cents": 100}).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=5).read()
            else:
                req = urllib.request.Request(f"{base}/api/orders/{random.randint(1, 2_000_000)}/status", data=b'{"status":"PAID"}',
                                             headers={"Content-Type": "application/json"}, method="PUT")
                urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            time.sleep(0.01)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--seconds", type=int, default=15); ap.add_argument("--concurrency", type=int, default=32)
    ap.add_argument("--base", default="http://localhost:8080"); a = ap.parse_args()
    stop = threading.Event()
    ts = [threading.Thread(target=worker, args=(stop, a.base), daemon=True) for _ in range(a.concurrency)]
    [t.start() for t in ts]; time.sleep(a.seconds); stop.set(); time.sleep(0.5)
