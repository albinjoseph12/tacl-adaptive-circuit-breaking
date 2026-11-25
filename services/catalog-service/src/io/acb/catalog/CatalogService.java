package io.acb.catalog;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import com.zaxxer.hikari.HikariPoolMXBean;
import jakarta.servlet.http.HttpServlet;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import org.apache.catalina.Context;
import org.apache.catalina.connector.Connector;
import org.apache.catalina.startup.Tomcat;
import org.apache.tomcat.util.threads.ThreadPoolExecutor;

import java.io.IOException;
import java.sql.*;
import java.util.*;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Downstream order service: embedded Apache Tomcat 10.1 (bounded worker pool) + HikariCP + PostgreSQL JDBC.
 * This is the same servlet/pool substrate that a Spring Boot MVC service runs on; it is used directly so that
 * the thread-pool and connection-pool telemetry can be read from the real Tomcat executor and Hikari pool.
 *
 * Endpoints:
 *   GET  /orders/{id}                 point read on the 2M-row orders table
 *   GET  /orders/customer/{id}        index range read (top-20 recent orders for a customer)
 *   POST /orders                      insert
 *   PUT  /orders/{id}/status          update (writes to a "hot" id range are what lock-contention faults block)
 *   GET  /telemetry                   JVM runtime telemetry (GC pause, thread saturation, pool wait) - no DB access
 *   POST /fault/gc?on=true|false      allocation-storm fault injector
 */
public class CatalogService {

    static final ObjectMapper JSON = new ObjectMapper();
    static HikariDataSource ds;
    static ThreadPoolExecutor tomcatExecutor;
    static int tomcatMax;
    // connection acquisition wait accounting (measured around getConnection())
    static final AtomicLong acqCount = new AtomicLong(), acqNanos = new AtomicLong(), acqTimeouts = new AtomicLong();
    static final AtomicLong reqCount = new AtomicLong(), reqErrors = new AtomicLong();

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(env("CATALOG_PORT", "8081"));
        int maxThreads = Integer.parseInt(env("TOMCAT_MAX_THREADS", "48"));
        int poolSize = Integer.parseInt(env("HIKARI_MAX_POOL", "16"));
        long connTimeout = Long.parseLong(env("HIKARI_CONN_TIMEOUT_MS", "2000"));

        HikariConfig hc = new HikariConfig();
        hc.setJdbcUrl(env("DB_URL", "jdbc:postgresql://localhost:5432/acb"));
        hc.setUsername(env("DB_USER", "acb"));
        hc.setPassword(env("DB_PASSWORD", "acb"));
        hc.setMaximumPoolSize(poolSize);
        hc.setMinimumIdle(poolSize);
        hc.setConnectionTimeout(connTimeout);
        hc.setPoolName("catalog-pool");
        hc.setRegisterMbeans(true);
        hc.addDataSourceProperty("prepareThreshold", "1");
        // The generic (parameter-agnostic) plan for the bounded customer-history queries degenerates into a bitmap scan +
        // sort over the whole customer history for heavy-tailed customers (~100 ms); custom plans keep the index order scan.
        // lock_timeout bounds how long a blocked write can hold a pooled connection (a standard production safeguard).
        hc.setConnectionInitSql("SET plan_cache_mode = force_custom_plan; SET lock_timeout = '1000ms'");
        ds = new HikariDataSource(hc);

        Tomcat tomcat = new Tomcat();
        tomcat.setBaseDir(System.getProperty("java.io.tmpdir") + "/catalog-tomcat");
        Connector connector = new Connector("org.apache.coyote.http11.Http11NioProtocol");
        connector.setPort(port);
        connector.setProperty("maxThreads", String.valueOf(maxThreads));
        connector.setProperty("minSpareThreads", "8");
        connector.setProperty("acceptCount", "500");
        connector.setProperty("maxConnections", "4000");
        connector.setProperty("maxKeepAliveRequests", "-1");
        connector.setProperty("connectionTimeout", "20000");
        tomcat.setConnector(connector);
        tomcatMax = maxThreads;
        // Management connector on its own (small) worker pool - equivalent to Spring Boot's management.server.port -
        // so that /telemetry stays answerable even when the request-serving pool is fully saturated.
        int mgmtPort = Integer.parseInt(env("CATALOG_MGMT_PORT", "8091"));
        Connector mgmt = new Connector("org.apache.coyote.http11.Http11NioProtocol");
        mgmt.setPort(mgmtPort);
        mgmt.setProperty("maxThreads", "4");
        mgmt.setProperty("minSpareThreads", "2");
        tomcat.getService().addConnector(mgmt);

        Context ctx = tomcat.addContext("", null);
        Tomcat.addServlet(ctx, "orders", new OrderServlet());
        ctx.addServletMappingDecoded("/orders/*", "orders");
        ctx.addServletMappingDecoded("/orders", "orders");
        Tomcat.addServlet(ctx, "telemetry", new TelemetryServlet());
        ctx.addServletMappingDecoded("/telemetry", "telemetry");
        Tomcat.addServlet(ctx, "fault", new FaultServlet());
        ctx.addServletMappingDecoded("/fault/*", "fault");
        tomcat.start();
        tomcatExecutor = (ThreadPoolExecutor) connector.getProtocolHandler().getExecutor();
        JvmTelemetry.start();
        System.out.println("[catalog-service] listening on " + port + " (management " + mgmtPort + ") tomcat.maxThreads=" + maxThreads
                + " hikari.maxPool=" + poolSize + " connTimeout=" + connTimeout + "ms");
        tomcat.getServer().await();
    }

    static String env(String k, String d) { String v = System.getenv(k); return v == null || v.isEmpty() ? d : v; }

    /** Timed connection acquisition so the true queueing delay in front of the pool is observable. */
    static Connection acquire() throws SQLException {
        long t0 = System.nanoTime();
        try {
            Connection c = ds.getConnection();
            acqNanos.addAndGet(System.nanoTime() - t0);
            acqCount.incrementAndGet();
            return c;
        } catch (SQLException e) {
            acqNanos.addAndGet(System.nanoTime() - t0);
            acqCount.incrementAndGet();
            acqTimeouts.incrementAndGet();
            throw e;
        }
    }

    static void json(HttpServletResponse resp, int status, Object body) throws IOException {
        resp.setStatus(status);
        resp.setContentType("application/json");
        JSON.writeValue(resp.getOutputStream(), body);
    }

    // ------------------------------------------------------------------ OLTP servlet
    static class OrderServlet extends HttpServlet {
        @Override
        protected void service(HttpServletRequest req, HttpServletResponse resp) throws IOException {
            reqCount.incrementAndGet();
            String path = req.getPathInfo() == null ? "" : req.getPathInfo();
            String m = req.getMethod();
            try {
                if ("GET".equals(m) && path.startsWith("/customer/")) {
                    long cid = Long.parseLong(path.substring("/customer/".length()));
                    List<Map<String, Object>> rows = new ArrayList<>();
                    Map<String, Object> summary = new LinkedHashMap<>();
                    try (Connection c = acquire()) {
                        try (PreparedStatement ps = c.prepareStatement(
                                "SELECT id, product_id, quantity, unit_price_cents, status, created_at FROM orders " +
                                        "WHERE customer_id = ? ORDER BY created_at DESC LIMIT 20")) {
                            ps.setLong(1, cid);
                            try (ResultSet rs = ps.executeQuery()) {
                                while (rs.next()) rows.add(row(rs, "id", "product_id", "quantity", "unit_price_cents", "status", "created_at"));
                            }
                        }
                        // customer summary over the most recent 2,000 orders (bounded index range scan + aggregate)
                        try (PreparedStatement ps = c.prepareStatement(
                                "SELECT count(*), coalesce(sum(quantity * unit_price_cents),0), " +
                                        "count(*) FILTER (WHERE status IN ('CANCELLED','RETURNED')) FROM " +
                                        "(SELECT quantity, unit_price_cents, status FROM orders WHERE customer_id = ? ORDER BY created_at DESC LIMIT 2000) r")) {
                            ps.setLong(1, cid);
                            try (ResultSet rs = ps.executeQuery()) {
                                rs.next();
                                summary.put("recent_orders", rs.getLong(1)); summary.put("recent_spend_cents", rs.getLong(2)); summary.put("recent_returns", rs.getLong(3));
                            }
                        }
                    }
                    json(resp, 200, Map.of("customer_id", cid, "summary", summary, "orders", rows));
                } else if ("GET".equals(m) && path.length() > 1) {
                    long id = Long.parseLong(path.substring(1));
                    try (Connection c = acquire()) {
                        Map<String, Object> order = null;
                        try (PreparedStatement ps = c.prepareStatement(
                                "SELECT id, customer_id, product_id, quantity, unit_price_cents, status, created_at, updated_at FROM orders WHERE id = ?")) {
                            ps.setLong(1, id);
                            try (ResultSet rs = ps.executeQuery()) { if (rs.next()) order = row(rs, "id", "customer_id", "product_id", "quantity", "unit_price_cents", "status", "created_at", "updated_at"); }
                        }
                        if (order == null) { json(resp, 404, Map.of("error", "not found")); return; }
                        // order detail carries the customer's recent order count and spend (index range scan, bounded to 1,000 rows)
                        try (PreparedStatement ps = c.prepareStatement(
                                "SELECT count(*), coalesce(sum(quantity * unit_price_cents),0) FROM " +
                                        "(SELECT quantity, unit_price_cents FROM orders WHERE customer_id = ? ORDER BY created_at DESC LIMIT 1000) r")) {
                            ps.setLong(1, ((Number) order.get("customer_id")).longValue());
                            try (ResultSet rs = ps.executeQuery()) { rs.next(); order.put("customer_recent_orders", rs.getLong(1)); order.put("customer_recent_spend_cents", rs.getLong(2)); }
                        }
                        json(resp, 200, order);
                    }
                } else if ("POST".equals(m)) {
                    Map<?, ?> body = JSON.readValue(req.getInputStream(), Map.class);
                    long customerId = num(body.get("customer_id"), ThreadLocalRandom.current().nextLong(1, 200_000));
                    long productId = num(body.get("product_id"), ThreadLocalRandom.current().nextLong(1, 50_000));
                    long qty = num(body.get("quantity"), 1);
                    long price = num(body.get("unit_price_cents"), 999);
                    try (Connection c = acquire()) {
                        long demand;
                        // demand check on the product before accepting the order (bounded index scan on product_id)
                        try (PreparedStatement ps = c.prepareStatement(
                                "SELECT count(*) FROM (SELECT 1 FROM orders WHERE product_id = ? AND status <> 'CANCELLED' LIMIT 1000) r")) {
                            ps.setLong(1, productId);
                            try (ResultSet rs = ps.executeQuery()) { rs.next(); demand = rs.getLong(1); }
                        }
                        try (PreparedStatement ps = c.prepareStatement(
                                "INSERT INTO orders(customer_id, product_id, quantity, unit_price_cents, status, created_at, updated_at) " +
                                        "VALUES (?,?,?,?,'NEW',now(),now()) RETURNING id")) {
                            ps.setLong(1, customerId); ps.setLong(2, productId); ps.setInt(3, (int) qty); ps.setLong(4, price);
                            try (ResultSet rs = ps.executeQuery()) {
                                rs.next();
                                json(resp, 201, Map.of("id", rs.getLong(1), "status", "NEW", "product_recent_demand", demand));
                            }
                        }
                    }
                } else if ("PUT".equals(m) && path.endsWith("/status")) {
                    long id = Long.parseLong(path.substring(1, path.length() - "/status".length()));
                    Map<?, ?> body = JSON.readValue(req.getInputStream(), Map.class);
                    Object st = body.get("status");
                    String status = st == null ? "SHIPPED" : String.valueOf(st);
                    try (Connection c = acquire();
                         PreparedStatement ps = c.prepareStatement("UPDATE orders SET status = ?, updated_at = now() WHERE id = ?")) {
                        ps.setString(1, status); ps.setLong(2, id);
                        int n = ps.executeUpdate();
                        if (n == 0) json(resp, 404, Map.of("error", "not found"));
                        else json(resp, 200, Map.of("id", id, "status", status));
                    }
                } else {
                    json(resp, 400, Map.of("error", "bad request"));
                }
            } catch (SQLException e) {
                reqErrors.incrementAndGet();
                // pool acquisition timeout or query failure -> 503 with reason; this is the downstream failure
                // signal that a client-side breaker observes
                json(resp, 503, Map.of("error", "db_unavailable", "detail", String.valueOf(e.getMessage()).split("\n")[0]));
            } catch (Exception e) {
                reqErrors.incrementAndGet();
                json(resp, 500, Map.of("error", e.getClass().getSimpleName()));
            }
        }

        private static long num(Object o, long d) { return o instanceof Number n ? n.longValue() : d; }

        private static Map<String, Object> row(ResultSet rs, String... cols) throws SQLException {
            Map<String, Object> m = new LinkedHashMap<>();
            for (String c : cols) {
                Object v = rs.getObject(c);
                m.put(c, v instanceof Timestamp ts ? ts.toInstant().toString() : v);
            }
            return m;
        }
    }

    // ------------------------------------------------------------------ telemetry servlet
    static class TelemetryServlet extends HttpServlet {
        @Override
        protected void doGet(HttpServletRequest req, HttpServletResponse resp) throws IOException {
            json(resp, 200, JvmTelemetry.snapshot());
        }
    }

    // ------------------------------------------------------------------ fault servlet
    static class FaultServlet extends HttpServlet {
        @Override
        protected void service(HttpServletRequest req, HttpServletResponse resp) throws IOException {
            String path = req.getPathInfo() == null ? "" : req.getPathInfo();
            if (path.startsWith("/gc")) {
                boolean on = "true".equals(req.getParameter("on"));
                int threads = req.getParameter("threads") == null ? 2 : Integer.parseInt(req.getParameter("threads"));
                int retain = req.getParameter("retainMb") == null ? 96 : Integer.parseInt(req.getParameter("retainMb"));
                json(resp, 200, GcStorm.set(on, threads, retain));
            } else {
                json(resp, 200, Map.of("gc_storm", GcStorm.active()));
            }
        }
    }

    static HikariPoolMXBean pool() { return ds.getHikariPoolMXBean(); }
}
