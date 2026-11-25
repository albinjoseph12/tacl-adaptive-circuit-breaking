package io.acb.gateway.policies;

import io.acb.gateway.Config;
import io.acb.gateway.ResiliencePolicy;

import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;

/**
 * Baseline: static threshold circuit breaker implementing the Resilience4j 2.x COUNT_BASED semantics
 * (https://resilience4j.readme.io/docs/circuitbreaker): a ring of the last N call outcomes; once at least
 * minimumNumberOfCalls are recorded, the breaker opens when failureRate >= failureRateThreshold OR
 * slowCallRate >= slowCallRateThreshold (slow = duration >= slowCallDurationThreshold). OPEN rejects all calls
 * for waitDurationInOpenState, then HALF_OPEN admits permittedNumberOfCallsInHalfOpenState probes and
 * re-evaluates the same thresholds over those probes only. State is evaluated on every recorded outcome.
 */
public final class StaticPolicy implements ResiliencePolicy {
    enum State { CLOSED, OPEN, HALF_OPEN }

    private final Config cfg;
    private final AtomicInteger inflight = new AtomicInteger();
    private final AtomicLong rejected = new AtomicLong(), openTransitions = new AtomicLong();
    private final Object lock = new Object();
    private final byte[] fail, slow;       // ring buffers
    private int idx = 0, count = 0, failures = 0, slows = 0;
    private volatile State state = State.CLOSED;
    private long openedAt = 0;
    private int halfOpenPermits = 0, hoDone = 0, hoFail = 0, hoSlow = 0;

    public StaticPolicy(Config cfg) {
        this.cfg = cfg;
        fail = new byte[cfg.windowSize];
        slow = new byte[cfg.windowSize];
    }

    @Override
    public <T> CompletableFuture<T> execute(String route, Supplier<CompletableFuture<T>> call) {
        synchronized (lock) {
            if (state == State.OPEN) {
                if (System.currentTimeMillis() - openedAt >= cfg.waitInOpenMs) {
                    state = State.HALF_OPEN; halfOpenPermits = cfg.permittedHalfOpen; hoDone = hoFail = hoSlow = 0;
                } else {
                    rejected.incrementAndGet();
                    return CompletableFuture.failedFuture(new RejectedException("breaker_open"));
                }
            }
            if (state == State.HALF_OPEN) {
                if (halfOpenPermits <= 0) {
                    rejected.incrementAndGet();
                    return CompletableFuture.failedFuture(new RejectedException("breaker_half_open"));
                }
                halfOpenPermits--;
            }
        }
        inflight.incrementAndGet();
        long t0 = System.nanoTime();
        return call.get().whenComplete((v, e) -> {
            inflight.decrementAndGet();
            long ms = (System.nanoTime() - t0) / 1_000_000;
            record(e != null, ms >= cfg.slowCallMs);
        });
    }

    private void record(boolean failure, boolean isSlow) {
        synchronized (lock) {
            if (state == State.HALF_OPEN) {
                hoDone++; if (failure) hoFail++; if (isSlow) hoSlow++;
                if (hoDone >= cfg.permittedHalfOpen) {
                    double fr = 100.0 * hoFail / hoDone, sr = 100.0 * hoSlow / hoDone;
                    if (fr >= cfg.failureRateThreshold || sr >= cfg.slowCallRateThreshold) open(); else close();
                }
                return;
            }
            if (state == State.OPEN) return;
            if (count == cfg.windowSize) { failures -= fail[idx]; slows -= slow[idx]; } else count++;
            fail[idx] = (byte) (failure ? 1 : 0); slow[idx] = (byte) (isSlow ? 1 : 0);
            failures += fail[idx]; slows += slow[idx];
            idx = (idx + 1) % cfg.windowSize;
            if (count >= cfg.minimumCalls) {
                double fr = 100.0 * failures / count, sr = 100.0 * slows / count;
                if (fr >= cfg.failureRateThreshold || sr >= cfg.slowCallRateThreshold) open();
            }
        }
    }

    private void open() { state = State.OPEN; openedAt = System.currentTimeMillis(); openTransitions.incrementAndGet(); reset(); }
    private void close() { state = State.CLOSED; reset(); }
    private void reset() { Arrays.fill(fail, (byte) 0); Arrays.fill(slow, (byte) 0); idx = count = failures = slows = 0; }

    @Override
    public Map<String, Object> state() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("policy", name());
        m.put("inflight", inflight.get());
        m.put("breaker_state", state.name());
        m.put("limit", -1);
        synchronized (lock) {
            m.put("failure_rate", count > 0 ? 100.0 * failures / count : 0.0);
            m.put("slow_call_rate", count > 0 ? 100.0 * slows / count : 0.0);
            m.put("window_count", count);
        }
        m.put("open_transitions", openTransitions.get());
        m.put("rejected_open_total", rejected.get());
        return m;
    }

    @Override
    public String name() { return "static"; }
}
