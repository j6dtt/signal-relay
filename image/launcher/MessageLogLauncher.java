package relay;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.Iterator;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/**
 * Starts signal-cli with its received-message log controlled by MESSAGE_LOG:
 *   redacted (default)  one plain-English line per event; message text, quotes, previews, captions, file names,
 *                       polls, contact cards and anything unrecognised are hidden (only lengths or counts shown)
 *   full                signal-cli's normal output, including message text
 *   off                 nothing
 * Only the "daemon" and "receive" commands are affected; every other command (link, register, ...) runs unchanged.
 * For redacted and off, signal-cli is switched to JSON output (one line per event, so multi-line messages can't
 * leak past the filter) and System.out is replaced by a filter that turns each line into a summary.
 *
 * Help (-h/--help) is never filtered. For "daemon", TLS_LISTEN also starts the mutual-TLS gateway (TlsGateway.java).
 *
 * Self-test: java @java.args --summarize-stdin < events.jsonl   (prints the summary of each JSON line)
 */
public final class MessageLogLauncher {
    private static final Set<String> RECEIVING = Set.of("daemon", "receive");
    private static final Set<String> GLOBAL_WITH_VALUE = Set.of("-a", "--account", "-c", "--config", "-d", "--data-dir",
            "-o", "--output", "--log-file", "--service-environment", "--trust-new-identities", "--bus-name");

    public static void main(String[] args) throws Exception {
        if (List.of(args).contains("--summarize-stdin")) {    // the image's entrypoint adds --config /data first
            Summarizer s = new Summarizer(false);
            BufferedReader in = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
            PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out), true, StandardCharsets.UTF_8);
            for (String line; (line = in.readLine()) != null; ) {
                String summary = s.apply(line);
                if (summary != null) out.println(summary);
            }
            return;
        }
        String mode = System.getenv().getOrDefault("MESSAGE_LOG", "redacted").trim().toLowerCase(Locale.ROOT);
        if (!Set.of("redacted", "full", "off").contains(mode)) {
            System.err.println("MESSAGE_LOG must be redacted, full or off (was \"" + mode + "\"); using redacted.");
            mode = "redacted";
        }
        boolean help = List.of(args).contains("-h") || List.of(args).contains("--help");
        if (!mode.equals("full") && !help && RECEIVING.contains(subcommand(args))) {
            args = withJsonOutput(args);
            OutputStream filter = new LineFilter(new FileOutputStream(FileDescriptor.out), new Summarizer(mode.equals("off")));
            System.setOut(new PrintStream(filter, true, StandardCharsets.UTF_8));
        }
        if (subcommand(args).equals("daemon") && !help) {
            TlsGateway.startIfConfigured(args);                  // only if TLS_LISTEN is set
            SignalHealth.start();                                // signal-cli's own health, for the gateway's check
        }
        startSignalCli(args);
    }

    /** Runs org.asamk.signal.Main's main(String[]). It isn't public (Java 25 allows that), so call it reflectively. */
    static void startSignalCli(String[] args) throws Exception {
        Class<?> main = Class.forName("org.asamk.signal.Main");
        java.lang.reflect.Method m = main.getDeclaredMethod("main", String[].class);
        m.setAccessible(true);
        Object target = null;
        if (!java.lang.reflect.Modifier.isStatic(m.getModifiers())) {
            java.lang.reflect.Constructor<?> c = main.getDeclaredConstructor();
            c.setAccessible(true);
            target = c.newInstance();
        }
        try {
            m.invoke(target, (Object) args);
        } catch (java.lang.reflect.InvocationTargetException e) {
            if (e.getCause() instanceof Exception ex) throw ex;
            if (e.getCause() instanceof Error err) throw err;
            throw e;
        }
    }

    /** First argument that isn't a global option or an option's value, e.g. "daemon". */
    static String subcommand(String[] args) {
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            if (!a.startsWith("-")) return a;
            if (!a.contains("=") && GLOBAL_WITH_VALUE.contains(a)) i++;
        }
        return "";
    }

    /** The same arguments with "-o json" in front, and any other output option removed. */
    static String[] withJsonOutput(String[] args) {
        List<String> out = new ArrayList<>(List.of("-o", "json"));
        String sub = subcommand(args);
        boolean beforeSub = true;
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            if (beforeSub && a.equals(sub)) beforeSub = false;
            if (beforeSub && (a.equals("-o") || a.equals("--output"))) { i++; continue; }
            if (beforeSub && (a.startsWith("-o=") || a.startsWith("--output="))) continue;
            out.add(a);
        }
        return out.toArray(new String[0]);
    }

    /** Collects bytes into lines and writes each line's summary (or nothing) to the real stdout. */
    static final class LineFilter extends OutputStream {
        private final OutputStream out;
        private final Summarizer summarizer;
        private final ByteArrayOutputStream line = new ByteArrayOutputStream();

        LineFilter(OutputStream out, Summarizer summarizer) { this.out = out; this.summarizer = summarizer; }

        @Override public synchronized void write(int b) throws IOException {
            if (b == '\n') emit(); else line.write(b);
        }

        @Override public synchronized void write(byte[] b, int off, int len) throws IOException {
            for (int i = off; i < off + len; i++) write(b[i]);
        }

        @Override public synchronized void close() throws IOException {
            if (line.size() > 0) emit();
            out.close();
        }

        private void emit() throws IOException {
            String summary = summarizer.apply(line.toString(StandardCharsets.UTF_8));
            line.reset();
            if (summary != null) {
                out.write((summary + "\n").getBytes(StandardCharsets.UTF_8));
                out.flush();
            }
        }
    }

    /** Turns one line of signal-cli JSON output into a plain-English summary with all content hidden. */
    static final class Summarizer {
        private static final ObjectMapper JSON = new ObjectMapper();
        private static final DateTimeFormatter WHEN = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss'Z'").withZone(ZoneOffset.UTC);
        private static final DateTimeFormatter TIME = DateTimeFormatter.ofPattern("HH:mm:ss'Z'").withZone(ZoneOffset.UTC);
        private static final DateTimeFormatter DAY = DateTimeFormatter.ofPattern("yyyy-MM-dd").withZone(ZoneOffset.UTC);
        private static final Set<String> DATA_KEYS = Set.of("timestamp", "message", "expiresInSeconds", "isExpirationUpdate",
                "viewOnce", "groupCallUpdate", "isEndSession", "isProfileKeyUpdate", "hasProfileKey", "reaction", "quote",
                "payment", "mentions", "previews", "attachments", "sticker", "remoteDelete", "contacts", "pollCreate",
                "pollVote", "pollTerminate", "textStyles", "groupInfo", "storyContext", "pinMessage", "unpinMessage",
                "adminDelete");
        private final boolean off;
        private final Map<String, String> names = new HashMap<>();   // uuid / number -> profile name, from senders
        private final Map<String, String> groups = new HashMap<>();  // group id -> group name
        private long eventTime;

        Summarizer(boolean off) { this.off = off; }

        String apply(String line) {
            if (line.isBlank()) return null;
            JsonNode node;
            try { node = JSON.readTree(line); } catch (IOException e) { node = null; }
            if (node == null || !node.isObject() || !node.has("envelope")) {
                return off ? null : "Other output from signal-cli [hidden, " + line.length() + " chars]";
            }
            if (off) return null;
            try {
                return summarize(node);
            } catch (RuntimeException e) {     // never let an odd event stop the log or leak its contents
                return "Event that could not be summarised [hidden, " + line.length() + " chars]";
            }
        }

        private String summarize(JsonNode node) {
            JsonNode env = node.path("envelope");
            String name = text(env, "sourceName"), number = text(env, "sourceNumber"), uuid = text(env, "sourceUuid");
            if (name != null) { if (uuid != null) names.put(uuid, name); if (number != null) names.put(number, name); }
            eventTime = env.path("timestamp").asLong();
            String head = WHEN.format(Instant.ofEpochMilli(eventTime)) + "  ";
            String who = person(name, number, uuid);
            String dev = env.has("sourceDevice") ? " (device " + env.get("sourceDevice").asInt() + ")" : "";

            if (node.has("exception")) {
                return head + "Message that could not be read from " + who + dev + ": "
                        + node.path("exception").path("type").asText("unknown error");
            }
            if (env.has("dataMessage")) {
                return head + data(env.get("dataMessage"), null, who + dev, null);
            }
            if (env.has("editMessage")) {
                JsonNode edit = env.get("editMessage");
                return head + data(edit.path("dataMessage"), edit.path("targetSentTimestamp").asLong(), who + dev, null);
            }
            if (env.has("syncMessage")) {
                JsonNode sync = env.get("syncMessage");
                String self = "this account (" + who + ", device " + env.path("sourceDevice").asInt()
                        + (env.path("sourceDevice").asInt() == 1 ? ", main device" : "") + ")";
                if (sync.has("sentMessage")) {
                    JsonNode sent = sync.get("sentMessage");
                    String to = sent.has("groupInfo") ? null
                            : " to " + person(null, text(sent, "destinationNumber"), text(sent, "destinationUuid"));
                    return head + data(sent, null, self, to);
                }
                if (sync.has("readMessages")) {
                    return head + "Read on another device of " + self + ": " + count(sync.get("readMessages").size(), "message");
                }
                return head + "Sync event from " + self + ": " + String.join(", ", keys(sync)) + " [hidden]";
            }
            if (env.has("receiptMessage")) {
                JsonNode r = env.get("receiptMessage");
                int n = r.path("timestamps").size();
                boolean read = r.path("isRead").asBoolean(), viewed = r.path("isViewed").asBoolean();
                String kind = read ? "Read" : viewed ? "Viewed" : "Delivery";
                String verb = read ? "read" : viewed ? "viewed" : "delivered";
                return head + kind + " receipt from " + who + dev + ": " + verb + " " + count(n, "message");
            }
            if (env.has("typingMessage")) {
                JsonNode t = env.get("typingMessage");
                String action = "STOPPED".equals(text(t, "action")) ? "stopped typing" : "started typing";
                return head + "Typing from " + who + dev + group(text(t, "groupId"), null) + ": " + action;
            }
            if (env.has("storyMessage")) {
                JsonNode st = env.get("storyMessage");
                String what = st.has("textAttachment") ? "text " + hidden(text(st.path("textAttachment"), "text"))
                        : st.has("fileAttachment") ? attachments(List.of(st.get("fileAttachment"))) : "contents [hidden]";
                return head + "Story from " + who + dev + group(text(st, "groupId"), null) + ": " + what;
            }
            if (env.has("callMessage")) {
                return head + "Call event from " + who + dev + ": " + String.join(", ", keys(env.get("callMessage")));
            }
            return head + "Other event from " + who + dev + ": kind not recognised, contents [hidden]";
        }

        /** One data message (normal, edited, or sent from this account), as "<Kind> from <who> in group ...: ...". */
        private String data(JsonNode dm, Long editTarget, String who, String to) {
            JsonNode gi = dm.path("groupInfo");
            String where = gi.isMissingNode() ? (to != null ? to : "") : group(text(gi, "groupId"), text(gi, "groupName"));
            String msg = text(dm, "message");
            List<String> details = new ArrayList<>();
            String kind;
            boolean sentByUs = who.startsWith("this account");

            if (editTarget != null) {
                kind = "Edit";
                details.add("their message from " + at(editTarget) + " now reads " + hidden(msg));
                msg = null;
            } else if (dm.has("remoteDelete")) {
                kind = "Deletion";
                details.add("deleted " + (sentByUs ? "a" : "their") + " message from " + at(dm.path("remoteDelete").path("timestamp").asLong()));
            } else if (dm.has("adminDelete")) {
                kind = "Admin deletion";
                details.add("deleted a message");
            } else if (dm.has("reaction")) {
                JsonNode r = dm.get("reaction");
                boolean removed = r.path("isRemove").asBoolean();
                kind = removed ? "Reaction removed" : "Reaction";
                details.add(r.path("emoji").asText("") + (removed ? " from " : " to ")
                        + ref(text(r, "targetAuthorUuid"), text(r, "targetAuthorNumber"), text(r, "targetAuthor"))
                        + "'s message from " + at(r.path("targetSentTimestamp").asLong()));
            } else if (dm.has("pollCreate")) {
                kind = "Poll";
                JsonNode p = dm.get("pollCreate");
                details.add("question " + hidden(text(p, "question")));
                if (p.path("options").isArray()) details.add(p.get("options").size() + " options [hidden]");
            } else if (dm.has("pollVote")) {
                kind = "Poll vote";
                details.add("vote [hidden]");
            } else if (dm.has("pollTerminate")) {
                kind = "Poll ended";
                details.add("poll closed");
            } else if (dm.has("pinMessage") || dm.has("unpinMessage")) {
                kind = dm.has("pinMessage") ? "Pin" : "Unpin";
                details.add(dm.has("pinMessage") ? "pinned a message" : "unpinned a message");
            } else if (dm.has("sticker")) {
                kind = "Sticker";
                details.add("1 sticker");
            } else if (dm.has("contacts")) {
                kind = "Contact card";
                details.add(dm.get("contacts").size() + " shared contact" + (dm.get("contacts").size() == 1 ? "" : "s") + " [hidden]");
            } else if (dm.path("isExpirationUpdate").asBoolean()) {
                kind = "Disappearing-message timer";
                int s = dm.path("expiresInSeconds").asInt();
                details.add(s == 0 ? "turned off" : "set to " + duration(s));
            } else if (msg == null && dm.path("attachments").size() == 0
                    && ("UPDATE".equals(text(gi, "type")) || "QUIT".equals(text(gi, "type")))) {
                kind = "Group change";
                details.add("QUIT".equals(text(gi, "type")) ? "left the group" : "group updated (revision " + gi.path("revision").asInt() + ")");
            } else if (dm.path("attachments").size() > 0) {
                kind = "Attachment";
            } else if (dm.has("quote")) {
                kind = "Reply";
            } else if (dm.has("groupCallUpdate")) {
                kind = "Group call";
                details.add("call started or updated");
            } else {
                kind = "Text message";
            }

            if (dm.path("attachments").size() > 0) {
                List<JsonNode> list = new ArrayList<>();
                dm.get("attachments").forEach(list::add);
                details.add(attachments(list));
            }
            if (msg != null && !msg.isEmpty()) details.add("text " + hidden(msg));
            if (dm.has("quote")) {
                JsonNode q = dm.get("quote");
                String qt = text(q, "text");
                details.add("quoting " + ref(text(q, "authorUuid"), text(q, "authorNumber"), text(q, "author"))
                        + "'s message from " + at(q.path("id").asLong()) + (qt == null || qt.isEmpty() ? "" : " " + hidden(qt)));
            }
            if (dm.path("mentions").size() > 0) {
                List<String> who2 = new ArrayList<>();
                dm.get("mentions").forEach(m -> who2.add(ref(text(m, "uuid"), text(m, "number"), text(m, "name"))));
                details.add("mentions " + String.join(", ", who2));
            }
            if (dm.path("previews").size() > 0) {
                JsonNode p = dm.get("previews").get(0);
                details.add("link preview [hidden: title " + len(text(p, "title")) + " chars, description "
                        + len(text(p, "description")) + " chars, address " + len(text(p, "url")) + " chars]");
            }
            if (dm.has("payment")) details.add("payment [hidden]");
            if (dm.has("storyContext")) details.add("replying to a story");
            if (dm.path("viewOnce").asBoolean()) details.add("view once");
            List<String> unknown = new ArrayList<>();
            dm.fieldNames().forEachRemaining(k -> { if (!DATA_KEYS.contains(k) && !k.startsWith("destination")) unknown.add(k); });
            if (!unknown.isEmpty()) details.add("other contents [hidden]");
            if (details.isEmpty()) details.add("no text");

            if (gi.isMissingNode() && !sentByUs) {                     // not in a group: a one-to-one chat
                if (kind.equals("Text message")) kind = "Direct message"; else where = " in a direct chat";
            }
            String verb = kind.equals("Reaction removed") || kind.equals("Group change") ? " by " : " from ";
            String line = sentByUs ? "Sent from " + who + (where.startsWith(" in group") ? " to group" + where.substring(9) : where)
                    + " (" + kind.toLowerCase(Locale.ROOT) + ")"
                    : kind + verb + who + where;
            return line + ": " + String.join(", ", details);
        }

        private String attachments(List<JsonNode> list) {
            List<String> each = new ArrayList<>();
            for (JsonNode a : list) {
                List<String> bits = new ArrayList<>();
                bits.add(a.path("contentType").asText("unknown type"));
                if (a.has("size")) bits.add(size(a.get("size").asLong()));
                if (text(a, "filename") != null) bits.add("file name [hidden]");
                if (text(a, "caption") != null) bits.add("caption " + hidden(text(a, "caption")));
                each.add(String.join(", ", bits));
            }
            return count(list.size(), "attachment") + " (" + String.join("; ", each) + ")";
        }

        private String group(String id, String name) {
            if (id == null) return "";
            if (name != null && !name.isEmpty()) groups.put(id, name); else name = groups.get(id);
            String shortId = id.length() > 6 ? id.substring(0, 6) + "…" : id;
            return name != null ? " in group “" + name + "” (" + shortId + ")" : " in group " + shortId;
        }

        /** Sender: “Name” +1555…567, or “Name” (number hidden, af1c84bc…). */
        private static String person(String name, String number, String uuid) {
            String quoted = name != null && !name.isBlank() ? "“" + name + "” " : "";
            if (number != null) return quoted + shortNumber(number);
            if (uuid != null) return quoted + "(number hidden, " + shortUuid(uuid) + ")";
            return quoted.isEmpty() ? "unknown sender" : quoted.trim();
        }

        /** Someone referred to (quoted, reacted to, mentioned): their name if they've sent a message since start. */
        private String ref(String uuid, String number, String fallback) {
            String name = uuid != null ? names.get(uuid) : null;
            if (name == null && number != null) name = names.get(number);
            if (name == null && fallback != null) name = names.get(fallback);
            if (name != null) return "“" + name + "”";
            if (number != null) return shortNumber(number);
            if (uuid != null) return shortUuid(uuid);
            if (fallback != null) return fallback.startsWith("+") ? shortNumber(fallback) : shortUuid(fallback);
            return "someone";
        }

        static String shortNumber(String n) { return n.length() > 8 ? n.substring(0, 5) + "…" + n.substring(n.length() - 3) : n; }

        static String shortUuid(String u) { return u.length() > 8 ? u.substring(0, 8) + "…" : u; }

        private String at(long ts) {
            if (ts <= 0) return "an earlier time";
            Instant t = Instant.ofEpochMilli(ts), e = Instant.ofEpochMilli(eventTime);
            return DAY.format(t).equals(DAY.format(e)) ? TIME.format(t) : WHEN.format(t);
        }

        private static String hidden(String s) {
            int lines = s == null ? 0 : (int) s.chars().filter(c -> c == '\n').count() + 1;
            return "[hidden, " + len(s) + " chars" + (lines > 1 ? ", " + lines + " lines" : "") + "]";
        }

        private static int len(String s) { return s == null ? 0 : s.codePointCount(0, s.length()); }

        private static String count(int n, String what) { return n + " " + what + (n == 1 ? "" : "s"); }

        private static String size(long b) {
            if (b < 1024) return b + " B";
            if (b < 1024 * 1024) return Math.round(b / 1024.0) + " KB";
            return String.format(Locale.ROOT, "%.1f MB", b / (1024.0 * 1024));
        }

        private static String duration(int s) {
            int[] units = {604800, 86400, 3600, 60, 1};
            String[] names = {"week", "day", "hour", "minute", "second"};
            for (int i = 0; i < units.length; i++) {
                if (s >= units[i] && s % units[i] == 0) return count(s / units[i], names[i]);
            }
            return count(s, "second");
        }

        private static List<String> keys(JsonNode n) {
            List<String> k = new ArrayList<>();
            for (Iterator<String> it = n.fieldNames(); it.hasNext(); ) k.add(it.next());
            return k;
        }

        private static String text(JsonNode n, String field) {
            JsonNode v = n.path(field);
            return v.isTextual() ? v.asText() : null;
        }
    }
}
