package relay;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.SocketException;
import java.net.SocketTimeoutException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.KeyFactory;
import java.security.KeyStore;
import java.security.PrivateKey;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.security.spec.PKCS8EncodedKeySpec;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Semaphore;
import javax.net.ssl.KeyManagerFactory;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLHandshakeException;
import javax.net.ssl.SSLServerSocket;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.TrustManagerFactory;

/**
 * Mutual-TLS front end for signal-cli's HTTP service, for when the subscriber runs on another network.
 * Turned on by TLS_LISTEN (e.g. 0.0.0.0:8443); signal-cli itself must then listen on 127.0.0.1 only.
 *
 *   subscriber ══TLS 1.3, client certificate══▶ gateway (TLS_LISTEN) ──plain, inside the container──▶ signal-cli
 *
 * - Only TLS 1.3. The client must present a certificate signed by our CA (TLS_DIR/ca.crt) whose name (CN) is in
 *   TLS_CLIENT_NAMES; anything else is refused before a single HTTP byte is read.
 * - Read-only allowlist, so even a stolen client certificate can't send Signal messages (the one-way rule):
 *     GET  /api/v1/events   the message stream (Last-Event-ID passed through, so missed events are resent)
 *     GET  /api/v1/check    health check: signal-cli's own answer, plus (when it is 200) a JSON body with signal-cli's
 *                           link to Signal's servers, receive loop, Java memory and problems (SignalHealth, image r2)
 *     POST /api/v1/rpc      only JSON-RPC methods in RPC_ALLOW (default listContacts), one call per request
 *   Everything else gets 403. Requests are rebuilt from a short list of headers, one request per connection.
 * - Settings: TLS_LISTEN, TLS_UPSTREAM (default 127.0.0.1:8080), TLS_DIR (default /certs: server.crt, server.key,
 *   ca.crt), TLS_CLIENT_NAMES (default signal-subscriber), RPC_ALLOW (default listContacts), TLS_CLIENT_ALLOW
 *   (r2: per client, which of events, check, rpc it may use, e.g. "signal-monitor=check;dump=events"; a client not
 *   listed may use all three, as before).
 * Logs go to stderr (never filtered by MESSAGE_LOG) and never include message contents.
 */
final class TlsGateway {
    private static final DateTimeFormatter WHEN = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss'Z'").withZone(ZoneOffset.UTC);
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final Set<String> FORWARDED = Set.of("accept", "last-event-id", "content-type", "content-length");
    private static final int MAX_HEAD = 16 * 1024, MAX_BODY = 64 * 1024, MAX_CONNECTIONS = 32;
    private static final int HANDSHAKE_MS = 15_000, STREAM_IDLE_MS = 60_000, RPC_MS = 120_000;

    private final InetSocketAddress listen, upstream;
    private final Set<String> clientNames, rpcAllow;
    private final Map<String, Set<String>> clientAllow;     // CN -> allowed of "events", "check", "rpc" (TLS_CLIENT_ALLOW)
    private final SSLServerSocket server;
    private final Semaphore slots = new Semaphore(MAX_CONNECTIONS);

    /** Starts the gateway if TLS_LISTEN is set. Exits the program if it is set but can't start: never fall back to plain. */
    static void startIfConfigured(String[] args) {
        String listen = env("TLS_LISTEN", "");
        if (listen.isEmpty()) return;
        try {
            String http = httpOption(args);
            if (http == null || !(http.startsWith("127.0.0.1:") || http.startsWith("localhost:"))) {
                throw new IllegalStateException("with TLS_LISTEN set, signal-cli must run with --http 127.0.0.1:PORT "
                        + "(it was " + (http == null ? "not set" : http) + "), so its unencrypted service stays inside the container");
            }
            TlsGateway g = new TlsGateway(address(listen), address(env("TLS_UPSTREAM", http)),
                    Path.of(env("TLS_DIR", "/certs")), names(env("TLS_CLIENT_NAMES", "signal-subscriber")),
                    names(env("RPC_ALLOW", "listContacts")), clientRules(env("TLS_CLIENT_ALLOW", "")));
            SignalHealth.gwConnections = () -> MAX_CONNECTIONS - g.slots.availablePermits();
            Thread t = new Thread(g::acceptLoop, "tls-gateway");
            t.setDaemon(true);
            t.start();
        } catch (Exception e) {
            log("TLS gateway could not start, so signal-cli will not start either: " + e.getMessage());
            System.exit(1);
        }
    }

    private TlsGateway(InetSocketAddress listen, InetSocketAddress upstream, Path dir, Set<String> clientNames,
                       Set<String> rpcAllow, Map<String, Set<String>> clientAllow) throws Exception {
        this.listen = listen;
        this.upstream = upstream;
        this.clientNames = clientNames;
        this.rpcAllow = rpcAllow;
        this.clientAllow = clientAllow;

        X509Certificate[] chain = certificates(dir.resolve("server.crt"));
        X509Certificate[] cas = certificates(dir.resolve("ca.crt"));
        KeyStore keys = KeyStore.getInstance("PKCS12");
        keys.load(null, null);
        keys.setKeyEntry("server", privateKey(dir.resolve("server.key")), new char[0], chain);
        KeyStore trust = KeyStore.getInstance("PKCS12");
        trust.load(null, null);
        for (int i = 0; i < cas.length; i++) trust.setCertificateEntry("ca" + i, cas[i]);

        KeyManagerFactory kmf = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
        kmf.init(keys, new char[0]);
        TrustManagerFactory tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        tmf.init(trust);
        SSLContext ctx = SSLContext.getInstance("TLSv1.3");
        ctx.init(kmf.getKeyManagers(), tmf.getTrustManagers(), null);

        server = (SSLServerSocket) ctx.getServerSocketFactory().createServerSocket();
        server.setEnabledProtocols(new String[]{"TLSv1.3"});
        server.setNeedClientAuth(true);
        server.bind(listen, 64);
        log("TLS gateway listening on " + listen.getHostString() + ":" + listen.getPort()
                + " (TLS 1.3, client certificate required: " + String.join(", ", clientNames) + "), forwarding to signal-cli at "
                + upstream.getHostString() + ":" + upstream.getPort() + ". Server certificate valid until "
                + WHEN.format(chain[0].getNotAfter().toInstant()) + ". Allowed: message stream, health check, RPC "
                + String.join(", ", rpcAllow) + "."
                + (clientAllow.isEmpty() ? "" : " Per client: " + clientAllow.entrySet().stream()
                        .map(e -> e.getKey() + " only " + String.join(", ", e.getValue())).sorted()
                        .collect(java.util.stream.Collectors.joining("; ")) + "."));
    }

    private void acceptLoop() {
        while (true) {
            try {
                Socket s = server.accept();
                if (!slots.tryAcquire()) {
                    SignalHealth.gwFull.incrementAndGet();
                    log("Refused a connection from " + peer(s) + ": already " + MAX_CONNECTIONS + " open.");
                    s.close();
                    continue;
                }
                Thread.ofVirtual().name("tls-conn").start(() -> {
                    try { handle((SSLSocket) s); } finally { slots.release(); }
                });
            } catch (IOException e) {
                log("TLS gateway could not accept a connection: " + e.getMessage());
            }
        }
    }

    /** A handshake we refused (no, untrusted or bad certificate; wrong TLS version) rather than one the network
     *  broke: a reset, broken pipe, timeout or early close shows up as one of these somewhere in the causes. */
    static boolean refused(Throwable e) {
        if (!(e instanceof SSLHandshakeException)) return false;
        for (Throwable c = e; c != null; c = c.getCause())
            if (c instanceof SocketException || c instanceof SocketTimeoutException || c instanceof EOFException) return false;
        return true;
    }

    private void handle(SSLSocket client) {
        String from = peer(client), who = from;
        try (client) {
            client.setSoTimeout(HANDSHAKE_MS);                   // covers the handshake and reading the request
            try {
                client.startHandshake();
            } catch (IOException e) {
                if (!refused(e)) {      // the client went away or stalled mid-handshake: not a refused certificate
                    SignalHealth.gwDropped.incrementAndGet();
                    log("A connection from " + from + " broke off during the TLS handshake (" + e.getMessage() + ").");
                    return;
                }
                SignalHealth.gwRefused.incrementAndGet();
                log("Refused a connection from " + from + ": TLS handshake failed (" + e.getMessage() + ").");
                return;
            }
            String cn = commonName((X509Certificate) client.getSession().getPeerCertificates()[0]);
            if (!clientNames.contains(cn)) {
                SignalHealth.gwRefused.incrementAndGet();
                log("Refused a connection from " + from + ": its certificate is for \"" + cn + "\", not an allowed client.");
                return;
            }
            who = from + " (" + cn + ")";

            InputStream in = client.getInputStream();
            OutputStream out = client.getOutputStream();
            Request req = Request.read(in);
            if (req == null) { reply(out, 400, "Bad request"); log("Refused a bad request from " + who + "."); return; }
            String path = req.target.split("\\?", 2)[0];
            String what;
            int idleMs;
            String kind = req.method.equals("GET") && path.equals("/api/v1/events") ? "events"
                    : req.method.equals("GET") && path.equals("/api/v1/check") ? "check"
                    : req.method.equals("POST") && path.equals("/api/v1/rpc") ? "rpc" : null;
            if (kind != null && !allowed(cn, kind)) {
                SignalHealth.gwBlocked.incrementAndGet();
                reply(out, 403, "Forbidden");
                log("Blocked a request from " + who + ": its certificate may not use " + kind + " (TLS_CLIENT_ALLOW).");
                return;
            }
            if (req.method.equals("GET") && path.equals("/api/v1/events")) {
                what = "the message stream" + (req.headers.containsKey("last-event-id") ? " (resuming after a given event)" : "");
                idleMs = STREAM_IDLE_MS;                         // signal-cli sends a keep-alive every 15 s
            } else if (req.method.equals("GET") && path.equals("/api/v1/check")) {
                check(out, who);
                return;
            } else if (req.method.equals("POST") && path.equals("/api/v1/rpc")) {
                String method = rpcMethod(req.body);
                if (method == null || !rpcAllow.contains(method)) {
                    SignalHealth.gwBlocked.incrementAndGet();
                    reply(out, 403, "Forbidden");
                    log("Blocked an RPC call from " + who + ": " + (method == null ? "not a single JSON-RPC call" : "method " + method
                            + " is not allowed (allowed: " + String.join(", ", rpcAllow) + ")") + ".");
                    return;
                }
                what = "RPC " + method;
                idleMs = RPC_MS;
            } else {
                SignalHealth.gwBlocked.incrementAndGet();
                reply(out, 403, "Forbidden");
                log("Blocked a request from " + who + ": " + req.method + " " + clean(path) + " is not allowed.");
                return;
            }
            forward(req, out, who, what, idleMs);
        } catch (IOException e) {
            log("Connection from " + who + " ended with an error: " + e.getMessage());
        }
    }

    /** The health check: asks signal-cli's own /api/v1/check; when that is 200, answers 200 with SignalHealth's body. */
    private void check(OutputStream out, String who) throws IOException {
        int status;
        try (Socket up = new Socket()) {
            up.connect(upstream, 5_000);
            up.setSoTimeout(10_000);
            up.getOutputStream().write(("GET /api/v1/check HTTP/1.1\r\nHost: " + upstream.getHostString() + ":" + upstream.getPort()
                    + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
            byte[] head = readHead(up.getInputStream());
            String[] first = head == null ? new String[0] : new String(head, StandardCharsets.ISO_8859_1).split("\r\n", 2)[0].split(" ");
            status = first.length >= 2 ? Integer.parseInt(first[1]) : 502;
        } catch (IOException | NumberFormatException e) {
            reply(out, 503, "signal-cli is not ready");
            return;
        }
        if (status / 100 != 2) { reply(out, status, "signal-cli answered " + status); return; }
        byte[] body = SignalHealth.json().getBytes(StandardCharsets.UTF_8);
        out.write(("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + body.length
                + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
        out.write(body);
        out.flush();
    }

    /** Sends the rebuilt request to signal-cli and copies the answer back, with "Connection: close". */
    private void forward(Request req, OutputStream out, String who, String what, int idleMs) throws IOException {
        try (Socket up = new Socket()) {
            try {
                up.connect(upstream, 5_000);
            } catch (IOException e) {
                reply(out, 503, "signal-cli is not ready");
                log("Could not reach signal-cli for " + who + " (" + e.getMessage() + "); it may still be starting.");
                return;
            }
            up.setSoTimeout(idleMs);
            StringBuilder head = new StringBuilder(req.method + " " + req.target + " HTTP/1.1\r\n")
                    .append("Host: ").append(upstream.getHostString()).append(':').append(upstream.getPort()).append("\r\n")
                    .append("Connection: close\r\n");
            req.headers.forEach((k, v) -> { if (FORWARDED.contains(k)) head.append(k).append(": ").append(v).append("\r\n"); });
            head.append("\r\n");
            OutputStream upOut = up.getOutputStream();
            upOut.write(head.toString().getBytes(StandardCharsets.ISO_8859_1));
            upOut.write(req.body);
            upOut.flush();

            InputStream upIn = up.getInputStream();
            byte[] respHead = readHead(upIn);
            if (respHead == null) { reply(out, 502, "Bad answer from signal-cli"); return; }
            StringBuilder rewritten = new StringBuilder();
            for (String line : new String(respHead, StandardCharsets.ISO_8859_1).split("\r\n")) {
                String lower = line.toLowerCase(Locale.ROOT);
                if (lower.startsWith("connection:") || lower.startsWith("keep-alive:")) continue;
                rewritten.append(line).append("\r\n");
            }
            rewritten.append("Connection: close\r\n\r\n");
            out.write(rewritten.toString().getBytes(StandardCharsets.ISO_8859_1));
            out.flush();
            if (what != null) log("Opened " + what + " for " + who + ".");
            byte[] buf = new byte[16 * 1024];
            for (int n; (n = upIn.read(buf)) > 0; ) {
                out.write(buf, 0, n);
                out.flush();                                     // stream events as they arrive
            }
            if (what != null && what.startsWith("the message stream")) log("Closed the message stream for " + who + ".");
        }
    }

    /** One HTTP request: method, target, headers (lower-case names) and body. Null if malformed or not allowed. */
    private record Request(String method, String target, Map<String, String> headers, byte[] body) {
        static Request read(InputStream in) throws IOException {
            byte[] raw = readHead(in);
            if (raw == null) return null;
            String[] lines = new String(raw, StandardCharsets.ISO_8859_1).split("\r\n");
            String[] first = lines[0].split(" ");
            if (first.length != 3 || !first[2].startsWith("HTTP/1.") || !first[1].startsWith("/")) return null;
            Map<String, String> headers = new HashMap<>();
            for (int i = 1; i < lines.length; i++) {
                int colon = lines[i].indexOf(':');
                if (colon <= 0) return null;
                String name = lines[i].substring(0, colon).trim().toLowerCase(Locale.ROOT);
                String value = lines[i].substring(colon + 1).trim();
                if (value.chars().anyMatch(c -> c < 0x20 && c != '\t')) return null;
                if (headers.put(name, value) != null && FORWARDED.contains(name)) return null;   // no duplicate lengths etc.
            }
            if (headers.containsKey("transfer-encoding")) return null;         // only plain Content-Length bodies
            int length = 0;
            if (headers.containsKey("content-length")) {
                try { length = Integer.parseInt(headers.get("content-length")); } catch (NumberFormatException e) { return null; }
                if (length < 0 || length > MAX_BODY) return null;
            }
            byte[] body = in.readNBytes(length);
            if (body.length != length) return null;
            return new Request(first[0], first[1], headers, body);
        }
    }

    /** Reads up to the blank line that ends an HTTP head; null if it's too long or the connection ends first. */
    private static byte[] readHead(InputStream in) throws IOException {
        ByteArrayOutputStream head = new ByteArrayOutputStream();
        int matched = 0;
        while (head.size() < MAX_HEAD) {
            int b = in.read();
            if (b < 0) return null;
            head.write(b);
            matched = (b == "\r\n\r\n".charAt(matched)) ? matched + 1 : (b == '\r' ? 1 : 0);
            if (matched == 4) {
                byte[] all = head.toByteArray();
                return java.util.Arrays.copyOf(all, all.length - 4);
            }
        }
        return null;
    }

    /** The method of a single JSON-RPC call, or null for batches, notifications without a method, or bad JSON. */
    private static String rpcMethod(byte[] body) {
        try {
            JsonNode n = JSON.readTree(body);
            return n != null && n.isObject() && n.path("method").isTextual() ? n.get("method").asText() : null;
        } catch (IOException e) {
            return null;
        }
    }

    private static void reply(OutputStream out, int status, String text) throws IOException {
        byte[] body = (text + "\n").getBytes(StandardCharsets.UTF_8);
        String reason = switch (status) { case 400 -> "Bad Request"; case 403 -> "Forbidden"; case 502 -> "Bad Gateway";
                                          default -> "Service Unavailable"; };
        out.write(("HTTP/1.1 " + status + " " + reason + "\r\nContent-Type: text/plain\r\nContent-Length: " + body.length
                + "\r\nConnection: close\r\n\r\n").getBytes(StandardCharsets.ISO_8859_1));
        out.write(body);
        out.flush();
    }

    private static X509Certificate[] certificates(Path file) throws Exception {
        try (InputStream in = Files.newInputStream(file)) {
            List<X509Certificate> list = new ArrayList<>();
            for (Certificate c : CertificateFactory.getInstance("X.509").generateCertificates(in)) list.add((X509Certificate) c);
            if (list.isEmpty()) throw new IllegalStateException("no certificate in " + file);
            return list.toArray(new X509Certificate[0]);
        }
    }

    /** A PEM "PRIVATE KEY" (PKCS#8, as openssl genpkey writes it), EC or RSA. */
    private static PrivateKey privateKey(Path file) throws Exception {
        String pem = Files.readString(file);
        int begin = pem.indexOf("-----BEGIN PRIVATE KEY-----"), end = pem.indexOf("-----END PRIVATE KEY-----");
        if (begin < 0 || end < 0) throw new IllegalStateException(file + " is not a PEM PKCS#8 private key");
        byte[] der = Base64.getMimeDecoder().decode(pem.substring(begin + 27, end));
        for (String alg : List.of("EC", "RSA")) {
            try { return KeyFactory.getInstance(alg).generatePrivate(new PKCS8EncodedKeySpec(der)); } catch (Exception ignored) { }
        }
        throw new IllegalStateException(file + " is not an EC or RSA key");
    }

    /** The certificate's CN (our certificates never have commas in their names). */
    private static String commonName(X509Certificate cert) {
        for (String part : cert.getSubjectX500Principal().getName().split(",")) {
            if (part.startsWith("CN=")) return part.substring(3);
        }
        return "";
    }

    /** Value of signal-cli's --http option (after the daemon subcommand), or null. */
    private static String httpOption(String[] args) {
        for (int i = 0; i < args.length; i++) {
            if (args[i].equals("--http") && i + 1 < args.length) return args[i + 1];
            if (args[i].startsWith("--http=")) return args[i].substring(7);
        }
        return null;
    }

    private static InetSocketAddress address(String hostPort) {
        int colon = hostPort.lastIndexOf(':');
        if (colon < 0) throw new IllegalArgumentException("\"" + hostPort + "\" must be HOST:PORT");
        return new InetSocketAddress(hostPort.substring(0, colon), Integer.parseInt(hostPort.substring(colon + 1)));
    }

    /** "signal-monitor=check;dump=events,check" -> CN -> allowed endpoints. Refuses unknown words, so a typo can't
     *  silently allow nothing or everything. */
    private static Map<String, Set<String>> clientRules(String spec) {
        Map<String, Set<String>> out = new HashMap<>();
        for (String rule : spec.split(";")) {
            if (rule.isBlank()) continue;
            String[] kv = rule.split("=", 2);
            if (kv.length != 2 || kv[0].isBlank()) throw new IllegalArgumentException("TLS_CLIENT_ALLOW: \"" + rule + "\" is not NAME=what,what");
            Set<String> what = names(kv[1]);
            for (String w : what)
                if (!Set.of("events", "check", "rpc").contains(w))
                    throw new IllegalArgumentException("TLS_CLIENT_ALLOW: \"" + w + "\" is not one of events, check, rpc");
            out.put(kv[0].trim(), what);
        }
        return out;
    }

    private boolean allowed(String cn, String what) {
        Set<String> a = clientAllow.get(cn);
        return a == null || a.contains(what);
    }

    private static Set<String> names(String list) {
        Set<String> s = new java.util.LinkedHashSet<>();
        for (String x : list.split(",")) if (!x.isBlank()) s.add(x.trim());
        return s;
    }

    private static String env(String name, String fallback) {
        String v = System.getenv(name);
        return v == null || v.isBlank() ? fallback : v.trim();
    }

    private static String peer(Socket s) {
        return s.getInetAddress() == null ? "unknown address" : s.getInetAddress().getHostAddress();
    }

    private static String clean(String s) {
        String t = s.replaceAll("[^\\x20-\\x7e]", "?");
        return t.length() > 80 ? t.substring(0, 80) + "…" : t;
    }

    static void log(String line) {
        System.err.println(WHEN.format(Instant.now()) + "  " + line);
    }
}
