package io.acb.gateway.policies;

import io.acb.gateway.Config;
import io.acb.gateway.ResiliencePolicy;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig;

import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;

/**
 * External-validity check on {@link StaticPolicy}: the same window/threshold/wait/half-open configuration,
 * but decided by the real io.github.resilience4j:resilience4j-circuitbreaker library (Apache-2.0) instead of
 * this testbed's own reimplementation of its semantics. Selected with ACB_POLICY=resilience4j (aggressive
 * tuning via the same CB_* env vars as "static"/"static-aggr", e.g. CB_SLOW_MS=250 CB_WAIT_OPEN_MS=5000).
 *
 * Only the circuit breaker is a real third-party dependency here -- there is no separate admission control
 * or bulkhead in this policy, matching what "static" measures against.
 */
public final class Resilience4jPolicy implements ResiliencePolicy {
    private final CircuitBreaker cb;
    private final AtomicLong rejected = new AtomicLong();

    public Resilience4jPolicy(Config cfg) {
        CircuitBreakerConfig ccfg = CircuitBreakerConfig.custom()
                .slidingWindowType(CircuitBreakerConfig.SlidingWindowType.COUNT_BASED)
                .slidingWindowSize(cfg.windowSize)
                .minimumNumberOfCalls(cfg.minimumCalls)
                .failureRateThreshold((float) cfg.failureRateThreshold)
                .slowCallRateThreshold((float) cfg.slowCallRateThreshold)
                .slowCallDurationThreshold(Duration.ofMillis(cfg.slowCallMs))
                .waitDurationInOpenState(Duration.ofMillis(cfg.waitInOpenMs))
                .permittedNumberOfCallsInHalfOpenState(cfg.permittedHalfOpen)
                // this testbed's own breakers treat "downstream answered but slow" as the only slow-call
                // signal (see StaticPolicy); a call that failed outright is scored as a failure, not also
                // counted against the slow-call rate, so results stay comparable across policies.
                .recordException(t -> true)
                .build();
        this.cb = CircuitBreaker.of("gateway-r4j", ccfg);
    }

    @Override
    public <T> CompletableFuture<T> execute(String route, Supplier<CompletableFuture<T>> call) {
        Supplier<CompletionStage<T>> decorated = CircuitBreaker.decorateCompletionStage(cb, () -> call.get());
        CompletableFuture<T> out = new CompletableFuture<>();
        decorated.get().whenComplete((v, e) -> {
            if (e != null) {
                Throwable cause = e instanceof java.util.concurrent.CompletionException ? e.getCause() : e;
                if (cause instanceof CallNotPermittedException) {
                    rejected.incrementAndGet();
                    out.completeExceptionally(new RejectedException("breaker_open"));
                } else {
                    out.completeExceptionally(cause);
                }
            } else {
                out.complete(v);
            }
        });
        return out;
    }

    @Override
    public Map<String, Object> state() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("policy", name());
        CircuitBreaker.Metrics metrics = cb.getMetrics();
        m.put("inflight", metrics.getNumberOfBufferedCalls() - metrics.getNumberOfFailedCalls() - metrics.getNumberOfSuccessfulCalls());
        m.put("breaker_state", cb.getState().name());
        m.put("limit", -1);
        m.put("failure_rate", (double) metrics.getFailureRate());
        m.put("slow_call_rate", (double) metrics.getSlowCallRate());
        m.put("window_count", metrics.getNumberOfBufferedCalls());
        m.put("open_transitions", -1);
        m.put("rejected_open_total", rejected.get());
        return m;
    }

    @Override
    public String name() { return "resilience4j"; }
}
