#!/usr/bin/env python3
"""Subscribe to signal-cli and stream one group's messages as CEF over UDP (one-way, diode-safe).

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

Each Signal event (raw signal-cli JSON) is split into parts so every CEF line fits one UDP datagram.
Every part is one datagram carrying one CEF line, built with comlibv3.string_to_cef and sent with
comlibv3.send_events_over_udp (one call per line, so PACE applies between datagrams):
    CEF:0|J6-DTT|CHAT|<i>|<n>|SIGNAL-RELAY|APP|{"s":session,"q":seq,"m":msgid,"i":i,"n":n,"d":"<json slice>"}
The receiver (cef_to_json.py, container signal-receiver) joins the "d" slices of one msgid in order and
json-decodes the result. In prod the data diode sits between this script and the receiver.

The event is forwarded as signal-cli sent it, with display names (looked up from the API) added:
  - every mention: "name" (which signal-cli fills with the number or uuid) is set to the display name
  - every quote:   "authorName" is added (signal-cli only gives the author's number and uuid)
The receiving side cannot ask the API, so this is the only place names can come from. If a lookup fails,
that field is left as signal-cli sent it.

With MONITOR_ADDR set (host:port of this host's signal-monitor), it also sends a small status packet there every
STATUS_EVERY seconds: connected or not, time since the last event, counts, queue, and new error lines. Never any
message content. RELAY_ID names the signal-cli instance it reads from; MONITOR_NAME names this subscriber.

    python cef_to_json.py         # then:  python subscriber_udp.py
"""
import asyncio, json, logging, os, socket, ssl, time, urllib.parse, urllib.request, websockets
from comlibv3 import string_to_cef, send_events_over_udp

NUM     = "+19415499805"            # your Signal number (the account the signal-cli daemon runs as)
GROUPS  = {g.strip() for g in os.environ.get("GROUP", "").split(",") if g.strip()}  # group ID(s) to relay; required
API     = os.environ.get("SIGNAL_API", "127.0.0.1:8080")                  # rest-api mode only (host:port)
URL     = os.environ.get("SIGNAL_URL") or f"http://{API}"                 # signal-cli: https://host:port via the TLS gateway
TLS     = {k: os.environ.get(f"TLS_{k.upper()}", f"/certs/{f}")            # used when URL is https://
           for k, f in (("ca", "ca.crt"), ("cert", "client.crt"), ("key", "client.key"))}
STATE   = os.environ.get("STATE_FILE", "signal-last-event-id") or None    # last handled event ID; "" to disable
MODE    = os.environ.get("SIGNAL_MODE", "signal-cli")                     # "signal-cli" or "rest-api" (old image)
DEST    = (os.environ.get("UDP_DEST", "127.0.0.1"), int(os.environ.get("UDP_PORT", 6510)))  # cef_to_json.py / diode ingress
HEADER  = "CEF:0|J6-DTT|CHAT|{}|{}|SIGNAL-RELAY|APP|"   # {}/{} = part i / part count
MAX_DGRAM = 1472                    # MTU 1500 - 20 IP - 8 UDP; whole CEF line must fit
PACE    = 0.002                     # seconds between datagrams (smooths bursts for the receiver)
COPIES  = 1                         # send each datagram N times; receiver drops repeats
LOG     = os.environ.get("SENT_LOG", "signal-udp-sent.jsonl") or None  # {"s","m","event"} per event, for manual recovery; "" to disable
NAMES_REFRESH = 60                  # seconds; reload the name list at most this often, when an unknown person is mentioned
PROFILE_RETRY = 600                 # seconds before asking Signal again for the same unnamed person's profile
MONITOR = os.environ.get("MONITOR_ADDR", "")                     # signal-monitor's host:port (UDP); "" = no status
RELAY_ID = os.environ.get("RELAY_ID", "relay-1")                 # the signal-cli instance this subscriber reads
MONITOR_NAME = os.environ.get("MONITOR_NAME") or socket.gethostname()   # this subscriber's name on the Monitor tab
STATUS_EVERY = 10                   # seconds between status packets

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("subscriber")

# What the status packets report (see MONITOR_ADDR). Updated as things happen; read by status_sender().
STATUS = {"stream": "starting", "stream_error": "", "last_event": None, "sent": 0, "ignored": 0,
          "errors": 0, "warnings": 0, "new_errors": [], "started": time.time()}


class CountProblems(logging.Handler):
    """Counts warnings and errors, and keeps new error lines for the next status packet."""
    def emit(self, record):
        if record.levelno >= logging.ERROR:
            STATUS["errors"] += 1
            if len(STATUS["new_errors"]) < 20:
                STATUS["new_errors"].append(record.getMessage()[:300])
        elif record.levelno >= logging.WARNING:
            STATUS["warnings"] += 1


log.addHandler(CountProblems())

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
SESSION = os.urandom(4).hex()       # new per run, so the receiver can tell restarts apart
WIDE    = 99999                     # worst-case width used when sizing parts (i, n, msgid)


def line_len(i, n, probe):
    """Exact byte length of one CEF line for this part."""
    return len(HEADER.format(i, n)) + len(string_to_cef([probe], ""))


def split(raw, msgid, seq0):
    """Cut raw JSON into the largest slices whose CEF line fits MAX_DGRAM. Returns part dicts."""
    slices, pos = [], 0
    while pos < len(raw):
        lo, hi = 1, min(len(raw) - pos, MAX_DGRAM)
        while lo < hi:                                  # largest slice that fits (binary search)
            mid = (lo + hi + 1) // 2
            probe = {"s": SESSION, "q": seq0 + WIDE, "m": WIDE, "i": WIDE, "n": WIDE,
                     "d": raw[pos:pos + mid]}
            if line_len(WIDE, WIDE, probe) <= MAX_DGRAM:
                lo = mid
            else:
                hi = mid - 1
        slices.append(raw[pos:pos + lo])
        pos += lo
    n = len(slices)
    return [{"s": SESSION, "q": seq0 + k, "m": msgid, "i": k + 1, "n": n, "d": d}
            for k, d in enumerate(slices)]


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
            log.warning("Could not load display names from the Signal API, so mentions may show numbers: %r", e)
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
            log.warning("Could not fetch the Signal profile of %s, so they stay unnamed for now: %r", key, e)
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
                            "mentioned person" if is_mention else "quoted author", number or uuid)
                continue
            obj["name" if is_mention else "authorName"] = name
            named[0 if is_mention else 1] += 1
        return tuple(named)


async def sender(queue, names):
    """Drain the queue: encode, split, and send datagrams at a steady pace."""
    seq, msgid = 0, 0
    sent_file = open(LOG, "a", encoding="utf-8") if LOG else None
    while True:
        raw, event_id = await queue.get()
        if raw is None:                                                # an ignored event: only remember its ID
            save_state(event_id)
            continue
        msgid += 1
        try:
            event = json.loads(raw)
            mentions, quotes = await names.fill(event)
            raw = json.dumps(event, separators=(",", ":"))             # compact, ASCII-only
            parts = split(raw, msgid, seq)
            lines = string_to_cef(parts, HEADER).split("\n")           # safe: escaped JSON has no \n
        except (Exception, SystemExit) as e:                           # comlibv3 calls exit() on error
            log.error("Could not convert message #%d to CEF, so it was skipped: %r", msgid, e)
            save_state(event_id)
            continue
        seq += len(parts)
        STATUS["sent"] += 1
        if sent_file:
            sent_file.write(json.dumps({"s": SESSION, "m": msgid, "event": event}) + "\n"); sent_file.flush()
        for line in lines:
            if len(line) > MAX_DGRAM:                                  # ASCII, so chars == bytes; should never happen
                log.warning("A packet for message #%d is %d bytes, over the %d-byte limit. "
                            "Sent anyway, but the network may drop it.", msgid, len(line), MAX_DGRAM)
            for _ in range(COPIES):
                send_events_over_udp(line, *DEST)                      # one line = one datagram; logs its own errors
                await asyncio.sleep(PACE)                              # one call per line keeps the pacing
        n = len(parts)
        packets = f"1 packet (#{seq - 1})" if n == 1 else f"{n} packets (#{seq - n} to #{seq - 1})"
        waiting = f" {queue.qsize()} more waiting to be sent." if queue.qsize() else ""
        named = "".join(f", {k} {what} named" for k, what in ((mentions, "mention(s)"), (quotes, "quote author"))
                        if k)
        log.info("Sent message #%d (%s%s, %s bytes) as %s.%s",
                 msgid, describe(event), named, f"{len(raw):,}", packets, waiting)
        save_state(event_id)


async def receive_signal_cli():
    """Yield (event, event ID) for each incoming event ({"envelope", "account"}) from signal-cli's /api/v1/events.
    Reconnects on its own; Last-Event-ID makes signal-cli resend events missed while disconnected. It starts from
    the saved ID: if signal-cli has restarted since, it doesn't know that ID and resends all it has kept."""
    import aiohttp
    url, last_id = f"{URL}/api/v1/events", load_state()
    timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout, read_bufsize=2 ** 22) as http:   # big events fit one line
        while True:
            headers = {"Accept": "text/event-stream"}
            if last_id:
                headers["Last-Event-ID"] = last_id
            try:
                async with http.get(url, headers=headers, ssl=SSL if SSL else True) as resp:
                    resp.raise_for_status()
                    STATUS["stream"], STATUS["stream_error"] = "connected", ""
                    log.info("Connected to signal-cli at %s. Waiting for messages%s.", url,
                             " (asked for anything missed since the last one)" if last_id else "")
                    data, event_id = [], None
                    async for raw_line in resp.content:                # one SSE line at a time
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
                            yield (p.get("result", p) if isinstance(p, dict) else {}), event_id
                        if event_id:
                            last_id = event_id
                        data, event_id = [], None
                STATUS["stream"], STATUS["stream_error"] = "disconnected", "signal-cli closed the event stream"
                log.warning("signal-cli closed the event stream. Reconnecting in 3 seconds...")
            except ssl.SSLError as e:
                STATUS["stream"], STATUS["stream_error"] = "disconnected", f"secure connection failed: {e}"
                log.error("The secure connection to signal-cli failed (%s). Check the certificates. Retrying in 3 seconds...", e)
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
                STATUS["stream"], STATUS["stream_error"] = "disconnected", f"lost the connection: {e}"
                log.warning("Lost the connection to signal-cli (%s). Reconnecting in 3 seconds...", e)
            await asyncio.sleep(3)


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
            log.warning("Lost the connection to the Signal API. Reconnecting...")


def cert_days():
    """Days until this subscriber's client certificate expires, or None."""
    try:
        return int((ssl.cert_time_to_seconds(ssl._ssl._test_decode_cert(TLS["cert"])["notAfter"]) - time.time()) // 86400)
    except Exception:
        return None


async def status_sender(queue):
    """Every STATUS_EVERY seconds: one small JSON packet to signal-monitor (this host's network; not the diode)."""
    host, _, port = MONITOR.rpartition(":")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    days, warned = cert_days() if URL.startswith("https://") else None, False
    log.info("Sending status to signal-monitor at %s every %d s, as %s (%s).", MONITOR, STATUS_EVERY, MONITOR_NAME, RELAY_ID)
    while True:
        last = STATUS["last_event"]
        msg = {"inst": RELAY_ID, "name": MONITOR_NAME, "stream": STATUS["stream"],
               "stream_error": STATUS["stream_error"][:150], "sent": STATUS["sent"], "ignored": STATUS["ignored"],
               "queue": queue.qsize(), "errors": STATUS["errors"], "warnings": STATUS["warnings"],
               "last_event_age_s": int(time.time() - last) if last else None, "cert_days": days,
               "up_s": int(time.time() - STATUS["started"]), "new_errors": STATUS["new_errors"][:10]}
        STATUS["new_errors"] = STATUS["new_errors"][10:]
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
    asyncio.create_task(sender(queue, names))
    if MONITOR:
        asyncio.create_task(status_sender(queue))
    log.info("Starting up. This run's session ID is %s.", SESSION)
    if MODE == "rest-api":
        log.info("Reading messages from the old REST API at %s.", API)
    else:
        log.info("Reading messages from signal-cli at %s, %s.", URL,
                 "encrypted (TLS 1.3, with this subscriber's certificate)" if SSL else "NOT encrypted")
        saved = load_state()
        log.info("Resuming after the last event handled before (saved in %s)." % STATE if saved else
                 "No saved position, so starting with new messages." if STATE else "Not saving the stream position.")
    log.info("Watching %s %s.", "group" if len(GROUPS) == 1 else "groups", ", ".join(sorted(GROUPS)))
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
        if gid not in GROUPS:
            STATUS["ignored"] += 1
            log.debug("Ignored an event that is not a message in the watched group.")
            queue.put_nowait((None, event_id))     # in order, so the saved ID never skips an unsent message
            continue
        queue.put_nowait((json.dumps(event), event_id))


if not GROUPS:   # never relay every chat by accident
    log.error("No group is set. Set GROUP to the ID of the group to relay (several IDs: separated by commas).")
    raise SystemExit(1)

try:
    asyncio.run(main())
except KeyboardInterrupt:
    log.info("Stopped.")
