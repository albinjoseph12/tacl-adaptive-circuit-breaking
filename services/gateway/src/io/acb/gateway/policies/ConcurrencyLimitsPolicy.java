package io.acb.gateway.policies;

import com.netflix.concurrency.limits.Limiter;
import com.netflix.concurrency.limits.limit.Gradient2Limit;
import com.netflix.concurrency.limits.limiter.SimpleLimiter;
import io.acb.gateway.ResiliencePolicy;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;

/**
 * External-validity check on {@link GradientPolicy}: the same "client-side, latency-only adaptive
 * concurrency limit, no breaker" shape, but decided by the real com.netflix.concurrency-limits
 * library (Apache-2.0) and its actual Gradient2 limit algorithm instead of this testbed's own
 * reimplementation of it. Selected with ACB_POLICY=concurrency-limits.
 *
 * acquire() returning empty means the library itself has no capacity to admit the call (its own
 * shedding decision, made before any downstream work) -- mapped to the same "shed" outcome
 * GradientPolicy reports. A call that completes is scored back with onSuccess(); a call that fails
 * or times out is scored with onDropped(), the library's convention for a sample that should count
 * against the limit rather than be ignored.
 */
public final class ConcurrencyLimitsPolicy implements ResiliencePolicy {
    private final Limiter<Void> limiter = SimpleLimiter.<Void>newBuilder()
            .limit(Gradient2Limit.newDefault())
            .build();
    private final AtomicLong shed = new AtomicLong();

    @Override
    public <T> CompletableFuture<T> execute(String route, Supplier<CompletableFuture<T>> call) {
        Optional<Limiter.Listener> listener = limiter.acquire(null);
        if (listener.isEmpty()) {
            shed.incrementAndGet();
            return CompletableFuture.failedFuture(new RejectedException("shed"));
        }
        Limiter.Listener l = listener.get();
        return call.get().whenComplete((v, e) -> {
            if (e != null) l.onDropped(); else l.onSuccess();
        });
    }

    @Override
    public Map<String, Object> state() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("policy", name());
        m.put("breaker_state", "N/A");
        m.put("limit", -1);
        m.put("shed_total", shed.get());
        return m;
    }

    @Override
    public String name() { return "concurrency-limits"; }
}
