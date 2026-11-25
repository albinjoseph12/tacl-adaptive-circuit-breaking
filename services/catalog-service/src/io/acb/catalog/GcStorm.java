package io.acb.catalog;

import java.util.Map;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * GC-pressure fault: allocator threads churn a rotating window of retained 1 MB buffers plus short-lived
 * garbage, forcing frequent young collections and promotion/mixed collections on the small service heap.
 * The request path is untouched; the only effect on OLTP traffic is stop-the-world pause time.
 */
final class GcStorm {
    private static final AtomicBoolean on = new AtomicBoolean(false);

    static synchronized Map<String, Object> set(boolean enable, int threads, int retainMb) {
        if (enable && on.compareAndSet(false, true)) {
            for (int i = 0; i < threads; i++) {
                Thread t = new Thread(() -> run(retainMb), "gc-storm-" + i);
                t.setDaemon(true);
                t.start();
            }
        } else if (!enable) {
            on.set(false);
        }
        return Map.of("gc_storm", on.get(), "threads", threads, "retain_mb", retainMb);
    }

    static boolean active() { return on.get(); }

    private static void run(int retainMb) {
        byte[][] retained = new byte[Math.max(8, retainMb)][];
        long i = 0;
        while (on.get()) {
            retained[(int) (i % retained.length)] = new byte[1 << 20];
            byte[] junk = new byte[ThreadLocalRandom.current().nextInt(32 << 10, 256 << 10)];
            junk[0] = (byte) i;
            i++;
            if ((i & 15) == 0) { try { Thread.sleep(0, 200_000); } catch (InterruptedException e) { return; } }
        }
    }
}
