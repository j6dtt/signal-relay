#!/usr/bin/env python3
"""Subscribe to signal-cli and stream groups' messages as CEF over UDP, one stream per room (one-way, diode-safe).

Where messages come from (SIGNAL_MODE):
  - "signal-cli" (default): the in-house signal-cli image's built-in HTTP service, at SIGNAL_URL. Events stream
    from /api/v1/events (Server-Sent Events); after a reconnect, Last-Event-ID resends what was missed (signal-cli
    keeps the last 1000 events). Names come from the JSON-RPC listContacts command.
    With an https:// SIGNAL_URL, the connection goes through signal-cli's TLS gateway: TLS 1.3, this script shows
    its client certificate (TLS_CERT/TLS_KEY) and trusts only our own CA (TLS_CA), not public ones.
    The ID of the last handled event is saved in STATE_FILE, so a restart, or a move to another host (copy the
    file along), resumes where it stopped instead of missing messages.
  - "rest-api": the old bbernhard/signal-cli-rest-api image (websocket /v1/receive, REST /v1/contacts).
    Kept for rollback only.

Which groups go where: ROUTES_FILE (routes.conf, the same file on every low-side host) lists, per subscriber, each
room and its groups; this subscriber reads the lines of ROUTES_FOR (its name there). A room is a diode route: the
diode sends each room to its receiver (in prod, one receiver and one Gotham room per room). The file is checked at
start and the subscriber refuses to start on any problem (bad room name, MONITOR, bad group ID, no lines for it,
the same group in the same room twice on any subscriber), so it never relays a chat by accident.

Each Signal event (raw signal-cli JSON) is sent once per room its group goes to, split into parts so every CEF line
fits one UDP datagram. Every part is one datagram carrying one CEF line, built with comlibv3.string_to_cef and sent
with comlibv3.send_events_over_udp (one call per line, so PACE applies between datagrams):
    CEF:0|CHAT|<ROOM>|<i>|<n>|SIGNAL-RELAY|APP|{"s":session,"q":seq,"m":msgid,"i":i,"n":n,"d":"<json slice>"}
Every room has its own session (new each run) and its own packet and message numbers, so a receiver sees no gaps
from other rooms. The receiver (cef_to_json.py) joins the "d" slices of one msgid in order and json-decodes the
result. In prod the data diode sits between this script and the receivers.

The event is forwarded as signal-cli sent it, with display names (looked up from the API) added:
  - every mention: "name" (which signal-cli fills with the number or uuid) is set to the display name
  - every quote:   "authorName" is added (signal-cli only gives the author's number and uuid)
The receiving side cannot ask the API, so this is the only place names can come from. If a lookup fails,
that field is left as signal-cli sent it.

With MONITOR_ADDR set (host:port of this host's signal-monitor), it also sends a small status packet there every
STATUS_EVERY seconds: connected or not, counts, queue, when each worker last did something, problems counted by
fixed code (every warning and error is counted: the catch-all), events missed (from signal-cli's event IDs), and
the last message number sent, so the right side can tell whether it arrived. Codes and figures only, never log
text or message content. signal-monitor names it by its container name, and finds its signal-cli by SIGNAL_URL's
host: no setting needed. Overrides, for a subscriber on another host than its signal-monitor: MONITOR_NAME (this
subscriber's name) and RELAY_ID (its signal-cli's name, as on the Monitor tab).

    python cef_to_json.py         # then:  python subscriber_udp.py
"""
import asyncio, json, logging, os, re, signal, socket, ssl, time, urllib.parse, urllib.request, websockets
from comlibv3 import string_to_cef, send_events_over_udp

NUM     = "+19415499805"            # your Signal number (the account the signal-cli daemon runs as)
ROUTES_FILE = os.environ.get("ROUTES_FILE", "/routes.conf")      # which groups go to which room (see read_routes)
ROUTES_FOR  = os.environ.get("ROUTES_FOR", "")                  # this subscriber's name in that file; required
ROOM_NAME = re.compile(r"^[A-Z0-9_-]{1,32}$")                     # the diode's rules need capitals; safe in a header
GROUP_ID  = re.compile(r"^[A-Za-z0-9+/]{43}=$")                   # a Signal group ID: 32 bytes, base64
RESERVED  = {"MONITOR"}                                          # CEF:0|CHAT|MONITOR| is the monitoring stream
API     = os.environ.get("SIGNAL_API", "127.0.0.1:8080")                  # rest-api mode only (host:port)
URL     = os.environ.get("SIGNAL_URL") or f"http://{API}"                 # signal-cli: https://host:port via the TLS gateway
TLS     = {k: os.environ.get(f"TLS_{k.upper()}", f"/certs/{f}")            # used when URL is https://
           for k, f in (("ca", "ca.crt"), ("cert", "client.crt"), ("key", "client.key"))}
STATE   = os.environ.get("STATE_FILE", "signal-last-event-id") or None    # last handled event ID; "" to disable
MODE    = os.environ.get("SIGNAL_MODE", "signal-cli")                     # "signal-cli" or "rest-api" (old image)
DEST    = (os.environ.get("UDP_DEST", "127.0.0.1"), int(os.environ.get("UDP_PORT", 6510)))  # cef_to_json.py / diode ingress
HEADER  = "CEF:0|CHAT|{room}|{{}}|{{}}|SIGNAL-RELAY|APP|"   # per room; then {}/{} = part i / part count
MAX_DGRAM = 1472                    # MTU 1500 - 20 IP - 8 UDP; whole CEF line must fit
PACE    = 0.002                     # seconds between datagrams (smooths bursts for the receiver)
COPIES  = 1                         # send each datagram N times; receiver drops repeats
LOG     = os.environ.get("SENT_LOG", "signal-udp-sent.jsonl") or None  # {"s","m","event"} per event, for manual recovery; "" to disable
NAMES_REFRESH = 60                  # seconds; reload the name list at most this often, when an unknown person is mentioned
PROFILE_RETRY = 600                 # seconds before asking Signal again for the same unnamed person's profile
MONITOR = os.environ.get("MONITOR_ADDR", "")                     # signal-monitor's host:port (UDP); "" = no status
RELAY_ID = os.environ.get("RELAY_ID", "")                       # override: its signal-cli's name ("" = signal-monitor finds it)
MONITOR_NAME = os.environ.get("MONITOR_NAME", "")               # override: its own name ("" = its container name)
STATUS_EVERY = 10                   # seconds between status packets

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("subscriber")

# What the status packets report (see MONITOR_ADDR). Updated as things happen; read by status_sender().
STATUS = {"stream": "starting", "stream_error": "", "stream_code": "", "last_event": None, "sent": 0, "ignored": 0,
          "errors": 0, "warnings": 0, "p": {}, "started": time.time(),
          "rx_at": None,          # when the event stream last gave us anything (an event or a keep-alive line)
          "dropped_at": None,     # when the event stream was last lost (we were in touch with signal-cli until then)
          "tx_at": time.time(),   # when the sender last finished a message or went idle
          "read": 0,              # events read from signal-cli (relayed + ignored)
          "lw": None}             # the last "may be lost" window [from, to] (ms) after a signal-cli restart
SEND_FAIL = ("An error occurred while sending data", "Socket timeout occurred")


class CountProblems(logging.Handler):
    """Counts every warning and error by a fixed code, for the status packets: the catch-all.
    A log call names its code with extra={"code": ...} (and "n" when one line stands for several). comlibv3 logs
    to the root logger: its UDP send errors count as SEND_FAILED, its CEF errors are already counted by the caller
    (MESSAGE_SKIPPED). Anything else is OTHER (errors) or OTHER_WARNING. Only codes and counts leave this
    container, never the log text: it can hold pieces of a message."""
    def emit(self, record):
        if record.levelno < logging.WARNING:
            return
        code = getattr(record, "code", None)
        if code is None and record.name == "root":
            if str(record.msg).startswith(SEND_FAIL):
                code = "SEND_FAILED"
            elif str(record.msg).startswith("Error converting data to CEF"):
                return
        code = code or ("OTHER" if record.levelno >= logging.ERROR else "OTHER_WARNING")
        STATUS["p"][code] = STATUS["p"].get(code, 0) + getattr(record, "n", 1)
        STATUS["errors" if record.levelno >= logging.ERROR else "warnings"] += 1


logging.getLogger().addHandler(CountProblems())        # the root logger: ours and comlibv3's lines


def problem(code, n=1):
    """extra= for a log call: its fixed code (and how many problems the one line stands for)."""
    return {"code": code, "n": n}

def tls_context():
    """TLS 1.3 to signal-cli's gateway: our client certificate, and only our CA trusted. None for plain http."""
    if not URL.startswith("https://"):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)          # checks the certificate and the host name
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_verify_locations(TLS["ca"])
    ctx.load_cert_chain(TLS["cert"], TLS["key"])
    return ctx


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return f.read().strip() or None
    except (TypeError, FileNotFoundError):
        return None


def save_state(event_id):
    """Write the last handled event ID atomically (a crash never leaves a half-written file)."""
    if STATE and event_id:
        with open(STATE + ".tmp", "w", encoding="utf-8") as f:
            f.write(event_id)
        os.replace(STATE + ".tmp", STATE)


SSL = tls_context()
WIDE    = 99999                     # worst-case width used when sizing parts (i, n, msgid)


def line_len(i, n, probe, header):
    """Exact byte length of one CEF line for this part."""
    return len(header.format(i, n)) + len(string_to_cef([probe], ""))


def split(raw, msgid, seq0, sess, header):
    """Cut raw JSON into the largest slices whose CEF line fits MAX_DGRAM. Returns part dicts."""
    slices, pos = [], 0
    while pos < len(raw):
        lo, hi = 1, min(len(raw) - pos, MAX_DGRAM)
        while lo < hi:                                  # largest slice that fits (binary search)
            mid = (lo + hi + 1) // 2
            probe = {"s": sess, "q": seq0 + WIDE, "m": WIDE, "i": WIDE, "n": WIDE,
                     "d": raw[pos:pos + mid]}
            if line_len(WIDE, WIDE, probe, header) <= MAX_DGRAM:
                lo = mid
            else:
                hi = mid - 1
        slices.append(raw[pos:pos + lo])
        pos += lo
    n = len(slices)
    return [{"s": sess, "q": seq0 + k, "m": msgid, "i": k + 1, "n": n, "d": d}
            for k, d in enumerate(slices)]


def read_routes(path, me):
    """routes.conf -> ({group ID: [rooms]} for subscriber `me`, None), or (None, what is wrong).

    One route per line: subscriber, room, groups (comma-separated). A line starting with a space or tab continues
    the groups of the line above. "#" starts a comment. Checked for every subscriber in the file, not only `me`:
    the same group in the same room twice would deliver every message twice."""
    if not me:
        return None, "ROUTES_FOR is not set (this subscriber's name in the routes file)"
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return None, f"can't read the routes file {path} ({e.strerror or e})"
    rows = []                                           # [line number, subscriber, room, [groups]]
    for n, line in enumerate(lines, 1):
        text = line.split("#", 1)[0].rstrip()
        if not text.strip():
            continue
        if line[:1] in (" ", "\t"):
            if not rows:
                return None, f"line {n}: a continued group list before any route"
            rows[-1][3] += [g.strip() for g in text.split(",") if g.strip()]
            continue
        cols = text.split(None, 2)
        if len(cols) < 3:
            return None, f"line {n}: expected 3 columns (subscriber, room, groups)"
        rows.append([n, cols[0], cols[1], [g.strip() for g in cols[2].split(",") if g.strip()]])
    seen = {}                                           # (room, group) -> (line, subscriber)
    for n, sub, room, groups in rows:
        if room in RESERVED:
            return None, f"line {n}: {room} is reserved for monitoring, not a chat room"
        if not ROOM_NAME.match(room):
            return None, f"line {n}: room {room!r} not allowed: use A-Z, 0-9, - and _ only (capitals), up to 32"
        if not groups:
            return None, f"line {n}: room {room} has no groups"
        for g in groups:
            if not GROUP_ID.match(g):
                return None, f"line {n}: {g[:12]!r}… is not a group ID (44 characters, ending in =)"
            if (room, g) in seen:
                m, other = seen[(room, g)]
                return None, (f"line {n}: group {g[:8]}… already goes to {room} on line {m} ({other}): every message "
                              "would arrive twice")
            seen[(room, g)] = (n, sub)
    routes = {}
    for n, sub, room, groups in rows:
        if sub == me:
            for g in groups:
                routes.setdefault(g, []).append(room)
    if not routes:
        return None, f"no lines for {me} in {path} (the first column must be this subscriber's ROUTES_FOR)"
    return routes, None


def describe(event):
    """Short plain-English description of an event, e.g. 'reaction from Tho Pham'."""
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


class Names:
    """Display names for mentions and quotes, from everyone this account knows (all recipients, not only
    saved contacts): JSON-RPC listContacts with allRecipients, or /v1/contacts?all_recipients=true (rest-api)."""
    def __init__(self):
        self.names = {}                 # uuid / number -> display name
        self.loaded = 0.0               # monotonic time of the last load attempt
        self.asked = {}                 # uuid / number -> monotonic time its profile was last refreshed

    @staticmethod
    def display(c):
        """The name Signal would show: nickname, then contact name, then profile name, then username.
        Handles both signal-cli's field names (camelCase) and the rest-api's (snake_case)."""
        p = c.get("profile") or {}
        nick = c.get("nickname") if isinstance(c.get("nickname"), dict) else {}
        joined = lambda *xs: " ".join(x for x in xs if x)
        return (nick.get("name") or c.get("nickName") or joined(c.get("nickGivenName"), c.get("nickFamilyName"))
                or c.get("name") or joined(c.get("givenName"), c.get("familyName"))
                or joined(p.get("givenName") or p.get("given_name"), p.get("familyName") or p.get("lastname"))
                or c.get("profile_name") or c.get("username") or None)

    def _fetch(self, recipient=None):
        """Everyone this account knows; or, with recipient (uuid or number), just that person, after signal-cli
        has refreshed their profile from Signal's servers (listContacts does that when given a recipient)."""
        if MODE == "rest-api":
            url = f"http://{API}/v1/contacts/{urllib.parse.quote(NUM)}?all_recipients=true"
            with urllib.request.urlopen(url, timeout=5) as resp:
                return json.load(resp)
        params = {"recipient": [recipient]} if recipient else {"allRecipients": True}
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "listContacts", "params": params}).encode()
        req = urllib.request.Request(f"{URL}/api/v1/rpc", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15, context=SSL) as resp:
            reply = json.load(resp)
        if "error" in reply:
            raise RuntimeError(reply["error"].get("message", reply["error"]))
        return reply["result"]

    async def load(self):
        self.loaded = time.monotonic()
        try:
            people = await asyncio.to_thread(self._fetch)
        except Exception as e:
            log.warning("Could not load display names from the Signal API, so mentions may show numbers: %r", e,
                        extra=problem("NAMES_FAILED"))
            return
        names = {}
        for c in people:
            name = self.display(c)
            for key in (c.get("uuid"), c.get("number")):
                if name and key:
                    names[key] = name
        self.names = names
        log.info("Loaded display names for %d people from the Signal API.", len(people))

    async def refresh(self, uuid, number):
        """Ask signal-cli to fetch one unnamed person's profile from Signal (a new account has no contacts and
        hasn't fetched group members' profiles yet). At most once per PROFILE_RETRY per person. Returns the name."""
        key = uuid or number
        if MODE == "rest-api" or not key or time.monotonic() - self.asked.get(key, -PROFILE_RETRY) < PROFILE_RETRY:
            return None
        self.asked[key] = time.monotonic()
        try:
            people = await asyncio.to_thread(self._fetch, key)
        except Exception as e:
            log.warning("Could not fetch the Signal profile of %s, so they stay unnamed for now: %r", key, e,
                        extra=problem("NAMES_FAILED"))
            return None
        name = next((self.display(c) for c in people if self.display(c)), None)
        if name:
            for k in (uuid, number):
                if k:
                    self.names[k] = name
            log.info("Fetched the Signal profile of %s: their display name is now known.", key)
        else:
            log.info("Fetched the Signal profile of %s, but it has no name. Will try again in %d minutes.",
                     key, PROFILE_RETRY // 60)
        return name

    def lookup(self, uuid, number):
        return self.names.get(uuid) or self.names.get(number)

    async def fill(self, event):
        """Add display names to every mention and quote in the event (data, edit, sync).
        Returns (mentions named, quotes named)."""
        mentions, quotes = [], []
        def walk(x):
            if isinstance(x, dict):
                if isinstance(x.get("mentions"), list):
                    mentions.extend(m for m in x["mentions"] if isinstance(m, dict))
                if isinstance(x.get("quote"), dict):
                    quotes.append(x["quote"])
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(event)
        people = [(m, m.get("uuid"), m.get("number")) for m in mentions] + \
                 [(q, q.get("authorUuid"), q.get("authorNumber") or q.get("author")) for q in quotes]
        if not people:
            return 0, 0
        if any(not self.lookup(u, n) for _, u, n in people) and time.monotonic() - self.loaded > NAMES_REFRESH:
            await self.load()               # someone new: reload once, then use what we have
        for _, uuid, number in people:      # still unnamed: fetch that person's profile (new accounts need this)
            if not self.lookup(uuid, number):
                await self.refresh(uuid, number)
        named = [0, 0]
        for k, (obj, uuid, number) in enumerate(people):
            name = self.lookup(uuid, number)
            is_mention = k < len(mentions)
            if not name:
                log.warning("No display name found for a %s (%s); sent as is.",
                            "mentioned person" if is_mention else "quoted author", number or uuid,
                            extra=problem("NAME_MISSING"))
                continue
            obj["name" if is_mention else "authorName"] = name
            named[0 if is_mention else 1] += 1
        return tuple(named)


class Files:
    """The sent log and the saved stream position. A write that fails (disk full, permissions) is logged as
    WRITE_FAILED and relaying carries on: losing the position only means resending after a restart."""
    def __init__(self):
        self.sent = None
        if LOG:
            try:
                self.sent = open(LOG, "a", encoding="utf-8")
            except OSError as e:
                log.error("Can't open the sent log %s (%s), so no copy of sent messages is kept.", LOG, e,
                          extra=problem("WRITE_FAILED"))

    def log_sent(self, st, room, event):
        if self.sent:
            msgid = st["m"]
            try:
                self.sent.write(json.dumps({"s": st["s"], "m": msgid, "room": room, "event": event}) + "\n")
                self.sent.flush()
            except OSError as e:
                log.error("Could not write message #%d to the sent log %s (%s).", msgid, LOG, e,
                          extra=problem("WRITE_FAILED"))

    def save(self, event_id):
        try:
            save_state(event_id)
        except OSError as e:
            log.error("Could not save the stream position in %s (%s): after a restart, messages may be sent again.",
                      STATE, e, extra=problem("WRITE_FAILED"))


async def sender(queue, names):
    """Drain the queue: encode, split, and send datagrams at a steady pace, once per room the message's group goes to,
    each room with its own numbering (STREAMS). One message that fails in a way not foreseen is logged
    (SENDER_ERROR) and skipped, so the sender itself never stops."""
    files = Files()
    while True:
        STATUS["tx_at"] = time.time()                               # idle (waiting) counts as alive
        raw, event_id, _, gid = await queue.get()
        try:
            if raw is None:                                         # an ignored event: only remember its ID
                files.save(event_id)
                continue
            try:
                event = json.loads(raw)
                mentions, quotes = await names.fill(event)
                raw = json.dumps(event, separators=(",", ":"))      # compact, ASCII-only
            except (Exception, SystemExit) as e:
                log.error("Could not prepare a message (%r), so it was skipped.", e, extra=problem("MESSAGE_SKIPPED"))
                files.save(event_id)
                continue
            STATUS["sent"] += 1
            for room in ROUTES.get(gid, []):
                if await send_to(room, raw, event, mentions, quotes, files, queue):
                    g = STREAMS[room]["g"].setdefault(gid, [0, None])   # per group, for its window on the high side
                    g[0], g[1] = g[0] + 1, time.time()
            files.save(event_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Sending a message failed unexpectedly, so it was skipped: %r", e, extra=problem("SENDER_ERROR"))


async def send_to(room, raw, event, mentions, quotes, files, queue):
    """One message on one room's stream: its next message number, its packet numbers, its header."""
    st = STREAMS[room]
    msgid = st["m"] + 1
    try:
        parts = split(raw, msgid, st["q"], st["s"], st["header"])
        lines = string_to_cef(parts, st["header"]).split("\n")       # safe: escaped JSON has no \n
    except (Exception, SystemExit) as e:                            # comlibv3 calls exit() on error
        log.error("Could not convert message #%d for %s to CEF, so it was skipped: %r", msgid, room, e,
                  extra=problem("MESSAGE_SKIPPED"))
        return False
    st["m"], st["q"] = msgid, st["q"] + len(parts)
    seq = st["q"]
    files.log_sent(st, room, event)
    for line in lines:
        if len(line) > MAX_DGRAM:                                   # ASCII, so chars == bytes; should never happen
            log.warning("A packet for message #%d to %s is %d bytes, over the %d-byte limit. "
                        "Sent anyway, but the network may drop it.", msgid, room, len(line), MAX_DGRAM,
                        extra=problem("PACKET_TOO_BIG"))
        for _ in range(COPIES):
            send_events_over_udp(line, *DEST)                       # one line = one datagram; logs its own errors
            await asyncio.sleep(PACE)                               # one call per line keeps the pacing
    st["m_at"] = time.time()                                        # for the high side's "did it arrive?" check
    n = len(parts)
    packets = f"1 packet (#{seq - 1})" if n == 1 else f"{n} packets (#{seq - n} to #{seq - 1})"
    waiting = f" {queue.qsize()} more waiting to be sent." if queue.qsize() else ""
    named = "".join(f", {k} {what} named" for k, what in ((mentions, "mention(s)"), (quotes, "quote author")) if k)
    log.info("Sent message #%d to %s (%s%s, %s bytes) as %s.%s",
             msgid, room, describe(event), named, f"{len(raw):,}", packets, waiting)
    return True


class EventIds:
    """Counts events this subscriber never got, from signal-cli's event IDs.

    signal-cli numbers its events "<its start time, ms>-<n>", n going up by one with no gaps (SseEventBuffer).
      - a jump in n (812 then 815): events 813-814 never reached us (EVENTS_MISSED)
      - a new start time: signal-cli restarted. Events it had received since we last heard from it were in its
        memory only (it already confirmed them to Signal), so they are lost if it restarted while we were away.
        We know when we lost touch and when it started again: if that gap is more than RESTART_S, the messages
        it received in between may be lost (CLI_RESTART_LOSS, with the time window). Its first event after a
        restart should be n=1; a higher n means events of the new run were dropped before we read them.
    lost_at is when we were last in touch with signal-cli's run (None while connected). After our own restart it is
    the saved position file's time: refreshed on a clean stop, so a crash only makes the window wider."""
    RESTART_S = 20                       # a signal-cli restart takes about this long: shorter gaps lose nothing

    def __init__(self, saved_id):
        self.run, self.n = self.parse(saved_id)
        try:
            self.lost_at = int(os.path.getmtime(STATE) * 1000) if STATE and saved_id else None
        except OSError:
            self.lost_at = None

    @staticmethod
    def parse(event_id):
        run, _, n = (event_id or "").rpartition("-")
        return (int(run), int(n)) if run.isdigit() and n.isdigit() else (None, None)

    def connected(self):
        pass                             # lost_at stays until the first event shows which run we are talking to

    def disconnected(self):
        if self.lost_at is None:
            self.lost_at = int(time.time() * 1000)

    def seen(self, event_id):
        run, n = self.parse(event_id)
        if run is None:
            return
        STATUS["read"] += 1
        if self.run is not None and run == self.run and n > self.n + 1:
            missed = n - self.n - 1
            log.warning("%d event(s) from signal-cli never reached this subscriber (event %d-%d to %d): they are lost.",
                        missed, run, self.n + 1, n - 1, extra=problem("EVENTS_MISSED", missed))
        elif self.run is not None and run != self.run:
            if self.lost_at and run - self.lost_at > self.RESTART_S * 1000:
                STATUS["lw"] = [self.lost_at, run]
                log.warning("signal-cli restarted while this subscriber was not connected to it: messages it received "
                            "between %s and %s UTC (up to its restart) were in its memory only and may be lost.",
                            utc(self.lost_at), utc(run), extra=problem("CLI_RESTART_LOSS"))
            else:
                log.info("signal-cli restarted (its new run started %s UTC). Nothing was missed: it restarted "
                         "while this subscriber was connected.", utc(run))
            if n > 1:
                log.warning("signal-cli's new run had dropped %d event(s) before this subscriber read them.", n - 1,
                            extra=problem("EVENTS_MISSED", n - 1))
        self.run, self.n, self.lost_at = run, n, None


def utc(ms):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000))


STALL_S = 45                         # signal-cli sends a keep-alive every 15 s: this long with nothing = stuck


async def receive_signal_cli():
    """Yield (event, event ID) for each incoming event ({"envelope", "account"}) from signal-cli's /api/v1/events.
    Reconnects on its own; Last-Event-ID makes signal-cli resend events missed while disconnected. It starts from
    the saved ID: if signal-cli has restarted since, it doesn't know that ID and resends all it has kept."""
    import aiohttp
    url, last_id = f"{URL}/api/v1/events", load_state()
    ids = EventIds(last_id)
    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=STALL_S)     # a stuck stream raises a timeout
    async with aiohttp.ClientSession(timeout=timeout, read_bufsize=2 ** 22) as http:   # big events fit one line
        while True:
            headers = {"Accept": "text/event-stream"}
            if last_id:
                headers["Last-Event-ID"] = last_id
            try:
                async with http.get(url, headers=headers, ssl=SSL if SSL else True) as resp:
                    resp.raise_for_status()
                    STATUS["stream"], STATUS["stream_error"], STATUS["stream_code"] = "connected", "", ""
                    log.info("Connected to signal-cli at %s. Waiting for messages%s.", url,
                             " (asked for anything missed since the last one)" if last_id else "")
                    data, event_id = [], None
                    async for raw_line in resp.content:                # one SSE line at a time
                        STATUS["rx_at"] = time.time()                  # events and keep-alives alike
                        line = raw_line.decode("utf-8").rstrip("\r\n")
                        if line:
                            field, _, value = line.partition(":")
                            value = value[1:] if value.startswith(" ") else value
                            if field == "data":
                                data.append(value)
                            elif field == "id":
                                event_id = value
                            continue
                        if data:                                       # a blank line ends one event
                            obj = json.loads("\n".join(data))
                            p = obj.get("params", obj) if isinstance(obj, dict) else {}
                            ids.seen(event_id)
                            yield (p.get("result", p) if isinstance(p, dict) else {}), event_id
                        if event_id:
                            last_id = event_id
                        data, event_id = [], None
                STATUS["stream"], STATUS["stream_error"], STATUS["stream_code"] = "disconnected", "signal-cli closed the event stream", "STREAM_CLOSED"
                log.warning("signal-cli closed the event stream. Reconnecting in 3 seconds...", extra=problem("STREAM_DROP"))
            except ssl.SSLError as e:
                STATUS["stream"], STATUS["stream_error"], STATUS["stream_code"] = "disconnected", f"secure connection failed: {e}", "TLS_FAILED"
                log.error("The secure connection to signal-cli failed (%s). Check the certificates. Retrying in 3 seconds...", e,
                          extra=problem("STREAM_DROP"))
            except aiohttp.SocketTimeoutError:              # read timeout only (a connect timeout is another class)
                STATUS["stream"], STATUS["stream_error"], STATUS["stream_code"] = "disconnected", f"no data for {STALL_S} s", "STALLED"
                log.warning("The event stream from signal-cli was stuck: nothing for %d s, not even its keep-alives. "
                            "Reconnecting in 3 seconds...", STALL_S, extra=problem("STREAM_STALLED"))
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
                STATUS["stream"], STATUS["stream_error"], STATUS["stream_code"] = "disconnected", f"lost the connection: {e}", stream_code(e)
                log.warning("Lost the connection to signal-cli (%s). Reconnecting in 3 seconds...", e, extra=problem("STREAM_DROP"))
            ids.disconnected()
            STATUS["dropped_at"] = time.time()
            await asyncio.sleep(3)


def stream_code(e):
    """A fixed code for why the event stream dropped, for the Monitor tab's likely-cause rules."""
    import aiohttp                             # imported where it's used, as in receive_signal_cli (else NameError: crash)
    if isinstance(e, aiohttp.ClientResponseError):
        return "HTTP_ERROR"                    # the gateway answered, but not 200 (e.g. a path it doesn't allow)
    if isinstance(e, aiohttp.ServerDisconnectedError):
        return "GATEWAY_CLOSED"                # connected, then closed: often a certificate name not in TLS_CLIENT_NAMES
    if isinstance(e, aiohttp.ClientSSLError):
        return "TLS_FAILED"                    # certificate problems aiohttp reports as its own error type
    if isinstance(e, aiohttp.ClientConnectorError):
        return "UNREACHABLE"                   # refused, no route, or the name doesn't resolve: signal-cli not running?
    if isinstance(e, asyncio.TimeoutError):
        return "TIMEOUT"
    return "LOST"


async def receive_rest_api():
    """Yield each incoming event from the old signal-cli-rest-api websocket (rollback only)."""
    url = f"ws://{API}/v1/receive/{NUM}"
    async for ws in websockets.connect(url, ping_interval=30):
        STATUS["stream"], STATUS["stream_error"] = "connected", ""
        log.info("Connected to the Signal API at %s. Waiting for messages.", url)
        try:
            async for raw in ws:
                yield json.loads(raw), None
        except websockets.ConnectionClosed:
            STATUS["stream"], STATUS["stream_error"] = "disconnected", "lost the connection to the Signal API"
            log.warning("Lost the connection to the Signal API. Reconnecting...", extra=problem("STREAM_DROP"))


def cert_days():
    """Days until this subscriber's client certificate expires, or None."""
    try:
        return int((ssl.cert_time_to_seconds(ssl._ssl._test_decode_cert(TLS["cert"])["notAfter"]) - time.time()) // 86400)
    except Exception:
        return None


def age(t):
    return int(time.time() - t) if t else None


async def status_sender(queue, tasks):
    """Every STATUS_EVERY seconds: one small JSON packet to signal-monitor (this host's network; not the diode).
    Codes and figures only: "p" counts problems by code since start, "cond" lists what is wrong right now (a
    worker that stopped), the ages say when each worker last did something."""
    host, _, port = MONITOR.rpartition(":")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    days, warned = cert_days() if URL.startswith("https://") else None, False
    log.info("Sending status to signal-monitor at %s every %d s%s.", MONITOR, STATUS_EVERY,
             f", as {MONITOR_NAME or '(its container name)'} reading {RELAY_ID or '(found from SIGNAL_URL)'}" if MONITOR_NAME or RELAY_ID else "")
    dead = set()
    while True:
        cond = []
        for name, t in tasks.items():                           # a worker that ended is a bug: say so, every time
            if t.done():
                cond.append(f"{name.upper()}_DEAD")
                if name not in dead:
                    dead.add(name)
                    err = t.exception() if not t.cancelled() else None
                    log.error("The %s stopped (%r): nothing is being %s. Restart the subscriber.", name, err,
                              {"sender": "sent", "reader": "read"}.get(name, "done"), extra=problem(f"{name.upper()}_DEAD"))
        oldest = queue._queue[0][2] if queue.qsize() else None   # when the oldest waiting message was read
        msg = {**({"inst": RELAY_ID} if RELAY_ID else {}), **({"name": MONITOR_NAME} if MONITOR_NAME else {}),
               "via": urllib.parse.urlsplit(URL).hostname or "", "stream": STATUS["stream"],
               "stream_error": STATUS["stream_error"][:150], "stream_code": STATUS["stream_code"], "sent": STATUS["sent"], "ignored": STATUS["ignored"],
               "queue": queue.qsize(), "errors": STATUS["errors"], "warnings": STATUS["warnings"],
               "last_event_age_s": age(STATUS["last_event"]), "cert_days": days,
               "up_s": int(time.time() - STATUS["started"]),
               "rx_age": age(STATUS["rx_at"]), "tx_age": age(STATUS["tx_at"]), "q_age": age(oldest),
               "p": STATUS["p"], "cond": cond, "cid": socket.gethostname()[:12],   # Docker: the short container ID
               "flow": {"rooms": [[room, st["s"], st["m"], age(st["m_at"])] for room, st in STREAMS.items()],
                        "rf": ROUTES_FOR[:64],                       # its routes.conf name: running twice = sent twice
                        # per group and room: [first 8 characters of the group ID, room, sent this run, seconds since]
                        "g": [[gid[:8], room, n, age(at)] for room, st in STREAMS.items() for gid, (n, at) in st["g"].items()][:12],
                        "read": STATUS["read"],
                        "lw": STATUS["lw"]}}
        try:
            sock.sendto(json.dumps(msg).encode(), (host, int(port)))
            warned = False
        except OSError as e:                     # e.g. signal-monitor's name doesn't resolve yet
            if not warned:
                log.warning("Could not send status to signal-monitor at %s (%s). Relaying is not affected.", MONITOR, e)
            warned = True
        await asyncio.sleep(STATUS_EVERY)


async def main():
    queue = asyncio.Queue()             # decouples reading from paced sending (a slow reader could miss
    names = Names()                     # events, and pacing must not hold up the connection)
    await names.load()
    tasks = {"sender": asyncio.create_task(sender(queue, names))}
    if MONITOR:
        asyncio.create_task(status_sender(queue, tasks))
    log.info("Starting up as %s (routes in %s). This run's sessions: %s.", ROUTES_FOR, ROUTES_FILE,
             ", ".join(f"{room} {st['s']}" for room, st in STREAMS.items()))
    if MODE == "rest-api":
        log.info("Reading messages from the old REST API at %s.", API)
    else:
        log.info("Reading messages from signal-cli at %s, %s.", URL,
                 "encrypted (TLS 1.3, with this subscriber's certificate)" if SSL else "NOT encrypted")
        saved = load_state()
        log.info("Resuming after the last event handled before (saved in %s)." % STATE if saved else
                 "No saved position, so starting with new messages." if STATE else "Not saving the stream position.")
    for room in STREAMS:
        mine = sorted(g for g, rooms in ROUTES.items() if room in rooms)
        log.info("Room %s gets %s %s.", room, "group" if len(mine) == 1 else "groups", ", ".join(g[:8] + "…" for g in mine))
    log.info("Sending to %s:%d over UDP, at most %d bytes per packet%s.", DEST[0], DEST[1], MAX_DGRAM,
             f", each packet sent {COPIES} times" if COPIES > 1 else "")
    log.info("Keeping a copy of every sent message in %s." if LOG else "Not keeping a copy of sent messages.", LOG)
    events = receive_rest_api() if MODE == "rest-api" else receive_signal_cli()
    async for event, event_id in events:
        env = event.get("envelope") or {}
        dm = ((env.get("editMessage") or {}).get("dataMessage")
              or env.get("dataMessage")
              or (env.get("syncMessage") or {}).get("sentMessage") or {})
        gid = (dm.get("groupInfo") or {}).get("groupId")
        STATUS["last_event"] = time.time()
        if gid not in ROUTES:
            STATUS["ignored"] += 1
            log.debug("Ignored an event that is not a message in a routed group.")
            queue.put_nowait((None, event_id, time.time(), None))   # in order, so the saved ID never skips an unsent message
            continue
        queue.put_nowait((json.dumps(event), event_id, time.time(), gid))


ROUTES, why = read_routes(ROUTES_FILE, ROUTES_FOR)   # never relay a chat by accident: refuse on any doubt
if why:
    log.error("Not starting: %s.", why)
    raise SystemExit(2)
# one stream per room: its own session (new each run), packet and message numbers, and CEF header
STREAMS = {room: {"s": os.urandom(4).hex(), "q": 0, "m": 0, "m_at": None, "header": HEADER.format(room=room),
                  "g": {gid: [0, None] for gid, rooms in ROUTES.items() if room in rooms}}   # per group: [sent, when]
           for room in sorted({r for rooms in ROUTES.values() for r in rooms})}

def stop_now(*_):
    """docker stop sends SIGTERM; stop at once (as Ctrl-C would) instead of being killed 10 s later.
    Safe: the saved event ID is written atomically, and anything still queued is resent on the next start."""
    raise KeyboardInterrupt


signal.signal(signal.SIGTERM, stop_now)
try:
    asyncio.run(main())
except KeyboardInterrupt:
    try:
        # "in touch with signal-cli until then" (see EventIds): now if still connected; else when the stream dropped
        # (signal-cli stopping a moment before us, as in "docker compose down", must not look like a long gap)
        t = time.time() if STATUS["stream"] == "connected" else STATUS["dropped_at"] or STATUS["rx_at"]
        if STATE and os.path.exists(STATE) and t and t > os.path.getmtime(STATE):
            os.utime(STATE, (t, t))
    except OSError:
        pass
    log.info("Stopped.")
