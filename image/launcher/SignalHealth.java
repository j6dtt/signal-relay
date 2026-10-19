package relay;

import ch.qos.logback.classic.Level;
import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.LoggerContext;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.classic.spi.IThrowableProxy;
import ch.qos.logback.core.Appender;
import ch.qos.logback.core.AppenderBase;
import ch.qos.logback.core.filter.Filter;
import ch.qos.logback.core.spi.FilterReply;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.lang.management.GarbageCollectorMXBean;
import java.lang.management.ManagementFactory;
import java.lang.management.MemoryPoolMXBean;
import java.lang.management.MemoryType;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;

/**
 * signal-cli's own health, for /api/v1/check (image r2 and later): its link to Signal's servers, its receive loop,
 * Java memory, the gateway's counters, and every warning and error counted by a fixed code.
 *
 * It listens to signal-cli's log events inside this JVM through logback's public API, not by parsing terminal text.
 * Signal's network library logs connects, keep-alives (every 30 s) and failures, but signal-cli only connects that
 * library to its logging with --verbose (Main.configureLogging -> ManagerLogger.initLogger). So this connects it
 * (SignalLogger.initLogger, public), lowers only the loggers it needs, for this listener, and keeps their events off
 * the console: without --verbose the Docker log is unchanged (our own "Signal servers: ..." lines report the
 * changes instead). Only codes, times and counts are kept: never a log line's text, which can hold identifiers.
 *
 * It must not touch logging before signal-cli's Main has configured it ("before any logger is initialized"), so a
 * thread waits for slf4j to finish its own start-up, then hooks in. If the hook can't be set up (a future signal-cli
 * logs differently), "sig" is left out and signal-monitor shows nothing rather than a false alarm.
 */
final class SignalHealth {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final long BOOT = System.currentTimeMillis();
    private static final String SIGNAL = "LibSignalService";                 // signal-cli's bridge for Signal's library
    private static final String RECEIVE = "org.asamk.signal.manager.helper.ReceiveHelper";
    private static final String HEALTH = "org.asamk.signal.manager.internal.SignalWebSocketHealthMonitor";
    private static final Set<String> LOWERED = Set.of(SIGNAL, RECEIVE, HEALTH);

    // link to Signal's servers (the authenticated "normal" connection, which receives the messages)
    private static volatile String state = "STARTING";
    private static volatile long since = BOOT, keepAlive = 0, keepAliveSent = 0, rtt = -1, failAt = 0, skewAt = 0;
    private static volatile String fail = null, lastWhy = "";
    private static final AtomicLong drops = new AtomicLong(), alerts = new AtomicLong();
    // receive loop
    private static volatile long loopAt = 0, caughtUp = 0;
    private static final AtomicLong envelopes = new AtomicLong();
    // problems by code; gateway counters (set by TlsGateway)
    private static final Map<String, AtomicLong> problems = new ConcurrentHashMap<>();
    static final AtomicLong gwRefused = new AtomicLong(), gwBlocked = new AtomicLong(), gwFull = new AtomicLong();
    static volatile java.util.function.IntSupplier gwConnections = () -> 0;
    private static volatile boolean hooked = false, everConnected = false;
    private static long gcLast = -1, gcLastAt = 0, gcPerMin = 0;

    private SignalHealth() {}

    static void start() {
        Thread t = new Thread(SignalHealth::hookWhenReady, "signal-health");
        t.setDaemon(true);
        t.start();
    }

    private static void hookWhenReady() {
        try {
            java.lang.reflect.Field f = org.slf4j.LoggerFactory.class.getDeclaredField("INITIALIZATION_STATE");
            f.setAccessible(true);
            for (int i = 0; i < 600 && f.getInt(null) != 3; i++) Thread.sleep(100);    // 3 = SUCCESSFUL_INITIALIZATION
            if (f.getInt(null) != 3) throw new IllegalStateException("signal-cli's logging did not start within 60 s");
            LoggerContext lc = (LoggerContext) org.slf4j.LoggerFactory.getILoggerFactory();
            Logger root = lc.getLogger(Logger.ROOT_LOGGER_NAME);
            boolean verbose = verboseLevel() > 0;                       // then signal-cli shows all of this itself
            Appender<ILoggingEvent> console = root.getAppender("console");
            if (console != null && !verbose) {
                console.addFilter(new Filter<>() {                        // the console stays as before
                    @Override
                    public FilterReply decide(ILoggingEvent e) {
                        String l = e.getLoggerName();
                        return SIGNAL.equals(l) || (LOWERED.contains(l) && !e.getLevel().isGreaterOrEqual(Level.WARN))
                                ? FilterReply.DENY : FilterReply.NEUTRAL;   // Signal's library was never on the console
                    }
                });
            }
            lc.getLogger(SIGNAL).setLevel(Level.DEBUG);
            lc.getLogger(HEALTH).setLevel(Level.DEBUG);
            lc.getLogger(RECEIVE).setLevel(Level.TRACE);
            AppenderBase<ILoggingEvent> listener = new AppenderBase<>() {
                @Override
                protected void append(ILoggingEvent e) {
                    try { seen(e); } catch (RuntimeException ignored) { }      // never disturb signal-cli's logging
                }
            };
            listener.setContext(lc);
            listener.setName("relay-health");
            listener.start();
            root.addAppender(listener);
            if (!verbose) org.asamk.signal.manager.internal.SignalLogger.initLogger();   // Signal's library -> slf4j
            hooked = true;
            TlsGateway.log("Watching signal-cli's link to Signal's servers and its own warnings and errors "
                    + "(for the health check).");
        } catch (Exception e) {
            TlsGateway.log("Could not watch signal-cli's own health (" + e + "): the health check reports less.");
        }
    }

    /** signal-cli's --verbose level (0 when not given), read from its LogConfigurator; 0 if it can't be read. */
    private static int verboseLevel() {
        try {
            java.lang.reflect.Field f = Class.forName("org.asamk.signal.logging.LogConfigurator").getDeclaredField("verboseLevel");
            f.setAccessible(true);
            return f.getInt(null);
        } catch (ReflectiveOperationException | RuntimeException e) {
            return 0;
        }
    }

    private static void problem(String code) {
        problems.computeIfAbsent(code, k -> new AtomicLong()).incrementAndGet();
    }

    private static void setState(String s) {
        if (!s.equals(state)) {
            state = s;
            since = System.currentTimeMillis();
            TlsGateway.log("Signal servers: " + switch (s) {
                case "CONNECTED" -> everConnected ? "connected again." : "connected.";
                case "CONNECTING" -> "connecting...";
                case "RECONNECTING" -> "lost the connection, reconnecting...";
                case "DISCONNECTED" -> "disconnected.";
                case "AUTH_FAILED" -> "Signal refuses this device's login (unlinked, or registered elsewhere)!";
                case "EXPIRED" -> "Signal refuses this signal-cli version as too old!";
                default -> s.toLowerCase(Locale.ROOT) + ".";
            });
            if ("CONNECTED".equals(s)) everConnected = true;
        }
    }

    /** One log event: update the link state and the counters. Text is matched, never kept. */
    private static void seen(ILoggingEvent e) {
        String logger = e.getLoggerName(), msg = e.getFormattedMessage();
        long now = System.currentTimeMillis();
        if (msg == null) msg = "";
        boolean normal = !msg.contains("unidentified");             // the logged-in connection, not the anonymous one
        if (SIGNAL.equals(logger) && normal) {
            if (msg.endsWith(" Connecting...")) { if (!"CONNECTED".equals(state)) setState("CONNECTING"); return; }
            if (msg.endsWith(" Connected")) { setState("CONNECTED"); return; }
            if (msg.endsWith(" Disconnecting...")) { setState("DISCONNECTED"); return; }
            if (msg.endsWith(" Sending keep alive...")) { keepAliveSent = now; return; }
            if (msg.endsWith("[sendKeepAlive] Success")) {
                keepAlive = now;
                if (keepAliveSent > 0 && now >= keepAliveSent) rtt = now - keepAliveSent;
                if (!"CONNECTED".equals(state)) setState("CONNECTED");
                return;
            }
            if (msg.contains("[connect] Failure")) {
                String why = throwableNames(e.getThrowableProxy()).trim();
                String reason = classify(e.getThrowableProxy(), msg);
                if (!reason.equals(fail) || !why.equals(lastWhy) || now - failAt > 300_000) {    // the reason, for docker logs
                    TlsGateway.log("Signal servers: connection failed (" + reason + "): "
                            + (why.isEmpty() ? "no details" : why.length() > 300 ? why.substring(0, 300) + "..." : why));
                    lastWhy = why;
                }
                fail = reason;
                failAt = now;
                if ("DEREGISTERED".equals(fail)) setState("AUTH_FAILED");
                else if ("EXPIRED".equals(fail)) setState("EXPIRED");
                else if ("CONNECTED".equals(state)) setState("RECONNECTING");
                return;
            }
        }
        if (RECEIVE.equals(logger)) {
            if (msg.startsWith("Checking for new message")) { loopAt = now; return; }
            if (msg.startsWith("Retrieved ")) {
                try { envelopes.addAndGet(Long.parseLong(msg.split(" ")[1])); } catch (RuntimeException ignored) { }
                return;
            }
            if (msg.startsWith("Received indicator that server queue is empty")) { caughtUp = now; return; }
            if (msg.startsWith("Connection closed unexpectedly")) {
                loopAt = now;
                if ("CONNECTED".equals(state)) { drops.incrementAndGet(); setState("RECONNECTING"); }
                return;
            }
            if (msg.startsWith("Keeping message with untrusted identity")) { problem("UNTRUSTED_IDENTITY"); return; }
            if (msg.startsWith("Received unparseable envelope")) { problem("EVENT_DROPPED"); return; }
        }
        if (HEALTH.equals(logger)) {
            if (msg.startsWith("Local clock is off")) { skewAt = now; return; }
            if (msg.startsWith("Received alerts")) { alerts.incrementAndGet(); return; }
        }
        if (!e.getLevel().isGreaterOrEqual(Level.WARN)) return;
        // the catch-all: every other warning and error, by a fixed code
        String l = msg.toLowerCase(Locale.ROOT), cls = throwableNames(e.getThrowableProxy()).toLowerCase(Locale.ROOT);
        if (msg.startsWith("Authorization failed")) { fail = "DEREGISTERED"; failAt = now; setState("AUTH_FAILED"); }
        else if (logger.endsWith("HttpServerHandler") && msg.startsWith("HTTP server has no authentication")) return;  // every start
        else if (l.startsWith("failed to decrypt incoming message")) problem("DECRYPT_FAILED");
        else if (l.startsWith("failed to serialize received message")) problem("EVENT_DROPPED");
        else if (l.startsWith("unknown error when handling") || l.startsWith("unknown error when receiving")) problem("HANDLE_ERROR");
        else if (logger.contains("sqlite") || logger.contains("hikari") || cls.contains("sqlexception") || l.contains("sqlite"))
            problem("STORAGE_ERROR");
        else if (SIGNAL.equals(logger) && msg.contains("unidentified")) problem("OTHER_WARNING");   // the anonymous socket
        else problem(e.getLevel().isGreaterOrEqual(Level.ERROR) ? "OTHER" : "OTHER_WARNING");
    }

    private static String throwableNames(IThrowableProxy t) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; t != null && i < 6; i++, t = t.getCause()) b.append(t.getClassName()).append(' ').append(t.getMessage()).append(' ');
        return b.toString();
    }

    /** Why a connection to Signal failed, as a fixed word. */
    static String classify(IThrowableProxy t, String msg) {
        String s = (throwableNames(t) + " " + msg).toLowerCase(Locale.ROOT);
        if (s.contains("devicederegistered")) return "DEREGISTERED";
        if (s.contains("appexpired")) return "EXPIRED";
        // libsignal-net tries several routes (direct, its own DNS, fallbacks) and then reports only this, without
        // the low-level reason (seen on dev 2026-10-18 with resets and with dropped packets alike)
        if (s.contains("all connect attempts failed")) return "CONNECT_FAILED";
        if (s.contains("unknownhost") || s.contains("dns") || s.contains("resolve") || s.contains("name or service")) return "DNS";
        if (s.contains("timed out") || s.contains("timeout")) return "TIMEOUT";
        if (s.contains("refused") || s.contains("reset")) return "REFUSED";
        if (s.contains("tls") || s.contains("ssl") || s.contains("certificate") || s.contains("handshake")) return "TLS";
        if (s.contains("unreachable") || s.contains("no route")) return "NETWORK";
        return "OTHER";
    }

    /** The health body for /api/v1/check. */
    static synchronized String json() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("v", 1);
        out.put("boot", BOOT);
        if (hooked) {
            Map<String, Object> sig = new LinkedHashMap<>();
            sig.put("st", state);
            sig.put("since", since);
            if (keepAlive > 0) sig.put("ka", keepAlive);
            if (rtt >= 0) sig.put("rtt", rtt);
            sig.put("rc", drops.get());
            if (fail != null) { sig.put("fail", fail); sig.put("fail_at", failAt); }
            if (skewAt > 0) sig.put("skew_at", skewAt);
            sig.put("alerts", alerts.get());
            out.put("sig", sig);
            Map<String, Object> rx = new LinkedHashMap<>();
            if (loopAt > 0) rx.put("loop", loopAt);
            rx.put("env", envelopes.get());
            if (caughtUp > 0) rx.put("caught", caughtUp);
            out.put("rx", rx);
        }
        out.put("jvm", jvm());
        Map<String, Object> gw = new LinkedHashMap<>();
        gw.put("conns", gwConnections.getAsInt());
        out.put("gw", gw);
        Map<String, Long> p = new LinkedHashMap<>();
        problems.forEach((k, v) -> p.put(k, v.get()));
        if (gwRefused.get() > 0) p.put("GATEWAY_REFUSED", gwRefused.get());
        if (gwBlocked.get() > 0) p.put("RPC_BLOCKED", gwBlocked.get());
        if (gwFull.get() > 0) p.put("GATEWAY_FULL", gwFull.get());
        out.put("p", p);
        try {
            return JSON.writeValueAsString(out);
        } catch (Exception e) {
            return "{\"v\":1,\"boot\":" + BOOT + "}";
        }
    }

    /** Heap still in use after the last garbage collection (% of the maximum), and GC time in the last minute. */
    private static Map<String, Object> jvm() {
        Map<String, Object> j = new LinkedHashMap<>();
        long used = 0, max = 0;
        for (MemoryPoolMXBean pool : ManagementFactory.getMemoryPoolMXBeans()) {
            if (pool.getType() != MemoryType.HEAP || pool.getCollectionUsage() == null) continue;
            used += pool.getCollectionUsage().getUsed();
            long m = pool.getUsage().getMax();
            max += m > 0 ? m : pool.getUsage().getCommitted();
        }
        if (max > 0) j.put("heap", Math.round(1000.0 * used / max) / 10.0);
        long gc = 0;
        for (GarbageCollectorMXBean b : ManagementFactory.getGarbageCollectorMXBeans()) gc += Math.max(0, b.getCollectionTime());
        long now = System.currentTimeMillis();
        if (gcLast >= 0 && now - gcLastAt >= 30_000) {
            gcPerMin = (gc - gcLast) * 60_000 / (now - gcLastAt);
            gcLast = gc; gcLastAt = now;
        } else if (gcLast < 0) {
            gcLast = gc; gcLastAt = now;
        }
        j.put("gc", gcPerMin);
        return j;
    }
}
