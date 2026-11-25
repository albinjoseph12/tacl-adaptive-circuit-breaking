package io.acb.catalog;

import com.zaxxer.hikari.HikariPoolMXBean;

import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryMXBean;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Samples JVM runtime signals every 100 ms and exposes a trailing-window snapshot:
 *   gc_pause_1s_ms        total stop-the-world collection time in the trailing 1 s (from GarbageCollectorMXBean deltas)
 *   thread_saturation     Tomcat busy worker threads / maxThreads
 *   pool_saturation       Hikari active connections / maximum pool size
 *   pool_pending_ratio    threads waiting for a connection / maximum pool size
 *   acquire_wait_mean_ms  EWMA of measured getConnection() latency (the real queueing delay in front of the pool)
 *   heap_utilisation      used / max heap
 */
public final class JvmTelemetry {

    private static final MemoryMXBean MEM = ManagementFactory.getMemoryMXBean();
    private static final long[] ring = new long[10];
    private static int idx = 0;
    private static long lastGcMs = 0;
    private static volatile long gcPause1s = 0, gcCountTotal = 0, gcTimeTotal = 0;
    private static long lastAcqCount = 0, lastAcqNanos = 0;
    private static volatile double acqWaitEwma = 0;
    private static volatile long seq = 0;

    static void start() {
        ScheduledExecutorService ses = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "jvm-telemetry"); t.setDaemon(true); return t;
        });
        ses.scheduleAtFixedRate(JvmTelemetry::sample, 0, 100, TimeUnit.MILLISECONDS);
    }

    private static void sample() {
        long t = 0, c = 0;
        for (GarbageCollectorMXBean gc : ManagementFactory.getGarbageCollectorMXBeans()) {
            t += Math.max(0, gc.getCollectionTime());
            c += Math.max(0, gc.getCollectionCount());
        }
        long d = t - lastGcMs;
        lastGcMs = t; gcCountTotal = c; gcTimeTotal = t;
        ring[idx] = d; idx = (idx + 1) % ring.length;
        long s = 0; for (long v : ring) s += v;
        gcPause1s = s;

        long ac = CatalogService.acqCount.get(), an = CatalogService.acqNanos.get();
        long dc = ac - lastAcqCount, dn = an - lastAcqNanos;
        lastAcqCount = ac; lastAcqNanos = an;
        double inst = dc > 0 ? (dn / 1e6) / dc : (acqWaitEwma * 0.5);   // decay when idle
        acqWaitEwma = 0.7 * acqWaitEwma + 0.3 * inst;
        seq++;
    }

    static Map<String, Object> snapshot() {
        Map<String, Object> m = new LinkedHashMap<>();
        HikariPoolMXBean p = CatalogService.pool();
        int busy = CatalogService.tomcatExecutor == null ? 0 : CatalogService.tomcatExecutor.getActiveCount();
        int maxT = CatalogService.tomcatMax;
        int active = p.getActiveConnections(), pending = p.getThreadsAwaitingConnection(), total = p.getTotalConnections();
        int maxPool = CatalogService.ds.getMaximumPoolSize();
        long used = MEM.getHeapMemoryUsage().getUsed(), max = MEM.getHeapMemoryUsage().getMax();
        m.put("ts_ms", System.currentTimeMillis());
        m.put("seq", seq);
        m.put("gc_pause_1s_ms", gcPause1s);
        m.put("gc_pause_fraction", gcPause1s / 1000.0);
        m.put("gc_count_total", gcCountTotal);
        m.put("gc_time_total_ms", gcTimeTotal);
        m.put("tomcat_busy", busy);
        m.put("tomcat_max", maxT);
        m.put("thread_saturation", maxT == 0 ? 0.0 : (double) busy / maxT);
        m.put("hikari_active", active);
        m.put("hikari_pending", pending);
        m.put("hikari_total", total);
        m.put("hikari_max", maxPool);
        m.put("pool_saturation", maxPool == 0 ? 0.0 : (double) active / maxPool);
        m.put("pool_pending_ratio", maxPool == 0 ? 0.0 : (double) pending / maxPool);
        m.put("acquire_wait_mean_ms", acqWaitEwma);
        m.put("acquire_timeouts_total", CatalogService.acqTimeouts.get());
        m.put("requests_total", CatalogService.reqCount.get());
        m.put("errors_total", CatalogService.reqErrors.get());
        m.put("heap_used_mb", used / 1048576.0);
        m.put("heap_max_mb", max / 1048576.0);
        m.put("heap_utilisation", max > 0 ? (double) used / max : 0.0);
        m.put("gc_storm", GcStorm.active());
        return m;
    }
}
