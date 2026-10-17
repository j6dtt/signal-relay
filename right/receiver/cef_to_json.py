#!/usr/bin/env python3
"""Convert relay CEF/UDP datagrams back into the original Signal JSON events and deliver them.

This is the receiving end of subscriber_udp.py, without any chat display: it is the starting point for
delivering messages to Palantir. It runs on its own (container: signal-receiver).

Packet format (one CEF line per UDP datagram, see subscriber_udp.py):
    CEF:0|J6-DTT|CHAT|<i>|<n>|SIGNAL-RELAY|APP|{"s":session,"q":seq,"m":msgid,"i":i,"n":n,"d":"<json slice>"}
The "d" slices of one (session, msgid) are joined in order of i and json-decoded into the original event.

Where rebuilt events go (see deliver()):
  - PLAYBACK_URL set: HTTP POST to <url>/event (lost-message notices to /notice, counters to /stats).
    This is how the signal-receiver container feeds playback_web.py; later, deliver() sends to Palantir.
  - PLAYBACK_URL unset: printed to stdout (pretty JSON in a terminal, one JSON object per line when piped).
  - Also, every shown message becomes a simple record (chat_render.py's Chat.record: time, group, from, type,
    text) and goes to my_forward(), which for now appends it to RECORD_LOG (one JSON object per line).
Plain-English logs always go to stderr.
    python cef_to_json.py                                   # print events
    PLAYBACK_URL=http://127.0.0.1:8091 python cef_to_json.py   # feed playback_web.py
"""
import json, logging, os, socket, sys, time, urllib.error, urllib.request
from collections import OrderedDict
from comlibv3 import parse_string_in_cef
from chat_render import Chat
#import mss_stream

LISTEN      = (os.environ.get("UDP_LISTEN", "127.0.0.1"), int(os.environ.get("UDP_PORT", 6510)))  # "0.0.0.0" for diode egress
RCVBUF      = 8 * 1024 * 1024      # capped by sysctl net.core.rmem_max
TAG         = "SIGNAL-RELAY"       # CEF Name field expected from the sender
TIMEOUT     = 30                   # seconds to wait for all parts of one message
MAX_PENDING = 1000                 # incomplete messages held at once
MAX_PARTS   = 2000                 # reject absurd part counts
SEEN_MAX    = 20000                # recent (session, seq) kept for duplicate detection
PRETTY      = os.environ.get("PRETTY", "auto")   # "yes", "no", or "auto" (pretty only in a terminal)
PLAYBACK_URL = os.environ.get("PLAYBACK_URL", "").rstrip("/")  # e.g. http://playback:8091; empty = print to stdout
POST_TIMEOUT = 5                  # seconds per HTTP delivery
RECORD_LOG  = os.environ.get("RECORD_LOG", "")   # where my_forward() appends simple records; empty = off

log = logging.getLogger("cef_to_json")


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
        self.stats = dict(packets=0, messages=0, lost=0, duplicates=0, bad=0, missing_packets=0)

    def datagram(self, data):
        self.stats["packets"] += 1
        try:
            parts, tag = parse_string_in_cef(data.decode("ascii"))
            p = parts[0]
            s, q, m, i, n, d = p["s"], p["q"], p["m"], p["i"], p["n"], p["d"]
            if tag != TAG or not (1 <= i <= n <= MAX_PARTS):
                raise ValueError(f"tag={tag} part {i} of {n}")
        except (UnicodeDecodeError, IndexError, KeyError, TypeError, ValueError) as e:
            self.stats["bad"] += 1
            log.warning("Received a packet (%d bytes) that is not a valid relay packet, so it was ignored: %r",
                        len(data), e)
            return

        if (s, q) in self.seen:                       # COPIES > 1, or network duplicate
            self.stats["duplicates"] += 1
            log.debug("Ignored a duplicate of packet #%d.", q)
            return
        self.seen[(s, q)] = None
        if len(self.seen) > SEEN_MAX:
            self.seen.popitem(last=False)

        if s not in self.next_q:
            log.info("A sender started (session %s). Its first packet is #%d.", s, q)
            self.next_q[s] = q
        if q > self.next_q[s]:
            missing = q - self.next_q[s]
            self.stats["missing_packets"] += missing
            which = f"#{self.next_q[s]}" if missing == 1 else f"#{self.next_q[s]} to #{q - 1}"
            log.warning("%d packet(s) never arrived (%s). Any message they belong to will be incomplete.",
                        missing, which)
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

    def complete(self, key, msg):
        m, n = key[1], msg["n"]
        try:
            event = json.loads("".join(msg["parts"][k] for k in range(1, n + 1)))
        except (KeyError, json.JSONDecodeError) as e:
            self.stats["bad"] += 1
            log.error("Message #%d had all %d packets but could not be rebuilt, so it was skipped: %r", m, n, e)
            return
        self.stats["messages"] += 1
        result = self.on_event(event, m)
        packets = "1 packet" if n == 1 else f"{n} packets"
        log.info("Rebuilt message #%d from %s (%s bytes)%s.", m, packets, f"{msg['bytes']:,}",
                 f": {result}" if result else "")

    def drop(self, key, why):
        msg = self.pending.pop(key)
        self.stats["lost"] += 1
        got, n = len(msg["parts"]), msg["n"]
        log.warning("Gave up on message #%d: only %d of %d packets arrived (%s).", key[1], got, n, why)
        if self.on_lost:
            self.on_lost(f"a message was lost on the way ({got} of {n} parts arrived)")

    def expire(self):
        now = time.monotonic()
        for key in [k for k, v in self.pending.items() if now - v["t"] > TIMEOUT]:
            self.drop(key, f"waited {TIMEOUT} seconds")

    def summary(self):
        s = self.stats
        return (f"Received {s['packets']} packets and rebuilt {s['messages']} messages; {s['lost']} lost, "
                f"{s['missing_packets']} packets missing, {s['duplicates']} duplicates, {s['bad']} bad packets.")


def open_udp(listen=LISTEN):
    """UDP socket bound to `listen`, with a large receive buffer for bursts."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF)
    sock.bind(listen)
    got = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    log.info("Listening for relay packets on UDP %s:%d (receive buffer %d KB).", listen[0], listen[1], got // 1024)
    return sock


# ---------- standalone: deliver every rebuilt event ----------
def post(path, payload):
    """POST JSON to PLAYBACK_URL + path. Returns None on success, or a short error text."""
    req = urllib.request.Request(PLAYBACK_URL + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT) as resp:
            resp.read()
        return None
    except (urllib.error.URLError, OSError) as e:
        return str(getattr(e, "reason", e))


def deliver(event):
    """Hand one rebuilt Signal event (the original signal-cli JSON, as a dict) to its destination.

    With PLAYBACK_URL set it is POSTed to playback (dev and prod); otherwise it is printed.
    Palantir Gotham gets my_forward's records, not this. Returns None on success, or a short error text.
    """
    if PLAYBACK_URL:
        return post("/event", event)
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
    An error raised here is logged and doesn't stop the receiver or playback.
    """
    
    """ Prod only
    payload_obj = {
        "records": [record]
        }
    
    payload_str = json.dumps(payload_obj, indent=None, separators=(",", ":"))
    log.info("Sending data to MSS...")
    mss_stream.mss_stream(payload_str)
    """
    
    if RECORD_LOG:
        with open(RECORD_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def notify_lost(text):
    """Tell the destination a message was lost (already logged by the Reassembler)."""
    if PLAYBACK_URL:
        err = post("/notice", {"text": text})
        if err:
            log.warning("Could not pass the lost-message notice on to %s: %s", PLAYBACK_URL, err)


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    where = PLAYBACK_URL or "the terminal (stdout)"
    simple = Chat(color=False)             # builds the records; remembers recent messages, like the chat view

    def on_event(event, msgid):
        try:
            record = simple.record(event)
            if record:
                my_forward(record)             # your function (see my_forward above)
        except Exception:
            log.exception("my_forward failed on message #%d (playback still gets it):", msgid)
        err = deliver(event)
        if err:
            log.error("Message #%d was rebuilt but could not be delivered to %s, so it was dropped: %s",
                      msgid, where, err)
            return f"{describe(event)}. NOT delivered"
        return f"{describe(event)}. Delivered to {where}"

    r = Reassembler(on_event, on_lost=notify_lost)
    sock = open_udp()
    sock.settimeout(1.0)
    log.info("Rebuilt messages will be delivered to %s.", where)
    if RECORD_LOG:
        log.info("Simple records of each message will be written to %s.", RECORD_LOG)
    last_stats, last_push = None, 0.0
    try:
        while True:
            try:
                data, _ = sock.recvfrom(65535)
                r.datagram(data)
            except socket.timeout:
                pass
            r.expire()
            now = time.monotonic()
            if PLAYBACK_URL and r.stats != last_stats and now - last_push >= 1:   # status bar in the browser
                if post("/stats", r.stats) is None:
                    last_stats = dict(r.stats)
                last_push = now
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Stopping. %s", r.summary())


if __name__ == "__main__":
    main()
