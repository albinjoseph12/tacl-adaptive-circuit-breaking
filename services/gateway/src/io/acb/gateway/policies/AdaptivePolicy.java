package io.acb.gateway.policies;

import io.acb.gateway.Config;
import io.acb.gateway.DownstreamTelemetry;
import io.acb.gateway.ResiliencePolicy;

import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;

/**
 * Proposed policy - Telemetry-driven Adaptive Circuit breaking and Load shedding (TACL).
 *
 * Two coupled 100 ms control loops driven by the downstream pressure index P (see DownstreamTelemetry):
 *
 * 1. Admission control (load shedding). Concurrency limit L follows a pressure-proportional AIMD rule
 *      P > P_high : L <- max(L_min, L * (1 - beta * (P - P_high) / (1 - P_high)))     (multiplicative decrease)
 *      P < P_low  : L <- min(L_max, L + a)                                              (additive increase)
 *      otherwise  : L <- min(L_max, L + a/4)                                            (slow probe inside the band)
 *    Calls arriving when inflight >= L are shed immediately (503, no downstream work).
 *
 * 2. Adaptive circuit breaker. Same count-based window as the static breaker, but the failure-rate
 *    threshold is a function of P:      theta_f(P) = theta_base + theta_span * (1 - P)
 *    When the downstream JVM is healthy (P small) observed failures are tolerated - they are likely transient
 *    (spike-induced timeout burst) - and the breaker is harder to trip; when the downstream is saturated
 *    (P large) the breaker trips earlier to protect the database. Recovery is telemetry-gated:
 *    OPEN -> HALF_OPEN as soon as P < P_low and min_open has elapsed, and no later than max_open.
 *
 * 3. Adaptive bulkheads (route-aware shedding). The global limit L is apportioned to request classes in proportion
 *    to their *arrival* share s_r (EWMA of admitted+shed arrivals):   C_r = max(C_min, L * s_r).
 *    A class whose downstream service time balloons (e.g. writes blocked on a row lock) needs far more concurrency
 *    than its arrival share and hits its cap first, so it is shed while the other classes keep flowing. This turns
 *    the static bulkhead pattern into one that is sized by observed traffic and shrinks with downstream pressure.
 */
public final class AdaptivePolicy implements ResiliencePolicy {
    enum State { CLOSED, OPEN, HALF_OPEN }

    private final Config cfg;
    private final DownstreamTelemetry telemetry;
    private final AtomicInteger inflight = new AtomicInteger();
    private volatile double limit;
    private volatile State state = State.CLOSED;
    private volatile double lastTheta, lastP;
    private long openedAt = 0, halfOpenAt = 0;
    private int halfOpenPermits = 0, hoDone = 0, hoFail = 0;
    private final byte[] window;
    private int idx = 0, count = 0, failures = 0;
    private final Object lock = new Object();
    private final AtomicLong shed = new AtomicLong(), shedRoute = new AtomicLong(), rejectedOpen = new AtomicLong(), openTransitions = new AtomicLong(),
            decreases = new AtomicLong(), increases = new AtomicLong(), telemetryRecoveries = new AtomicLong(), timerRecoveries = new AtomicLong();

    /** Per-route state for the adaptive bulkheads. */
    static final class Route {
        final AtomicInteger inflight = new AtomicInteger();
        final AtomicLong arrivals = new AtomicLong(), shed = new AtomicLong();
        volatile double share = 0.25;       // EWMA arrival share, initialised uniform
        long lastArrivals = 0;
    }
    private final java.util.concurrent.ConcurrentHashMap<String, Route> routes = new java.util.concurrent.ConcurrentHashMap<>();
    private final int routeCapMin = 2;
    private final boolean routeAware = !"false".equalsIgnoreCase(System.getenv().getOrDefault("ACB_ROUTE_AWARE", "true"));
    // Ablations (Section VI-F): each isolates one of the three mechanisms in the class javadoc above.
    // ACB_ROUTE_AWARE=false already isolates (3) adaptive bulkheads. These two isolate (1) and (2):
    //   ACB_LIMIT_ADAPTIVE=false  -- admission control stops reacting to pressure; limit is pinned at
    //                                limitInit, so only the adaptive breaker (2) and bulkheads (3) remain.
    //   ACB_THETA_ADAPTIVE=false  -- the breaker's failure-rate threshold stops reacting to pressure
    //                                (theta_f(P) collapses to the constant thetaBase), so only admission
    //                                control (1) and bulkheads (3) remain telemetry-driven.
    private final boolean limitAdaptive = !"false".equalsIgnoreCase(System.getenv().getOrDefault("ACB_LIMIT_ADAPTIVE", "true"));
    private final boolean thetaAdaptive = !"false".equalsIgnoreCase(System.getenv().getOrDefault("ACB_THETA_ADAPTIVE", "true"));

    public AdaptivePolicy(Config cfg, DownstreamTelemetry telemetry) {
        this.cfg = cfg;
        this.telemetry = telemetry;
        this.limit = cfg.limitInit;
        this.window = new byte[cfg.windowSize];
        Executors.newSingleThreadScheduledExecutor(r -> { Thread t = new Thread(r, "acb-controller"); t.setDaemon(true); return t; })
                .scheduleAtFixedRate(this::tick, 100, 100, TimeUnit.MILLISECONDS);
    }

    /** 100 ms control tick. */
    void tick() {
        double p = telemetry.pressure();
        lastP = p;
        updateShares();
        if (!limitAdaptive) {
            // ablation: admission control ignores pressure entirely; limit stays pinned at its initial value.
            limit = cfg.limitInit;
        } else if (p > cfg.pHigh) {
            double overshoot = (p - cfg.pHigh) / (1.0 - cfg.pHigh);
            double next = Math.max(cfg.limitMin, limit * (1.0 - cfg.beta * overshoot));
            if (next < limit) decreases.incrementAndGet();
            limit = next;
        } else {
            // additive increase below P_low; a slower upward probe inside the dead band [P_low, P_high] so that the limit
            // is never pinned at L_min by a persistent but sub-critical signal (e.g. a moderate GC pause fraction)
            double inc = p < cfg.pLow ? cfg.additiveIncrease : cfg.additiveIncrease / 4.0;
            double next = Math.min(cfg.limitMax, limit + inc);
            if (next > limit) increases.incrementAndGet();
            limit = next;
        }
        lastTheta = theta(p);
        synchronized (lock) {
            if (state == State.OPEN) {
                long openFor = System.currentTimeMillis() - openedAt;
                boolean byTelemetry = p < cfg.pLow && openFor >= cfg.minOpenMs;
                if (byTelemetry || openFor >= cfg.maxOpenMs) {
                    if (byTelemetry) telemetryRecoveries.incrementAndGet(); else timerRecoveries.incrementAndGet();
                    state = State.HALF_OPEN; halfOpenPermits = cfg.permittedHalfOpen; hoDone = hoFail = 0; halfOpenAt = System.currentTimeMillis();
                }
            } else if (state == State.HALF_OPEN && System.currentTimeMillis() - halfOpenAt >= cfg.maxOpenMs) {
                // watchdog: probes that never completed (e.g. lost connections) must not pin the breaker half-open
                halfOpenPermits = cfg.permittedHalfOpen; hoDone = hoFail = 0; halfOpenAt = System.currentTimeMillis();
            } else if (state == State.CLOSED && count >= cfg.minimumCalls && 100.0 * failures / count >= lastTheta) {
                open();
            }
        }
    }

    /** Arrival-share EWMA per route (alpha 0.1 per 100 ms tick ~ 1 s time constant). */
    private void updateShares() {
        long total = 0; java.util.Map<Route, Long> d = new java.util.HashMap<>();
        for (Route r : routes.values()) { long a = r.arrivals.get(); long delta = a - r.lastArrivals; r.lastArrivals = a; d.put(r, delta); total += delta; }
        if (total == 0) return;
        for (var e : d.entrySet()) { Route r = e.getKey(); double inst = (double) e.getValue() / total; r.share = 0.9 * r.share + 0.1 * inst; }
    }

    private double theta(double p) {
        if (!thetaAdaptive) return cfg.thetaBase; // ablation: breaker threshold ignores pressure entirely.
        return cfg.thetaBase + cfg.thetaSpan * (1.0 - Math.max(0, Math.min(1, p)));
    }

    private void open() { state = State.OPEN; openedAt = System.currentTimeMillis(); openTransitions.incrementAndGet(); reset(); }
    private void reset() { Arrays.fill(window, (byte) 0); idx = count = failures = 0; }

    private void record(boolean failure) {
        synchronized (lock) {
            if (state == State.HALF_OPEN) {
                hoDone++; if (failure) hoFail++;
                if (hoDone >= cfg.permittedHalfOpen) {
                    if (100.0 * hoFail / hoDone >= theta(telemetry.pressure())) open(); else { state = State.CLOSED; reset(); }
                }
                return;
            }
            if (state == State.OPEN) return;
            if (count == cfg.windowSize) failures -= window[idx]; else count++;
            window[idx] = (byte) (failure ? 1 : 0);
            failures += window[idx];
            idx = (idx + 1) % cfg.windowSize;
            if (count >= cfg.minimumCalls && 100.0 * failures / count >= lastTheta) open();
        }
    }

    @Override
    public <T> CompletableFuture<T> execute(String routeKey, Supplier<CompletableFuture<T>> call) {
        Route route = routes.computeIfAbsent(routeKey, k -> new Route());
        route.arrivals.incrementAndGet();
        boolean probe = false;
        synchronized (lock) {
            if (state == State.OPEN) {
                rejectedOpen.incrementAndGet();
                return CompletableFuture.failedFuture(new RejectedException("breaker_open"));
            }
            if (state == State.HALF_OPEN) {
                if (halfOpenPermits <= 0) { rejectedOpen.incrementAndGet(); return CompletableFuture.failedFuture(new RejectedException("breaker_half_open")); }
                halfOpenPermits--;
                probe = true;
            }
        }
        if (probe) {
            // Half-open probes bypass admission control: they are bounded (permittedHalfOpen) and their outcome is the
            // only thing that can close the breaker, so shedding them would deadlock the state machine.
            inflight.incrementAndGet();
            long t0 = System.nanoTime();
            return call.get().whenComplete((v, e) -> {
                inflight.decrementAndGet();
                record(e != null || (System.nanoTime() - t0) / 1_000_000 >= cfg.slowCallMs);
            });
        }
        int cur = inflight.incrementAndGet();
        if (cur > (int) limit) {
            inflight.decrementAndGet();
            shed.incrementAndGet(); route.shed.incrementAndGet();
            return CompletableFuture.failedFuture(new RejectedException("shed"));
        }
        if (routeAware) {
            int cap = Math.max(routeCapMin, (int) Math.ceil(limit * route.share));
            int rc = route.inflight.incrementAndGet();
            if (rc > cap) {
                route.inflight.decrementAndGet(); inflight.decrementAndGet();
                shedRoute.incrementAndGet(); route.shed.incrementAndGet();
                return CompletableFuture.failedFuture(new RejectedException("shed_route"));
            }
        }
        long t0 = System.nanoTime();
        return call.get().whenComplete((v, e) -> {
            inflight.decrementAndGet();
            if (routeAware) route.inflight.decrementAndGet();
            long ms = (System.nanoTime() - t0) / 1_000_000;
            record(e != null || ms >= cfg.slowCallMs);
        });
    }

    @Override
    public Map<String, Object> state() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("policy", name());
        m.put("inflight", inflight.get());
        m.put("breaker_state", state.name());
        m.put("limit", limit);
        m.put("theta_f", lastTheta);
        m.put("pressure_ctl", lastP);
        synchronized (lock) {
            m.put("failure_rate", count > 0 ? 100.0 * failures / count : 0.0);
            m.put("window_count", count);
        }
        m.put("open_transitions", openTransitions.get());
        m.put("shed_total", shed.get());
        m.put("shed_route_total", shedRoute.get());
        StringBuilder rs = new StringBuilder();
        routes.forEach((k, r) -> rs.append(k).append('=').append(String.format("%.2f", r.share)).append('/').append(r.inflight.get()).append('/').append(r.shed.get()).append(';'));
        m.put("routes", rs.toString());
        m.put("rejected_open_total", rejectedOpen.get());
        m.put("limit_decreases", decreases.get());
        m.put("limit_increases", increases.get());
        m.put("recoveries_telemetry", telemetryRecoveries.get());
        m.put("recoveries_timer", timerRecoveries.get());
        return m;
    }

    @Override
    public String name() { return "adaptive"; }
}
