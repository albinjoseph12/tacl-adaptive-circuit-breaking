#!/usr/bin/env python3
"""
Seed the PostgreSQL `orders` table with 2,000,000 synthetic OLTP rows.

The row distribution is chosen to look like a real e-commerce order ledger rather than uniform noise:
  * 200,000 customers with a Zipf-like (heavy-tailed) order count so that some customer_id lookups return 20 rows
    and most return a handful (as in real order histories);
  * 50,000 products, also heavy-tailed;
  * status drawn from a realistic funnel (NEW / PAID / SHIPPED / DELIVERED / CANCELLED / RETURNED);
  * created_at spread over the trailing 24 months with a weekly seasonality.
Rows are streamed with COPY (binary-safe CSV) in 100k-row batches; a deterministic RNG seed makes the data set
reproducible bit-for-bit across runs.
"""
import argparse, csv, io, math, os, random, sys, time
import psycopg2

STATUSES = ["NEW", "PAID", "SHIPPED", "DELIVERED", "CANCELLED", "RETURNED"]
STATUS_W = [0.06, 0.10, 0.12, 0.62, 0.07, 0.03]

def zipf_id(rng, n, alpha=1.07):
    """Heavy-tailed id in [1, n] via inverse CDF approximation (fast, no scipy)."""
    u = rng.random()
    x = int(n ** (u ** alpha))
    return max(1, min(n, x))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=2_000_000)
    ap.add_argument("--batch", type=int, default=100_000)
    ap.add_argument("--dsn", default=os.environ.get("ACB_DSN", "dbname=acb user=acb password=acb host=localhost port=5432"))
    ap.add_argument("--seed", type=int, default=20260905)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    conn = psycopg2.connect(args.dsn)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        DROP TABLE IF EXISTS orders;
        CREATE TABLE orders (
            id               BIGSERIAL PRIMARY KEY,
            customer_id      BIGINT      NOT NULL,
            product_id       BIGINT      NOT NULL,
            quantity         INTEGER     NOT NULL,
            unit_price_cents BIGINT      NOT NULL,
            status           VARCHAR(16) NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL,
            updated_at       TIMESTAMPTZ NOT NULL
        );
    """)
    now = time.time()
    t0 = time.time()
    written = 0
    while written < args.rows:
        n = min(args.batch, args.rows - written)
        buf = io.StringIO()
        w = csv.writer(buf)
        for _ in range(n):
            cid = zipf_id(rng, 200_000)
            pid = zipf_id(rng, 50_000)
            qty = 1 if rng.random() < 0.72 else rng.randint(2, 6)
            price = int(rng.lognormvariate(7.4, 0.9))          # median ~ $16, long tail to a few hundred dollars
            status = rng.choices(STATUSES, STATUS_W)[0]
            age_days = rng.random() ** 0.8 * 730                 # denser recent history
            dow_boost = 0.0 if rng.random() < 0.5 else rng.random() * 1.5
            ts = now - age_days * 86400 - dow_boost * 3600
            created = time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime(ts))
            updated = time.strftime("%Y-%m-%d %H:%M:%S+00", time.gmtime(ts + rng.random() * 5 * 86400))
            w.writerow([cid, pid, qty, price, status, created, updated])
        buf.seek(0)
        cur.copy_expert("COPY orders(customer_id, product_id, quantity, unit_price_cents, status, created_at, updated_at) FROM STDIN WITH (FORMAT csv)", buf)
        written += n
        print(f"  seeded {written:,}/{args.rows:,} rows ({time.time()-t0:.1f}s)", flush=True)

    print("creating indexes ...", flush=True)
    cur.execute("CREATE INDEX orders_customer_created_idx ON orders(customer_id, created_at DESC);")
    cur.execute("CREATE INDEX orders_product_idx ON orders(product_id);")
    cur.execute("ANALYZE orders;")
    cur.execute("SELECT count(*), pg_size_pretty(pg_total_relation_size('orders')) FROM orders;")
    cnt, size = cur.fetchone()
    print(f"done: {cnt:,} rows, total relation size {size}, {time.time()-t0:.1f}s")

if __name__ == "__main__":
    main()
