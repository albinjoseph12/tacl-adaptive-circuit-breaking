package io.acb.gateway;

import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.function.Supplier;

/** A resilience policy wraps every asynchronous downstream call made by the gateway. */
public interface ResiliencePolicy {

    /** Raised when the policy rejects a call without sending it downstream (open breaker or shed). */
    final class RejectedException extends RuntimeException {
        public final String reason;
        public RejectedException(String reason) { super(reason, null, false, false); this.reason = reason; }
    }

    /** Raised by the transport when the downstream answered with an error status or the call timed out. */
    final class DownstreamException extends RuntimeException {
        public final int status; public final boolean timeout;
        public DownstreamException(int status, boolean timeout) { super("downstream " + status, null, false, false); this.status = status; this.timeout = timeout; }
    }

    /** @param route logical request class (e.g. "GET /orders/{id}") used by route-aware policies */
    <T> CompletableFuture<T> execute(String route, Supplier<CompletableFuture<T>> call);

    Map<String, Object> state();

    String name();
}
