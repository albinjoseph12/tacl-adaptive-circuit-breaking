package io.acb.gateway;

/** All tunables, overridable from the environment so that run_experiment.sh can sweep them. */
public final class Config {
    static String env(String k, String d) { String v = System.getenv(k); return v == null || v.isEmpty() ? d : v; }
    static double d(String k, double v) { return Double.parseDouble(env(k, String.valueOf(v))); }
    static int i(String k, int v) { return Integer.parseInt(env(k, String.valueOf(v))); }
    static long l(String k, long v) { return Long.parseLong(env(k, String.valueOf(v))); }

    public final String policy = env("ACB_POLICY", "adaptive").toLowerCase();
    public final int port = i("GATEWAY_PORT", 8080);
    public final String downstream = env("DOWNSTREAM_URL", "http://localhost:8081");
    public final String telemetryUrl = env("TELEMETRY_URL", "http://localhost:8091");
    // Second topology (Section VI-G): when DOWNSTREAM_URL_B is set the gateway fans out, sending read
    // routes to A and write routes to B. Both services share one database, so the bottleneck is shared
    // while the JVM-level signals are per-service. Empty means the single-service chain topology.
    public final String downstreamB = env("DOWNSTREAM_URL_B", "");
    public final String telemetryUrlB = env("TELEMETRY_URL_B", "");
    public final boolean split = !downstreamB.isEmpty();
    public final long callTimeoutMs = l("CALL_TIMEOUT_MS", 3000);
    public final long telemetryPeriodMs = l("TELEMETRY_PERIOD_MS", 100);

    // static breaker (Resilience4j-style count-based sliding window)
    public final int windowSize = i("CB_WINDOW", 100);
    public final int minimumCalls = i("CB_MIN_CALLS", 20);
    public final double failureRateThreshold = d("CB_FAILURE_RATE", 50);
    public final double slowCallRateThreshold = d("CB_SLOW_RATE", 50);
    public final long slowCallMs = l("CB_SLOW_MS", 1000);
    public final long waitInOpenMs = l("CB_WAIT_OPEN_MS", 10000);
    public final int permittedHalfOpen = i("CB_HALF_OPEN", 10);

    // adaptive controller
    public final double pLow = d("ACB_P_LOW", 0.55);
    public final double pHigh = d("ACB_P_HIGH", 0.80);
    public final int limitMin = i("ACB_LIMIT_MIN", 8);
    public final int limitMax = i("ACB_LIMIT_MAX", 512);
    public final int limitInit = i("ACB_LIMIT_INIT", 128);
    public final double additiveIncrease = d("ACB_AI", 4);
    public final double beta = d("ACB_BETA", 0.5);
    public final double thetaBase = d("ACB_THETA_BASE", 30);
    public final double thetaSpan = d("ACB_THETA_SPAN", 40);
    public final double gcRefMs = d("ACB_GC_REF_MS", 250);
    public final double waitRefMs = d("ACB_WAIT_REF_MS", 100);
    public final long minOpenMs = l("ACB_MIN_OPEN_MS", 1000);
    public final long maxOpenMs = l("ACB_MAX_OPEN_MS", 10000);
}
