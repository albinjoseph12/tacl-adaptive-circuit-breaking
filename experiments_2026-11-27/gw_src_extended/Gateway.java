package io.acb.gateway;

import com.fasterxml.jackson.databind.ObjectMapper;
import io.acb.gateway.policies.AdaptivePolicy;
import io.acb.gateway.policies.ConcurrencyLimitsPolicy;
import io.acb.gateway.policies.GradientPolicy;
import io.acb.gateway.policies.NonePolicy;
import io.acb.gateway.policies.Resilience4jPolicy;
import io.acb.gateway.policies.StaticPolicy;
import io.netty.bootstrap.ServerBootstrap;
import io.netty.buffer.Unpooled;
import io.netty.channel.*;
import io.netty.channel.nio.NioEventLoopGroup;
import io.netty.channel.socket.SocketChannel;
import io.netty.channel.socket.nio.NioServerSocketChannel;
import io.netty.handler.codec.http.*;
import io.netty.util.CharsetUtil;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;

/**
 * Reactive edge service: a non-blocking Netty HTTP/1.1 server (2 event-loop threads) that proxies /api/** to the
 * downstream catalog-service through the configured resilience policy using the JDK's asynchronous HttpClient.
 * No request thread ever blocks: downstream completion is delivered as a CompletableFuture and the response is
 * written back on the originating channel's event loop.
 *
 * Every response carries X-ACB-Outcome in {ok, not_found, downstream_error, timeout, shed, breaker_open, breaker_half_open}
 * so that the load generator can attribute each request. /control/state exposes controller + telemetry state
 * and is polled every 100 ms by the experiment logger.
 */
public final class Gateway {

    static final ObjectMapper JSON = new ObjectMapper();
    static Config cfg;
    static ResiliencePolicy policy;
    static DownstreamTelemetry telemetry;
    static HttpClient client;
    static final AtomicLong rrCounter = new AtomicLong();
    static final boolean lbRR = "rr".equalsIgnoreCase(System.getenv().getOrDefault("ACB_LB_MODE", ""));
    static final AtomicLong total = new AtomicLong(), ok = new AtomicLong(), timeouts = new AtomicLong(), errors = new AtomicLong(), rejected = new AtomicLong();

    public static void main(String[] args) throws Exception {
        cfg = new Config();
        telemetry = new DownstreamTelemetry(cfg);
        policy = switch (cfg.policy) {
            case "none" -> new NonePolicy();
            case "static" -> new StaticPolicy(cfg);
            case "gradient" -> new GradientPolicy();
            case "adaptive" -> new AdaptivePolicy(cfg, telemetry);
            case "resilience4j" -> new Resilience4jPolicy(cfg);
            case "concurrency-limits" -> new ConcurrencyLimitsPolicy();
            default -> throw new IllegalArgumentException("unknown ACB_POLICY " + cfg.policy);
        };
        client = HttpClient.newBuilder()
                .version(HttpClient.Version.HTTP_1_1)
                .connectTimeout(Duration.ofMillis(cfg.callTimeoutMs))
                .executor(Executors.newFixedThreadPool(4, r -> { Thread t = new Thread(r, "ds-client"); t.setDaemon(true); return t; }))
                .build();

        EventLoopGroup boss = new NioEventLoopGroup(1), workers = new NioEventLoopGroup(2);
        ServerBootstrap b = new ServerBootstrap();
        b.group(boss, workers).channel(NioServerSocketChannel.class)
                .option(ChannelOption.SO_BACKLOG, 4096)
                .childOption(ChannelOption.TCP_NODELAY, true)
                .childHandler(new ChannelInitializer<SocketChannel>() {
                    @Override protected void initChannel(SocketChannel ch) {
                        ch.pipeline().addLast(new HttpServerCodec(), new HttpObjectAggregator(65536), new Handler());
                    }
                });
        Channel ch = b.bind(cfg.port).sync().channel();
        System.out.println("[gateway] policy=" + policy.name() + " port=" + cfg.port + " downstream=" + cfg.downstream
                + (cfg.split ? " downstreamB=" + cfg.downstreamB : "") + " timeout=" + cfg.callTimeoutMs + "ms");
        ch.closeFuture().sync();
    }

    static final class Handler extends SimpleChannelInboundHandler<FullHttpRequest> {
        @Override
        protected void channelRead0(ChannelHandlerContext ctx, FullHttpRequest req) {
            String uri = req.uri();
            boolean keepAlive = HttpUtil.isKeepAlive(req);
            if (uri.startsWith("/control/state")) {
                write(ctx, HttpResponseStatus.OK, toJson(state()), "control", keepAlive);
                return;
            }
            if (!uri.startsWith("/api/")) {
                write(ctx, HttpResponseStatus.NOT_FOUND, "{\"error\":\"no route\"}", "no_route", keepAlive);
                return;
            }
            String method = req.method().name();
            String body = req.content().toString(CharsetUtil.UTF_8);
            String dsPath = route(method, uri);
            if (dsPath == null) {
                write(ctx, HttpResponseStatus.NOT_FOUND, "{\"error\":\"no route\"}", "no_route", keepAlive);
                return;
            }
            total.incrementAndGet();
            String routeKey = method + " " + routeClass(dsPath);
            final String base = baseFor(method);
            Supplier<CompletableFuture<String>> call = () -> downstream(base, method, dsPath, body);
            policy.execute(routeKey, call).whenComplete((resp, err) -> {
                HttpResponseStatus st; String outcome; String out;
                if (err == null) { ok.incrementAndGet(); st = HttpResponseStatus.OK; outcome = "ok"; out = resp; }
                else {
                    Throwable e = err instanceof java.util.concurrent.CompletionException ce && ce.getCause() != null ? ce.getCause() : err;
                    if (e instanceof ResiliencePolicy.RejectedException re) { rejected.incrementAndGet(); st = HttpResponseStatus.SERVICE_UNAVAILABLE; outcome = re.reason; out = "{\"error\":\"" + re.reason + "\"}"; }
                    else if (e instanceof ResiliencePolicy.DownstreamException de) {
                        if (de.timeout) { timeouts.incrementAndGet(); st = HttpResponseStatus.GATEWAY_TIMEOUT; outcome = "timeout"; }
                        else if (de.status == 404) { st = HttpResponseStatus.NOT_FOUND; outcome = "not_found"; }
                        else { errors.incrementAndGet(); st = HttpResponseStatus.BAD_GATEWAY; outcome = "downstream_error"; }
                        out = "{\"error\":\"" + outcome + "\",\"status\":" + de.status + "}";
                    } else { errors.incrementAndGet(); st = HttpResponseStatus.BAD_GATEWAY; outcome = "downstream_error"; out = "{\"error\":\"" + e.getClass().getSimpleName() + "\"}"; }
                }
                final HttpResponseStatus fst = st; final String fo = outcome, fout = out;
                if (ctx.channel().eventLoop().inEventLoop()) write(ctx, fst, fout, fo, keepAlive);
                else ctx.channel().eventLoop().execute(() -> write(ctx, fst, fout, fo, keepAlive));
            });
        }

        @Override public void exceptionCaught(ChannelHandlerContext ctx, Throwable cause) { ctx.close(); }
    }

    /** Collapses a downstream path to its route class (path parameters removed). */
    static String routeClass(String dsPath) {
        if (dsPath.startsWith("/orders/customer/")) return "/orders/customer/{id}";
        if (dsPath.equals("/orders")) return "/orders";
        if (dsPath.endsWith("/status")) return "/orders/{id}/status";
        return "/orders/{id}";
    }

    /** Maps gateway API routes onto downstream paths. */
    static String route(String method, String uri) {
        String path = uri.contains("?") ? uri.substring(0, uri.indexOf('?')) : uri;
        if (method.equals("GET") && path.startsWith("/api/orders/")) return "/orders/" + path.substring("/api/orders/".length());
        if (method.equals("GET") && path.startsWith("/api/customers/") && path.endsWith("/orders"))
            return "/orders/customer/" + path.substring("/api/customers/".length(), path.length() - "/orders".length());
        if (method.equals("POST") && path.equals("/api/orders")) return "/orders";
        if (method.equals("PUT") && path.startsWith("/api/orders/") && path.endsWith("/status")) return "/orders/" + path.substring("/api/orders/".length());
        return null;
    }

    /** Asynchronous downstream call; completes exceptionally with DownstreamException on timeout / error status. */
    /** In the split topology reads go to service A and writes to service B; otherwise everything to A. */
    static String baseFor(String method) {
        if (!cfg.split) return cfg.downstream;
        if (lbRR) return (rrCounter.getAndIncrement() & 1L) == 0L ? cfg.downstream : cfg.downstreamB;
        return method.equals("GET") ? cfg.downstream : cfg.downstreamB;
    }

    static CompletableFuture<String> downstream(String base, String method, String path, String body) {
        HttpRequest.Builder rb = HttpRequest.newBuilder(URI.create(base + path)).timeout(Duration.ofMillis(cfg.callTimeoutMs));
        HttpRequest.BodyPublisher bp = body == null || body.isEmpty() ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8);
        switch (method) {
            case "POST" -> rb.header("Content-Type", "application/json").POST(bp);
            case "PUT" -> rb.header("Content-Type", "application/json").PUT(bp);
            default -> rb.GET();
        }
        CompletableFuture<String> f = new CompletableFuture<>();
        client.sendAsync(rb.build(), HttpResponse.BodyHandlers.ofString()).whenComplete((r, e) -> {
            if (e != null) {
                Throwable c = e instanceof java.util.concurrent.CompletionException ce && ce.getCause() != null ? ce.getCause() : e;
                f.completeExceptionally(new ResiliencePolicy.DownstreamException(0, c instanceof java.net.http.HttpTimeoutException));
            } else if (r.statusCode() >= 200 && r.statusCode() < 300) f.complete(r.body());
            else f.completeExceptionally(new ResiliencePolicy.DownstreamException(r.statusCode(), false));
        });
        return f;
    }

    static void write(ChannelHandlerContext ctx, HttpResponseStatus status, String body, String outcome, boolean keepAlive) {
        FullHttpResponse resp = new DefaultFullHttpResponse(HttpVersion.HTTP_1_1, status, Unpooled.copiedBuffer(body, CharsetUtil.UTF_8));
        resp.headers().set(HttpHeaderNames.CONTENT_TYPE, "application/json");
        resp.headers().set("X-ACB-Outcome", outcome);
        resp.headers().setInt(HttpHeaderNames.CONTENT_LENGTH, resp.content().readableBytes());
        if (keepAlive) { resp.headers().set(HttpHeaderNames.CONNECTION, HttpHeaderValues.KEEP_ALIVE); ctx.writeAndFlush(resp); }
        else ctx.writeAndFlush(resp).addListener(ChannelFutureListener.CLOSE);
    }

    static Map<String, Object> state() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ts_ms", System.currentTimeMillis());
        m.putAll(policy.state());
        m.putAll(telemetry.state());
        m.put("gw_total", total.get()); m.put("gw_ok", ok.get()); m.put("gw_timeouts", timeouts.get());
        m.put("gw_errors", errors.get()); m.put("gw_rejected", rejected.get());
        return m;
    }

    static String toJson(Object o) { try { return JSON.writeValueAsString(o); } catch (Exception e) { return "{}"; } }
}
