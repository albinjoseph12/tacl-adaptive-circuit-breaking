package io.acb.gateway;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Polls the downstream /telemetry endpoint every 100 ms (on a dedicated connection, off the request path)
 * and folds the raw JVM signals into a pressure index P in [0,1]:
 *
 *   P = max( thread_sat, 0.9*pool_sat, pending/max_pool, min(1, acquire_wait/wait_ref), min(1, gc_pause_1s/gc_ref) )
 *
 * The maximum is used because the bottleneck resource - whichever it is - determines whether additional
 * load will queue. A full but non-queuing pool is discounted (0.9) so that only actual queueing
 * (pending > 0 or acquire wait > 0) or GC stall drives P above P_high. The controller reads a light
 * EWMA (alpha 0.5 per tick) of P; unreachable downstream is treated as P = 1.
 */
public final class DownstreamTelemetry {

    public record Sample(long tsMs, double threadSat, double poolSat, double poolPending, double acquireWaitMs,
                         double gcPause1sMs, double heapUtil, boolean reachable) {}

    private static final ObjectMapper JSON = new ObjectMapper();
    private final Config cfg;
    private final HttpClient client;
    private final HttpRequest request;
    private final HttpRequest requestB;                 // second downstream, split topology only
    private volatile Sample last = new Sample(0, 0, 0, 0, 0, 0, 0, false);
    private volatile Sample lastB = new Sample(0, 0, 0, 0, 0, 0, 0, false);
    private volatile double pressureRaw = 0, pressure = 0, pressureA = 0, pressureB = 0;
    private volatile long polls = 0, unreachable = 0;
    private final AtomicBoolean inflight = new AtomicBoolean(false);
    private final AtomicBoolean inflightB = new AtomicBoolean(false);

    public DownstreamTelemetry(Config cfg) {
        this.cfg = cfg;
        this.client = HttpClient.newBuilder().connectTimeout(Duration.ofMillis(200))
                .executor(Executors.newSingleThreadExecutor(r -> { Thread t = new Thread(r, "telemetry-http"); t.setDaemon(true); return t; }))
                .build();
        this.request = HttpRequest.newBuilder(URI.create(cfg.telemetryUrl + "/telemetry")).timeout(Duration.ofMillis(250)).GET().build();
        this.requestB = cfg.telemetryUrlB.isEmpty() ? null
                : HttpRequest.newBuilder(URI.create(cfg.telemetryUrlB + "/telemetry")).timeout(Duration.ofMillis(250)).GET().build();
        ScheduledExecutorService ses = Executors.newSingleThreadScheduledExecutor(r -> { Thread t = new Thread(r, "telemetry-poll"); t.setDaemon(true); return t; });
        ses.scheduleAtFixedRate(this::poll, 0, cfg.telemetryPeriodMs, TimeUnit.MILLISECONDS);
        if (requestB != null) ses.scheduleAtFixedRate(this::pollB, 0, cfg.telemetryPeriodMs, TimeUnit.MILLISECONDS);
    }

    private void poll() {
        if (!inflight.compareAndSet(false, true)) return;   // never queue polls behind a slow one
        client.sendAsync(request, HttpResponse.BodyHandlers.ofString()).whenComplete((resp, err) -> {
            try {
                Sample s;
                if (err != null || resp.statusCode() != 200) {
                    unreachable++;
                    s = new Sample(System.currentTimeMillis(), 1, 1, 1, 1e9, 1e9, 1, false);
                } else {
                    JsonNode n = JSON.readTree(resp.body());
                    s = new Sample(n.path("ts_ms").asLong(), n.path("thread_saturation").asDouble(0), n.path("pool_saturation").asDouble(0),
                            n.path("pool_pending_ratio").asDouble(0), n.path("acquire_wait_mean_ms").asDouble(0),
                            n.path("gc_pause_1s_ms").asDouble(0), n.path("heap_utilisation").asDouble(0), true);
                }
                last = s;
                pressureA = compute(s);
                // In the split topology the controller must act on the service that is actually
                // congested, so the two pressure indices are folded with a maximum: the bottleneck
                // resource, wherever it sits, decides whether more load will queue.
                pressureRaw = requestB == null ? pressureA : Math.max(pressureA, pressureB);
                pressure = 0.5 * pressure + 0.5 * pressureRaw;
                polls++;
            } catch (Exception e) {
                unreachable++;
            } finally {
                inflight.set(false);
            }
        });
    }

    private void pollB() {
        if (requestB == null || !inflightB.compareAndSet(false, true)) return;
        client.sendAsync(requestB, HttpResponse.BodyHandlers.ofString()).whenComplete((resp, err) -> {
            try {
                Sample s;
                if (err != null || resp.statusCode() != 200) {
                    unreachable++;
                    s = new Sample(System.currentTimeMillis(), 1, 1, 1, 1e9, 1e9, 1, false);
                } else {
                    JsonNode n = JSON.readTree(resp.body());
                    s = new Sample(n.path("ts_ms").asLong(), n.path("thread_saturation").asDouble(0), n.path("pool_saturation").asDouble(0),
                            n.path("pool_pending_ratio").asDouble(0), n.path("acquire_wait_mean_ms").asDouble(0),
                            n.path("gc_pause_1s_ms").asDouble(0), n.path("heap_utilisation").asDouble(0), true);
                }
                lastB = s;
                pressureB = compute(s);
            } catch (Exception e) {
                unreachable++;
            } finally {
                inflightB.set(false);
            }
        });
    }

    private double compute(Sample s) {
        if (!s.reachable) return 1.0;
        double thread = clamp(s.threadSat), pool = 0.9 * clamp(s.poolSat), pend = clamp(s.poolPending);
        double wait = clamp(s.acquireWaitMs / cfg.waitRefMs), gc = clamp(s.gcPause1sMs / cfg.gcRefMs);
        return Math.max(Math.max(thread, pool), Math.max(Math.max(pend, wait), gc));
    }

    private static double clamp(double v) { return Double.isNaN(v) ? 0 : Math.max(0, Math.min(1, v)); }

    public double pressure() { return pressure; }

    public Map<String, Object> state() {
        Sample s = last;
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("pressure", pressure);
        m.put("pressure_raw", pressureRaw);
        m.put("ds_reachable", s.reachable);
        m.put("ds_thread_sat", s.threadSat);
        m.put("ds_pool_sat", s.poolSat);
        m.put("ds_pool_pending", s.poolPending);
        m.put("ds_acquire_wait_ms", s.acquireWaitMs);
        m.put("ds_gc_pause_1s_ms", s.gcPause1sMs);
        m.put("ds_heap_util", s.heapUtil);
        if (requestB != null) {
            Sample b = lastB;
            m.put("pressure_a", pressureA);
            m.put("pressure_b", pressureB);
            m.put("dsb_reachable", b.reachable);
            m.put("dsb_thread_sat", b.threadSat);
            m.put("dsb_pool_sat", b.poolSat);
            m.put("dsb_pool_pending", b.poolPending);
            m.put("dsb_acquire_wait_ms", b.acquireWaitMs);
            m.put("dsb_gc_pause_1s_ms", b.gcPause1sMs);
        }
        m.put("telemetry_polls", polls);
        m.put("telemetry_unreachable", unreachable);
        return m;
    }
}
