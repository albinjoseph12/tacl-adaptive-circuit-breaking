package io.acb.gateway.policies;

import io.acb.gateway.ResiliencePolicy;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;

/**
 * Comparator: client-side, latency-only adaptive concurrency limiting in the style of Netflix
 * concurrency-limits Gradient2. The limit is moved by the ratio of long-term to short-term RTT
 * (gradient) plus a sqrt(limit) queue allowance, smoothed; a dropped/failed sample shrinks the limit.
 * The policy sees only what the caller can observe (its own response times); it knows nothing
 * about the downstream JVM. There is no breaker: every rejection is pure load shedding.
 */
public final class GradientPolicy implements ResiliencePolicy {
    private final AtomicInteger inflight = new AtomicInteger();
    private final AtomicLong shed = new AtomicLong();
    private volatile double limit = 128;
    private final int minLimit = 8, maxLimit = 512;
    private double longRtt = 0, shortRtt = 0;
    private final double smoothing = 0.2, tolerance = 1.5;
    private long lastUpdate = System.nanoTime();
    private final Object lock = new Object();

    @Override
    public <T> CompletableFuture<T> execute(String route, Supplier<CompletableFuture<T>> call) {
        int cur = inflight.incrementAndGet();
        if (cur > (int) limit) {
            inflight.decrementAndGet();
            shed.incrementAndGet();
            return CompletableFuture.failedFuture(new RejectedException("shed"));
        }
        long t0 = System.nanoTime();
        return call.get().whenComplete((v, e) -> {
            inflight.decrementAndGet();
            onSample(System.nanoTime() - t0, e != null);
        });
    }

    private void onSample(long rttNs, boolean dropped) {
        double rtt = rttNs / 1e6;
        synchronized (lock) {
            shortRtt = shortRtt == 0 ? rtt : 0.9 * shortRtt + 0.1 * rtt;
            longRtt = longRtt == 0 ? rtt : 0.995 * longRtt + 0.005 * rtt;
            long now = System.nanoTime();
            if (now - lastUpdate < 100_000_000L) return;
            lastUpdate = now;
            double queue = Math.sqrt(limit);
            double gradient = Math.max(0.5, Math.min(1.0, tolerance * longRtt / shortRtt));
            double next = dropped ? limit * 0.9 : limit * gradient + queue;
            next = limit * (1 - smoothing) + next * smoothing;
            limit = Math.max(minLimit, Math.min(maxLimit, next));
        }
    }

    @Override
    public Map<String, Object> state() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("policy", name());
        m.put("inflight", inflight.get());
        m.put("breaker_state", "N/A");
        m.put("limit", limit);
        m.put("short_rtt_ms", shortRtt);
        m.put("long_rtt_ms", longRtt);
        m.put("shed_total", shed.get());
        return m;
    }

    @Override
    public String name() { return "gradient"; }
}
