#!/usr/bin/env python3
"""Convert relay CEF/UDP datagrams back into the original Signal JSON events and deliver them.

This is the receiving end of subscriber_udp.py, without any chat display: it is the starting point for
delivering messages to Palantir. It runs on its own (container: signal-receiver).

Packet format (one CEF line per UDP datagram, see subscriber_udp.py):
    CEF:0|CHAT|<ROOM>|<i>|<n>|SIGNAL-RELAY|APP|{"s":session,"q":seq,"m":msgid,"i":i,"n":n,"d":"<json slice>"}
The diode routes on fields 1 and 2 (CHAT, the room): each room goes to its receiver. ROOMS lists the room(s) this
receiver serves (prod: one; dev, with no diode: all of them); a packet for any other room is dropped and counted
(ROOM_MISMATCH), so it can never reach the wrong Gotham room. Each sender run has its own session per room.
The "d" slices of one (session, msgid) are joined in order of i and json-decoded into the original event.
Any other header (including the old CEF:0|J6-DTT|CHAT|, before rooms) is not a chat packet: counted as BAD_PACKET.

Where rebuilt events go (see deliver()):
  - CONSOLE_URL set: HTTP POST to <url>/event (lost-message notices to /notice, counters to /stats).
    This is how the signal-receiver container feeds console_web.py; later, deliver() sends to Palantir.
  - CONSOLE_URL unset: printed to stdout (pretty JSON in a terminal, one JSON object per line when piped).
  - Also, every shown message becomes a simple record (chat_render.py's Chat.record: time, group, from, type,
    text) and goes to my_forward(), which for now appends it to RECORD_LOG (one JSON object per line).
Plain-English logs always go to stderr.
    python cef_to_json.py                                   # print events
    CONSOLE_URL=http://127.0.0.1:8091 python cef_to_json.py   # feed console_web.py
"""
import json, logging, os, re, shutil, signal, socket, sys, threading, time, urllib.error, urllib.request
from collections import OrderedDict
from comlibv3 import parse_string_in_cef
from chat_render import Chat
#import mss_stream

LISTEN      = (os.environ.get("UDP_LISTEN", "127.0.0.1"), int(os.environ.get("UDP_PORT", 6510)))  # "0.0.0.0" for diode egress
RCVBUF      = 8 * 1024 * 1024      # capped by sysctl net.core.rmem_max
TAG         = "SIGNAL-RELAY"       # CEF Name field expected from the sender
ROOM_NAME   = re.compile(r"^[A-Z0-9_-]{1,32}$")   # the diode's rules need capitals; also safe in a CEF header
RESERVED    = {"MONITOR"}          # CEF:0|CHAT|MONITOR| is the monitoring stream (monitor-receiver)
ROOMS       = [r.strip() for r in os.environ.get("ROOMS", "").split(",") if r.strip()]   # the room(s) it serves
RX_NAME     = os.environ.get("RECEIVER_NAME", "")   # its name on the Monitor tab; "" = its container name (found by the console)
# where my_forward delivers this receiver's room (its box on the Monitor tab, e.g. "Gotham Chat"); "off" = this room
# goes nowhere past the receiver: my_forward isn't called (no records) and the Monitor tab shows no destination box
FORWARD_TO  = os.environ.get("FORWARD_TO", "Gotham Chat").strip()
FORWARD     = FORWARD_TO.lower() not in ("", "off", "none", "no")
TIMEOUT     = 30                   # seconds to wait for all parts of one message
MAX_PENDING = 1000                 # incomplete messages held at once
MAX_PARTS   = 2000                 # reject absurd part counts
SEEN_MAX    = 20000                # recent (session, seq) kept for duplicate detection
PRETTY      = os.environ.get("PRETTY", "auto")   # "yes", "no", or "auto" (pretty only in a terminal)
CONSOLE_URL = os.environ.get("CONSOLE_URL", "").rstrip("/")  # e.g. http://console:8091; empty = print to stdout
POST_TIMEOUT = 5                  # seconds per HTTP delivery
RECORD_LOG  = os.environ.get("RECORD_LOG", "")   # where my_forward() appends simple records; empty = off
FORWARD_SLOW_S = 2                 # my_forward() taking longer than this holds up the receiving loop
STALL_S     = 20                   # the receiving loop silent this long is stuck (the watchdog reports it)
SESSIONS    = 8                    # senders (subscriber runs) remembered for the "did it arrive?" check
PROGRESS    = os.environ.get("PROGRESS_FILE", "")   # where that progress is kept across restarts; empty = memory only

log = logging.getLogger("cef_to_json")
PROBLEMS = {}                      # fixed code -> count since start: every warning and error (the catch-all)


class CountProblems(logging.Handler):
    """Counts every warning and error by its fixed code (extra={"code": ...}); anything else is OTHER /
    OTHER_WARNING. Sent with the counters to the console: codes and counts only, never log text."""
    def emit(self, record):
        if record.levelno >= logging.WARNING:
            code = getattr(record, "code", None) or ("OTHER" if record.levelno >= logging.ERROR else "OTHER_WARNING")
            PROBLEMS[code] = PROBLEMS.get(code, 0) + 1


def problem(code):
    return {"code": code}


def rx_drops():
    """Packets the kernel dropped because our receive buffer was full (this container's own network counters)."""
    try:
        rows = [l.split() for l in open("/proc/net/snmp") if l.startswith("Udp:")]
        return int(dict(zip(rows[0][1:], rows[1][1:]))["RcvbufErrors"])
    except (OSError, IndexError, KeyError, ValueError):
        return None


def describe(event):
    """Short plain-English description of a Signal event, e.g. 'reaction from Tho Pham'."""
    env = event.get("envelope", {})
    who = env.get("sourceName") or env.get("sourceNumber") or "unknown sender"
    edit = env.get("editMessage")
    dm = ((edit or {}).get("dataMessage") or env.get("dataMessage")
          or (env.get("syncMessage") or {}).get("sentMessage") or {})
    if edit:                                   kind = "edited message"
    elif dm.get("remoteDelete"):               kind = "deleted message"
    elif dm.get("reaction"):                   kind = "reaction"
    elif dm.get("sticker"):                    kind = "sticker"
    elif dm.get("attachments"):                kind = f"{len(dm['attachments'])} attachment(s)"
    elif dm.get("quote"):                      kind = "reply"
    elif (dm.get("groupInfo") or {}).get("type") in ("UPDATE", "QUIT"): kind = "group change"
    else:                                      kind = "text message"
    return f"{kind} from {who}"


class Reassembler:
    """Feed it raw datagrams; it calls on_event(event, msgid) for every complete Signal event.

    on_event may return a short phrase that is appended to the "Rebuilt message" log line.
    on_lost(text), if given, is called with a plain-English note when a message cannot be completed.
    """
    def __init__(self, on_event, on_lost=None):
        self.on_event = on_event
        self.on_lost = on_lost
        self.pending = OrderedDict()   # (s, m) -> {"n", "parts": {i: d}, "t", "bytes"}
        self.seen = OrderedDict()      # (s, q) -> None
        self.next_q = {}               # s -> next expected seq
        self.stats = dict(packets=0, messages=0, lost=0, duplicates=0, bad=0, missing_packets=0, wrong_room=0)
        self.sessions = OrderedDict()  # s -> [highest message number done (rebuilt or given up), rebuilt, lost]
        self.since = int(time.time() * 1000)      # since when self.sessions has been kept (survives a restart)
        self.sess_room = OrderedDict() # s -> the room its packets carry (one room per session)
        self.rooms = {r: room_row() for r in ROOMS}   # per room: packets, messages, lost, packets missing, last packet
        self.wrong = {}                # room -> packets dropped because this receiver doesn't serve that room
        self.wrong_logged = {}         # room -> when last logged (at most once a minute per room)
        self.load()

    def datagram(self, data):
        self.stats["packets"] += 1
        try:
            text = data.decode("ascii")
            room = room_of(text)
            if room not in self.rooms:                # before anything else: it isn't ours, whatever it holds
                self.wrong_room(room)
                return
            parts, tag = parse_string_in_cef(text)
            p = parts[0]
            s, q, m, i, n, d = p["s"], p["q"], p["m"], p["i"], p["n"], p["d"]
            if tag != TAG or not (1 <= i <= n <= MAX_PARTS):
                raise ValueError(f"tag={tag} part {i} of {n}")
        except (UnicodeDecodeError, IndexError, KeyError, TypeError, ValueError) as e:
            self.stats["bad"] += 1
            log.warning("Received a packet (%d bytes) that is not a valid relay packet, so it was ignored: %r",
                        len(data), e, extra=problem("BAD_PACKET"))
            return
        rr = self.rooms[room]
        rr["packets"] += 1
        rr["last_at"] = int(time.time() * 1000)

        if (s, q) in self.seen:                       # COPIES > 1, or network duplicate
            self.stats["duplicates"] += 1
            log.debug("Ignored a duplicate of packet #%d.", q)
            return
        self.seen[(s, q)] = None
        if len(self.seen) > SEEN_MAX:
            self.seen.popitem(last=False)

        if s not in self.next_q:
            log.info("A sender started (session %s%s). Its first packet is #%d.", s, in_room(room), q)
            self.next_q[s] = q
        if self.sess_room.get(s) != room:
            self.sess_room[s] = room
            while len(self.sess_room) > 4 * SESSIONS:
                self.sess_room.popitem(last=False)
        if q > self.next_q[s]:
            missing = q - self.next_q[s]
            self.stats["missing_packets"] += missing
            rr["missing_packets"] += missing
            which = f"#{self.next_q[s]}" if missing == 1 else f"#{self.next_q[s]} to #{q - 1}"
            log.warning("%d packet(s)%s never arrived (%s). Any message they belong to will be incomplete.",
                        missing, in_room(room, " for"), which, extra=problem("PACKETS_MISSING"))
        self.next_q[s] = max(self.next_q[s], q + 1)

        key = (s, m)
        msg = self.pending.get(key)
        if msg is None:
            if len(self.pending) >= MAX_PENDING:
                self.drop(next(iter(self.pending)), "too many incomplete messages waiting")
            msg = self.pending[key] = {"n": n, "parts": {}, "t": time.monotonic(), "bytes": 0}
            if n > 1:
                log.info("Receiving message #%d: expecting %d packets.", m, n)
        msg["parts"][i] = d
        msg["bytes"] += len(data)
        log.debug("Got packet %d of %d for message #%d (packet #%d, %d bytes).", i, n, m, q, len(data))
        if len(msg["parts"]) == msg["n"]:
            del self.pending[key]
            self.complete(key, msg)

    def wrong_room(self, room):
        """A packet for a room this receiver doesn't serve: the diode sent it to the wrong place (or the room is new).
        Dropped, so it never reaches the wrong Gotham room; counted, and logged at most once a minute per room."""
        self.stats["wrong_room"] += 1
        self.wrong[room] = self.wrong.get(room, 0) + 1
        now = time.monotonic()
        if now - self.wrong_logged.get(room, -60) >= 60:
            self.wrong_logged[room] = now
            what = f"for room {room}"
            log.warning("Got a packet %s, which this receiver doesn't serve (it serves %s), so it was dropped "
                        "(%d so far). Check the diode's routes.", what, ", ".join(self.rooms) or "no room",
                        self.wrong[room], extra=problem("ROOM_MISMATCH"))
        else:
            PROBLEMS["ROOM_MISMATCH"] = PROBLEMS.get("ROOM_MISMATCH", 0) + 1

    def room(self, s):
        return self.rooms.get(self.sess_room.get(s))

    def complete(self, key, msg):
        m, n = key[1], msg["n"]
        try:
            event = json.loads("".join(msg["parts"][k] for k in range(1, n + 1)))
        except (KeyError, json.JSONDecodeError) as e:
            self.stats["bad"] += 1
            self.done(key, lost=True)
            log.error("Message #%d had all %d packets but could not be rebuilt, so it was skipped: %r", m, n, e,
                      extra=problem("MESSAGE_BROKEN"))
            return
        self.stats["messages"] += 1
        if self.room(key[0]):
            self.room(key[0])["messages"] += 1
        self.done(key)
        result = self.on_event(event, m, key[0], self.sess_room.get(key[0]))
        packets = "1 packet" if n == 1 else f"{n} packets"
        log.info("Rebuilt message #%d%s from %s (%s bytes)%s.", m, in_room(self.sess_room.get(key[0]), " for"), packets,
                 f"{msg['bytes']:,}", f": {result}" if result else "")

    def drop(self, key, why):
        msg = self.pending.pop(key)
        self.stats["lost"] += 1
        if self.room(key[0]):
            self.room(key[0])["lost"] += 1
        self.done(key, lost=True)
        got, n = len(msg["parts"]), msg["n"]
        room = self.sess_room.get(key[0])
        log.warning("Gave up on message #%d%s: only %d of %d packets arrived (%s).", key[1], in_room(room, " for"),
                    got, n, why, extra=problem("MESSAGE_LOST"))
        if self.on_lost:
            self.on_lost(f"a message{in_room(room, ' for')} was lost on the way ({got} of {n} parts arrived)")

    def done(self, key, lost=False):
        """Remember per sender how far it got: [highest message number done, rebuilt, lost]."""
        s, m = key
        row = self.sessions.pop(s, [0, 0, 0])
        row[0] = max(row[0], m) if isinstance(m, int) else row[0]
        row[2 if lost else 1] += 1
        self.sessions[s] = row
        while len(self.sessions) > SESSIONS:
            self.sessions.popitem(last=False)
        self.save()

    def load(self):
        """The progress kept before a restart: the console can then tell that messages sent while the receiver was
        down never arrived (UDP sent to a stopped receiver is lost)."""
        if not PROGRESS:
            return
        try:
            with open(PROGRESS, encoding="utf-8") as f:
                d = json.load(f)
            self.since = int(d["since"])
            for s, row in list(d["sessions"].items())[-SESSIONS:]:
                if isinstance(s, str) and isinstance(row, list) and len(row) == 3 and all(isinstance(x, int) for x in row):
                    self.sessions[s] = row
            for s, room in (d.get("sess_room") or {}).items():
                if s in self.sessions and isinstance(room, str):
                    self.sess_room[s] = room
            for k, v in (d.get("forward") or {}).items():     # my_forward's counts: the destination box stays green
                if k in FLOW and (isinstance(v, int) or v is None):
                    FLOW[k] = v
            log.info("Kept the progress of %d sender(s) from before this start (since %s UTC).", len(self.sessions),
                     time.strftime("%Y-%m-%d %H:%M", time.gmtime(self.since / 1000)))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.warning("Could not read the progress file %s (%s); starting afresh.", PROGRESS, e, extra=problem("PROGRESS_FAILED"))

    def save(self):
        if not PROGRESS:
            return
        try:
            with open(PROGRESS + ".tmp", "w", encoding="utf-8") as f:
                json.dump({"since": self.since, "sessions": self.sessions,
                           "forward": {k: FLOW[k] for k in ("forwarded", "forward_failed", "forward_slow", "forward_at")},
                           "sess_room": {s: r for s, r in self.sess_room.items() if s in self.sessions}}, f)
            os.replace(PROGRESS + ".tmp", PROGRESS)
        except OSError as e:
            log.warning("Could not save the progress file %s (%s).", PROGRESS, e, extra=problem("PROGRESS_FAILED"))

    def expire(self):
        now = time.monotonic()
        for key in [k for k, v in self.pending.items() if now - v["t"] > TIMEOUT]:
            self.drop(key, f"waited {TIMEOUT} seconds")

    def summary(self):
        s = self.stats
        return (f"Received {s['packets']} packets and rebuilt {s['messages']} messages; {s['lost']} lost, "
                f"{s['missing_packets']} packets missing, {s['duplicates']} duplicates, {s['bad']} bad packets, "
                f"{s['wrong_room']} for rooms it doesn't serve.")


def room_row():
    return {"packets": 0, "messages": 0, "lost": 0, "missing_packets": 0, "last_at": None}


def in_room(room, word=","):
    """ ", room OPS" / " for OPS" for log lines."""
    return "" if not room else (f", room {room}" if word == "," else f"{word} {room}")


def room_of(line):
    """The room in a packet's header: CEF:0|CHAT|<ROOM>|... Anything else is not a chat packet (ValueError)."""
    f = line.split("|", 3)
    if len(f) < 4 or f[0] != "CEF:0":
        raise ValueError("not a CEF line")
    if f[1] == "CHAT":
        return f[2]
    raise ValueError(f"header {f[1]}|{f[2]} is not CHAT|<room>")


def check_rooms():
    """At start: every room name must be one the diode can route and the wire can carry. Returns a problem or None."""
    bad = [r for r in ROOMS if not ROOM_NAME.match(r)]
    if bad:
        return f"room name(s) {', '.join(map(repr, bad))} not allowed: use A-Z, 0-9, - and _ only (capitals), up to 32"
    if RESERVED & set(ROOMS):
        return "MONITOR is reserved for monitoring (monitor-receiver), not a chat room"
    if len(set(ROOMS)) != len(ROOMS):
        return "a room is listed twice in ROOMS"
    if not ROOMS:
        return "no room set: set ROOMS to the room(s) this receiver serves, e.g. ROOMS=OPS"
    return None


def open_udp(listen=LISTEN):
    """UDP socket bound to `listen`, with a large receive buffer for bursts."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
    sock.bind(listen)
    got = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    log.info("Listening for relay packets on UDP %s:%d (receive buffer %d KB).", listen[0], listen[1], got // 1024)
    return sock


# ---------- standalone: deliver every rebuilt event ----------
def post(path, payload, headers=None):
    """POST JSON to CONSOLE_URL + path. Returns None on success, or a short error text."""
    req = urllib.request.Request(CONSOLE_URL + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT) as resp:
            resp.read()
        return None
    except (urllib.error.URLError, OSError) as e:
        return str(getattr(e, "reason", e))


def deliver(event, session=None, room=None):
    """Hand one rebuilt Signal event (the original signal-cli JSON, as a dict) to its destination.

    With CONSOLE_URL set it is POSTed to the console (dev and prod); otherwise it is printed.
    Palantir Gotham gets my_forward's records, not this. Returns None on success, or a short error text.
    """
    if CONSOLE_URL:      # which route and sender run it came by, for the console's per-group delivery figures
        return post("/event", event, {"X-Relay-Session": session or "", "X-Relay-Room": room or ""})
    pretty = PRETTY == "yes" or (PRETTY == "auto" and sys.stdout.isatty())
    print(json.dumps(event, indent=2 if pretty else None, ensure_ascii=False), flush=True)
    return None


# ---------- your function: called with every rebuilt message, as a simple record ----------
def my_forward(record):
    """PLACEHOLDER: replace with your own code. Called once for every message the chat view would show.

    record -- a dict, built by chat_render.py's Chat.record() from the rebuilt Signal event, for example:
                {"id": "b2e1…:1791514700000", "time": "2026-10-16T02:58:20Z", "group": "SIGNAL TEST",
                 "group_id": "zCV6…=", "from": "Alice Nguyen", "type": "reply",
                 "text": "↪ Tho Pham \"Convoy departs…\" Copy, Alpha checked in",
                 "ref": "a1f0…:1791514641000"}
              Every record has these eight keys. time is when Signal's server received the message (UTC).
              id is "<sender uuid>:<sent timestamp>"; ref is the id of the
              message a reply, reaction, edit or delete points to, and null for every other type.
              type: text, reply, attachment, sticker, edit, delete, reaction, reaction_removed,
                    group_update, group_leave or disappearing. Receipts and typing produce no record.

    For now it appends the record to RECORD_LOG, one JSON object per line.
    It runs in the receiver's main loop: keep it quick (a few seconds at most), or hand slow work to a thread.

    Delivered or not (what the Monitor tab's destination box shows):
      - returns normally (None, True, anything but False): delivered;
      - raises an exception, or returns False: NOT delivered. Counted (FORWARD_FAILED), logged, the box turns red;
        the receiver and the console carry on. So if your sending call reports a failure by its return value rather
        than by raising, check it and raise (or return False), else a failed send counts as delivered.
    Not called at all with FORWARD_TO=off (a room that doesn't go to Gotham).
    """
    
    """ Prod only
    payload_obj = {
        "records": [record]
        }
    
    payload_str = json.dumps(payload_obj, indent=None, separators=(",", ":"))
    log.info("Sending data to MSS...")
    result = mss_stream.mss_stream(payload_str, os.environ["MSS_URL"])   # this room's stream (MSS_URL in its block);
                                                   # an error it raises (no connection, timeout) = not delivered
    status = getattr(result, "status_code", result)  # an HTTP response object: its status code
    if status is False or (type(status) is int and not 200 <= status < 300):
        raise RuntimeError(f"MSS did not accept the record (it answered {status})")   # no message content here
    """
    
    if RECORD_LOG:
        with open(RECORD_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def notify_lost(text):
    """Tell the destination a message was lost (already logged by the Reassembler)."""
    if CONSOLE_URL:
        err = post("/notice", {"text": text})
        if err:
            log.warning("Could not pass the lost-message notice on to %s: %s", CONSOLE_URL, err, extra=problem("NOTICE_FAILED"))


FLOW = dict(forwarded=0, forward_failed=0, forward_slow=0, delivered=0, deliver_failed=0,
            forward_at=None,              # when my_forward last returned without an error (ms): the Gotham box on the Monitor tab
            forward_last=None,            # how the latest hand-off went: "ok", "slow" or "failed" (the link's colour now)
            deliver_last=None)            # the latest delivery to the console: "ok" or "failed"
LOOP = {"at": time.monotonic(), "in": None, "stalled": False}   # the receiving loop's heartbeat, for the watchdog
STARTED_MS = int(time.time() * 1000)


def health(r):
    """The counters the console gets (every 30 s, and on change): the reassembly figures, what happened after
    (forwarded to my_forward, delivered to the console), per-sender progress, problems by code, and conditions."""
    out = {**r.stats, **FLOW, **({"name": RX_NAME} if RX_NAME else {}), "started": STARTED_MS,
           "forward_to": FORWARD_TO if FORWARD else "",
           "rooms": list(r.rooms), "room_stats": r.rooms, "wrong_room": dict(r.wrong),
           "sess_room": {s: room for s, room in r.sess_room.items() if s in r.sessions}, "rx_drops": rx_drops(), "p": dict(PROBLEMS),
           "sess": {s: row for s, row in r.sessions.items()}, "sess_since": r.since,
           "cond": ["RX_STALLED"] if LOOP["stalled"] else []}
    if LOOP["stalled"]:
        out["stalled_in"] = LOOP["in"] or "the receiving loop"
    if RECORD_LOG:
        try:
            u = shutil.disk_usage(os.path.dirname(os.path.abspath(RECORD_LOG)))
            out["disk_free_pct"] = round(100 * u.free / u.total, 1)
        except OSError:
            pass
    return out


def watchdog(r):
    """A second thread: if the receiving loop hasn't come round for STALL_S (stuck in my_forward, say), tell
    the console, since the loop itself can't."""
    while True:
        time.sleep(5)
        stuck = time.monotonic() - LOOP["at"] > STALL_S
        if stuck and not LOOP["stalled"]:
            LOOP["stalled"] = True
            log.error("The receiving loop has been stuck for %d s (in %s): packets are piling up and may be dropped.",
                      STALL_S, LOOP["in"] or "an unknown place", extra=problem("RX_STALLED"))
        if stuck and CONSOLE_URL:
            post("/stats", health(r))
        elif not stuck and LOOP["stalled"]:
            LOOP["stalled"] = False
            log.info("The receiving loop is running again.")
            if CONSOLE_URL:
                post("/stats", health(r))                 # say so now, not at the next 30 s report


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger().addHandler(CountProblems())
    where = CONSOLE_URL or "the terminal (stdout)"
    simple = Chat(color=False)             # builds the records; remembers recent messages, like the chat view

    def on_event(event, msgid, session=None, room=None):
        try:
            record = simple.record(event)
            if record and FORWARD:
                LOOP["in"], t0 = "my_forward", time.monotonic()
                result = my_forward(record)    # your function (see my_forward above)
                took = time.monotonic() - t0
                if result is False:
                    FLOW["forward_failed"] += 1
                    FLOW["forward_last"] = "failed"
                    log.error("my_forward reported that message #%d was not delivered to %s (it returned False). The "
                              "console still gets it.", msgid, FORWARD_TO, extra=problem("FORWARD_FAILED"))
                else:
                    FLOW["forwarded"] += 1
                    FLOW["forward_at"] = int(time.time() * 1000)
                    FLOW["forward_last"] = "ok"
                if took > FORWARD_SLOW_S:
                    FLOW["forward_slow"] += 1
                    if result is not False:
                        FLOW["forward_last"] = "slow"
                    log.warning("my_forward took %.1f s for message #%d: while it runs, nothing else is received.",
                                took, msgid, extra=problem("FORWARD_SLOW"))
        except (Exception, SystemExit):        # SystemExit too: a library calling exit() must not stop the receiver
            FLOW["forward_failed"] += 1
            FLOW["forward_last"] = "failed"
            log.exception("my_forward failed on message #%d (the console still gets it):", msgid, extra=problem("FORWARD_FAILED"))
        if FORWARD:
            r.save()                           # my_forward's counts survive a restart (the progress file)
        LOOP["in"] = "delivering to the console"
        err = deliver(event, session, room)
        LOOP["in"] = None
        if err:
            FLOW["deliver_failed"] += 1
            FLOW["deliver_last"] = "failed"
            log.error("Message #%d was rebuilt but could not be delivered to %s, so it was dropped: %s",
                      msgid, where, err, extra=problem("DELIVER_FAILED"))
            return f"{describe(event)}. NOT delivered"
        FLOW["delivered"] += 1
        FLOW["deliver_last"] = "ok"
        return f"{describe(event)}. Delivered to {where}"

    why = check_rooms()
    if why:
        log.error("Not starting: %s.", why)
        sys.exit(2)
    r = Reassembler(on_event, on_lost=notify_lost)
    served = [x for x in r.rooms if x]
    log.info("Serving room%s %s.", "s" if len(served) != 1 else "", ", ".join(served) or "(none)")
    log.info("Each message's record goes to %s (my_forward)." % FORWARD_TO if FORWARD else
             "FORWARD_TO is off: these rooms go nowhere past this receiver (my_forward is not called, no records).")
    if len(served) > 1:
        log.info("Note: this receiver serves %d rooms; my_forward's records don't say which room a message came "
                 "through (fine on dev; in prod each receiver serves one room).", len(served))
    sock = open_udp()
    sock.settimeout(1.0)
    log.info("Rebuilt messages will be delivered to %s.", where)
    if RECORD_LOG:
        log.info("Simple records of each message will be written to %s.", RECORD_LOG)
    threading.Thread(target=watchdog, args=(r,), daemon=True).start()
    last_stats, last_push = None, 0.0
    try:
        while True:
            LOOP["at"] = time.monotonic()
            try:
                data, _ = sock.recvfrom(65535)
                r.datagram(data)
            except socket.timeout:
                pass
            r.expire()
            now = time.monotonic()
            # status bar in the browser; also every 30 s unchanged, as this receiver's heartbeat on the Monitor tab
            changed = (r.stats, FLOW, PROBLEMS) != last_stats
            if CONSOLE_URL and ((changed and now - last_push >= 1) or now - last_push >= 30):
                if post("/stats", health(r)) is None:
                    last_stats = (dict(r.stats), dict(FLOW), dict(PROBLEMS))
                last_push = now
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Stopping. %s", r.summary())


def stop_now(*_):
    """docker stop / k8s send SIGTERM; stop at once (as Ctrl-C would) instead of being killed after the grace period.
    The progress file is saved after every message, so nothing of it is lost."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_now)
    main()
