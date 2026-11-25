package io.acb.gateway.policies;

import io.acb.gateway.ResiliencePolicy;

import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Supplier;

/** Baseline: no protection. Every call is forwarded; only the per-call timeout applies. */
public final class NonePolicy implements ResiliencePolicy {
    private final AtomicInteger inflight = new AtomicInteger();

    @Override
    public <T> CompletableFuture<T> execute(String route, Supplier<CompletableFuture<T>> call) {
        inflight.incrementAndGet();
        return call.get().whenComplete((v, e) -> inflight.decrementAndGet());
    }

    @Override
    public Map<String, Object> state() {
        return Map.of("policy", name(), "inflight", inflight.get(), "breaker_state", "N/A", "limit", -1);
    }

    @Override
    public String name() { return "none"; }
}
