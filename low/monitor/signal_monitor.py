#!/usr/bin/env python3
"""signal-monitor: watches this host's part of the relay and reports it across the diode (one-way).

One per left host. It never sends anything to Signal and never touches messages.
  - signal-cli: GET /api/v1/check through each instance's TLS gateway, with this monitor's own client certificate
    (CN signal-monitor, which must be in TLS_CLIENT_NAMES); also reads the gateway certificate's expiry date.
  - subscribers: each sends a small status packet here every 10 s (UDP, this host's network only). One that goes
    quiet for SUB_TIMEOUT is reported DOWN.
  - this host: free disk space under DISK_PATHS.
Every HEARTBEAT seconds it sends, as CEF over UDP to the diode (UDP_DEST:UDP_PORT; header CEF:0|CHAT|MONITOR|, which
the diode routes to monitor-receiver):
  {"t":"hb", ...}   its own heartbeat
  {"t":"st", ...}   the state of each signal-cli and subscriber (also sent at once when it changes), with "code":
                    a fixed word for why (OK, STARTING, CERT_EXPIRING, UNREACHABLE, NOT_CONNECTED, NO_STATUS, ...)
  {"t":"ev", ...}   things that are not states: error lines from a subscriber, start-up
Each message is one datagram (at most MAX_DGRAM bytes):
  CEF:0|CHAT|MONITOR|1|1|SIGNAL-RELAY-MON|MON|{"t":..,"host":..,"boot":..,"seq":..,"ts":..,...}
"boot" is random per start and "seq" counts up, so the right side can count lost packets.

Settings (environment):
  MONITOR_HOST     this host's name on the Monitor tab (default: the Docker host's own name, from /etc/hostname
                   mounted at /host/hostname; else the container's)
  SIGNAL_TARGETS   each signal-cli's URL, comma-separated, e.g. "https://signal:8443". Each is named on the Monitor
                   tab by its container name (from container-facts, so also while it is stopped; else Docker's DNS;
                   else the URL's host). "name=URL" sets a name by hand.
  Subscribers need no name either: each is named by its container name (Docker's DNS, from where its status
  came from) and belongs to the signal-cli whose URL host matches its SIGNAL_URL (or the only one).
  STATUS_PORT      where subscribers send their status (UDP, default 6520)
  UDP_DEST, UDP_PORT   the diode's input for monitoring (compose: the same input as chat, routed by header)
  TLS_CA, TLS_CERT, TLS_KEY   default /certs/ca.crt, /certs/client.crt, /certs/client.key
  DISK_PATHS       comma-separated folders whose disk to check (default /certs: same disk as low/); DISK_LOW_PCT (default 10)
  CERT_WARN_DAYS   a certificate closer than this to expiry makes its part WARN (default 30)
  FACTS_URL        container-facts (read-only view of this project's containers), e.g. http://container-facts:8094;
                   empty: no container facts. Only news is sent ({"t":"cf","c":[one container]}, 3 times), plus
                   one summary at start; the heartbeat carries a fingerprint ("cfh") of what is known.
"""
import asyncio, hashlib, json, logging, os, secrets, signal, shutil, socket, ssl, time, urllib.request
from datetime import datetime
from urllib.parse import urlsplit
from comlibv3 import string_to_cef, send_events_over_udp

def host_name():
    try:
        return open("/host/hostname").read().strip() or socket.gethostname()
    except OSError:
        return socket.gethostname()


HOST      = os.environ.get("MONITOR_HOST") or host_name()
TARGET_LIST = [t.strip() for t in os.environ.get("SIGNAL_TARGETS", "").split(",") if t.strip()]
TARGETS   = {}                         # name -> URL, filled by name_targets() at start
STATUS    = ("0.0.0.0", int(os.environ.get("STATUS_PORT", 6520)))
DEST      = (os.environ.get("UDP_DEST", "127.0.0.1"), int(os.environ.get("UDP_PORT", 6511)))
TLS       = {k: os.environ.get(f"TLS_{k.upper()}", f"/certs/{f}") for k, f in
             (("ca", "ca.crt"), ("cert", "client.crt"), ("key", "client.key"))}
DISKS     = [d.strip() for d in os.environ.get("DISK_PATHS", "/certs").split(",") if d.strip()]
DISK_LOW  = float(os.environ.get("DISK_LOW_PCT", 10))
CERT_WARN = int(os.environ.get("CERT_WARN_DAYS", 30))
HEADER    = "CEF:0|CHAT|MONITOR|{}|{}|SIGNAL-RELAY-MON|MON|"   # the diode routes CHAT|MONITOR to monitor-receiver
MAX_DGRAM = 1472
HEARTBEAT = 30                      # seconds between heartbeats (and status repeats)
SUB_TIMEOUT = 35                    # a subscriber silent this long is DOWN (it reports every 10 s)
CHECK_TIMEOUT = 10
START_GRACE = 60                    # seconds after a start in which "not answering yet" is WARN, not DOWN
FACTS_URL = os.environ.get("FACTS_URL", "").rstrip("/")
FACTS_EVERY = 15
NAME_OK   = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._@-")

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("monitor")

BOOT, STARTED = secrets.token_hex(4), time.time()
seq = 0


def name_ok(s):
    return isinstance(s, str) and 0 < len(s) <= 64 and set(s) <= NAME_OK


def send(msg):
    """One message = one datagram across the diode. Shortens "text" if the line would be too long."""
    global seq
    seq += 1
    msg = {"t": msg.pop("t"), "host": HOST, "boot": BOOT, "seq": seq, "ts": int(time.time() * 1000), **msg}
    for _ in range(4):
        try:
            line = string_to_cef([msg], HEADER)
        except SystemExit:                                   # comlibv3 calls exit() on error
            log.error("Could not turn a monitoring message into CEF, so it was not sent.")
            return
        if len(line) <= MAX_DGRAM:
            send_events_over_udp(line, *DEST)                # logs its own errors
            return
        msg["text"] = (msg.get("text") or "")[: max(0, len(msg.get("text") or "") - (len(line) - MAX_DGRAM) - 20)] + "…"
        msg["info"] = {k: v for k, v in (msg.get("info") or {}).items() if not isinstance(v, str)}
    log.warning("A monitoring message stayed over %d bytes, so it was not sent.", MAX_DGRAM)


def days_left(not_after):
    """Whole days until an X.509 notAfter date (as Python's ssl gives it)."""
    return int((ssl.cert_time_to_seconds(not_after) - time.time()) // 86400)


def own_cert_days():
    try:
        return days_left(ssl._ssl._test_decode_cert(TLS["cert"])["notAfter"])
    except Exception:
        return None


def tls_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_verify_locations(TLS["ca"])
    ctx.load_cert_chain(TLS["cert"], TLS["key"])
    return ctx


def check(url, ctx):
    """GET /api/v1/check through the TLS gateway. (ok, ms, gateway certificate days left, error text, code, health).
    code is fixed (for the Monitor tab's likely-cause rules): OK, GATEWAY_CLOSED, HTTP_ERROR, TLS_FAILED, TIMEOUT,
    UNREACHABLE. health: the JSON body the in-house image (r2 and later) answers with: signal-cli's link to Signal's
    servers, its receive loop, Java memory, the gateway's counters and problems counted by code; None before r2."""
    u = urlsplit(url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=CHECK_TIMEOUT) as raw:
            with (ctx.wrap_socket(raw, server_hostname=host) if u.scheme == "https" else raw) as s:   # http: test rigs only
                days = days_left(s.getpeercert()["notAfter"]) if u.scheme == "https" else None
                s.sendall(f"GET /api/v1/check HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                resp = b""
                while len(resp) < 65536:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
        head, _, body = resp.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        ms = int((time.monotonic() - t0) * 1000)
        parts = status.split()
        if len(parts) >= 2 and parts[1].startswith("2"):
            try:
                health = json.loads(body) if body.strip() else None
            except ValueError:
                health = None
            return True, ms, days, "", "OK", health if isinstance(health, dict) else None
        if not status:
            return (False, ms, days, "the gateway closed the connection without answering (is signal-monitor in "
                    "TLS_CLIENT_NAMES?)", "GATEWAY_CLOSED", None)
        return False, ms, days, f"answered {status}", "HTTP_ERROR", None
    except ssl.SSLError as e:
        return False, None, None, f"secure connection failed ({e.reason or e}); check the certificates", "TLS_FAILED", None
    except TimeoutError:
        return False, None, None, f"no answer within {CHECK_TIMEOUT} s", "TIMEOUT", None
    except OSError as e:
        return False, None, None, f"not reachable ({e.strerror or e})", "UNREACHABLE", None


RECENT_S = 60                       # a counted problem of the app's own keeps its part WARN/DOWN this long after it
                                    # last happened: the colour is now, not "earlier" (user, 2026-10-20: was 600)


class Recent:
    """Problem counters (code -> count since the app started) in, "how many in the last RECENT_S" out.
    The counts are cumulative, so a lost status packet loses nothing; a count that goes down means the app restarted.
    The first counts seen are the baseline, unless the app has only just started (then they are all new)."""
    def __init__(self):
        self.last, self.ups = None, {}               # code -> count; code -> [(time, how many more)]

    def update(self, counts, fresh=False):
        now = time.time()
        counts = {k: v for k, v in (counts or {}).items() if isinstance(k, str) and isinstance(v, int)}
        if self.last is None and not fresh:
            self.last = counts
            return
        last = self.last or {}
        restarted = any(v < last.get(k, 0) for k, v in counts.items())
        for code, n in counts.items():
            more = n if restarted else n - last.get(code, 0)
            if more > 0:
                self.ups.setdefault(code, []).append((now, more))
        self.last = counts
        for code in list(self.ups):
            self.ups[code] = [x for x in self.ups[code] if now - x[0] < RECENT_S]
            if not self.ups[code]:
                del self.ups[code]

    def n(self, code):
        return sum(m for _, m in self.ups.get(code, []))


def utc(ms):
    return time.strftime("%H:%M", time.gmtime(ms / 1000)) if isinstance(ms, (int, float)) else "?"


# A subscriber's counted problems of its own that change its state, worst first: (code, state, text for n of them).
# Problems on its links (SEND_FAILED: the chat route; STREAM_STALLED: the stream from signal-cli) and lost messages
# (EVENTS_MISSED, CLI_RESTART_LOSS) are counted ("p") but don't colour the box: the console puts them on the
# connection and in the Log (docs/monitoring-redesign.md).
SUB_COUNTED = [
    ("MESSAGE_SKIPPED", "warn", "{n} message(s) could not be converted and were skipped (not relayed) just now"),
    ("SENDER_ERROR", "warn", "{n} message(s) failed to send in an unexpected way and were skipped"),
    ("WRITE_FAILED", "warn", "{n} write(s) to its files failed just now (sent log or resume position): disk full or permissions?"),
    ("PACKET_TOO_BIG", "warn", "{n} packet(s) over the 1472-byte limit: the network may drop them"),
    ("OTHER", "warn", "{n} unexpected error(s) in its log just now (see its Docker log)"),
]
# Why a subscriber's stream from signal-cli is down, in plain words (its stream_code); the raw error stays in its log.
STREAM_WORDS = {"UNREACHABLE": "can't connect to signal-cli (refused, no route, or the name doesn't resolve)",
                "TLS_FAILED": "the secure connection to signal-cli failed (a certificate problem)",
                "GATEWAY_CLOSED": "signal-cli's gateway closed the connection (is this subscriber's certificate allowed?)",
                "HTTP_ERROR": "signal-cli's gateway answered with an error",
                "TIMEOUT": "signal-cli didn't answer in time",
                "LOST": "the connection to signal-cli dropped"}
# signal-cli's counted problems (from its health report, image r2).
CLI_COUNTED = [
    ("STORAGE_ERROR", "down", "{n} database error(s) in its account data just now"),
    ("EVENT_DROPPED", "warn", "{n} received message(s) it could not turn into an event (not relayed)"),
    ("DECRYPT_FAILED", "warn", "{n} message(s) it could not decrypt"),
    ("UNTRUSTED_IDENTITY", "warn", "{n} message(s) from a sender whose safety number changed (held back)"),
    ("HANDLE_ERROR", "warn", "{n} error(s) while handling received messages"),
    ("GATEWAY_FULL", "warn", "its gateway refused {n} connection(s): all 32 connections in use"),
    ("OTHER", "warn", "{n} unexpected error(s) in its log just now (see its Docker log)"),
]
# GATEWAY_REFUSED (a client with a certificate it doesn't allow) and RPC_BLOCKED (a call that isn't allowed) are the
# gateway doing its job, not signal-cli's health: counted ("p") and logged, no colour.
SIGNAL_DOWN_S = 120                  # signal-cli without Signal's servers this long: DOWN (before: reconnecting, WARN)
STALLED_S = 180                      # signal-cli's receive loop wakes at least once a minute
FLAP_PER_H = 5                       # more reconnects to Signal than this in an hour: flapping


def counted(rules, recent, extra=None):
    """Active counted problems: [(code, state, text)], worst first."""
    out = []
    for code, state, text in rules:
        n = recent.n(code)
        if n:
            out.append((code, state, text.format(n=n, **(extra or {}))))
    return out


class Monitor:
    def __init__(self):
        self.ctx = tls_context() if any(u.startswith("https://") for u in TARGETS.values()) else None
        self.cli = {}           # instance -> {"state", "info", "text", ...}; (instance, "signal-servers") -> the same
        self.cli_recent, self.reconnects, self.last_rc = {}, {}, {}
        self.subs = {}          # (instance, name) -> {"state", "info", "text", "seen", "err_window", "err_count", ...}

    # ---- states ----
    def report(self, inst, comp, name, state, info, text, code, extra=None):
        """Send a part's state (on every heartbeat, and at once when it changes). code: a fixed word for why.
        extra: "active" (every code that applies now, worst first), "p" (problem counts since the part started),
        "flow" (a subscriber's numbering, for the right side's "did it arrive?" check)."""
        if comp == "subscriber" and (inst, name) in self.subs:      # when its own status last arrived (this host's
            extra = {**(extra or {}), "heard": int(self.subs[(inst, name)]["seen"] * 1000)}   # clock, like container facts)
        send({"t": "st", "inst": inst, "comp": comp, "name": name, "state": state, "code": code, "info": info,
              "text": text, **(extra or {})})

    def set_cli(self, inst, state, info, text, code, extra=None, comp="signal-cli"):
        key = inst if comp == "signal-cli" else (inst, comp)
        old = self.cli.get(key, {}).get("code")
        self.cli[key] = {"state": state, "info": info, "text": text, "code": code, "extra": extra, "comp": comp, "inst": inst}
        if code != old:
            what = "signal-cli" if comp == "signal-cli" else "signal-cli's link to Signal's servers"
            mine = "" if inst == "signal-cli" else f" ({inst})"           # only when the container has another name
            (log.info if state == "up" else log.warning)("%s%s is %s%s.", what, mine, state.upper(), f": {text}" if text else "")
            self.report(inst, comp, comp, state, info, text, code, extra)

    async def check_loop(self):
        while True:
            for inst, url in TARGETS.items():
                ok, ms, days, err, code, health = await asyncio.to_thread(check, url, self.ctx)
                info = {"check_ms": ms, "cert_days": days}
                active, extra = [], {}
                state, text = ("up", "") if ok else ("down", err)
                if not ok and time.time() - STARTED < START_GRACE:      # everything starting together
                    state, text, code = "warn", f"not answering yet, just started ({err})", "STARTING"
                if ok and health:
                    active, extra, more = self.cli_health(inst, health)
                    info.update(more)
                if active:                                          # worst first; a DOWN beats the certificate
                    state, text, code = active[0][1], active[0][2], active[0][0]
                if ok and days is not None and days < CERT_WARN and state == "up":
                    state, text, code = "warn", f"its certificate expires in {days} days", "CERT_EXPIRING"
                extra["active"] = [c for c, _, _ in active]
                self.set_cli(inst, state, info, text, code, extra)
            await asyncio.sleep(HEARTBEAT / 2)

    def cli_health(self, inst, h):
        """signal-cli's own health report (image r2): its problems ([(code, state, text)], worst first), extra fields
        for its st, and figures for its tile. Also reports its link to Signal's servers as a part of its own."""
        now = int(time.time() * 1000)
        r = self.cli_recent.setdefault(inst, Recent())
        boot = h.get("boot") if isinstance(h.get("boot"), int) else None
        r.update(h.get("p") if isinstance(h.get("p"), dict) else {}, fresh=bool(boot and now - boot < 120_000))
        rx, jvm, gw, sig = (h.get(k) if isinstance(h.get(k), dict) else {} for k in ("rx", "jvm", "gw", "sig"))
        active = []
        loop = rx.get("loop")
        if isinstance(loop, int) and now - loop > STALLED_S * 1000:
            active.append(("RECEIVE_STALLED", "down", f"its receive loop has not run for {(now - loop) // 60000} min: it is stuck, nothing is received"))
        active += [x for x in counted(CLI_COUNTED, r) if x[1] == "down"]
        heap = jvm.get("heap")
        if isinstance(heap, (int, float)) and heap >= 90:
            active.append(("JVM_MEMORY", "warn", f"Java memory {heap}% full after garbage collection: it may run out soon"))
        active += [x for x in counted(CLI_COUNTED, r) if x[1] == "warn"]
        info = {"heap_pct": heap, "gc_ms_min": jvm.get("gc"), "envelopes": rx.get("env"),
                "loop_age_s": (now - loop) // 1000 if isinstance(loop, int) else None, "gw_conns": gw.get("conns")}
        self.signal_servers(inst, sig, boot, now)
        return active, {"p": r.last or {}}, info

    def signal_servers(self, inst, sig, boot, now):
        """The "Signal servers" part: signal-cli's link to Signal (connected, keep-alives answered, failures)."""
        st, since, ka, fail = sig.get("st"), sig.get("since"), sig.get("ka"), sig.get("fail")
        hist = self.reconnects.setdefault(inst, [])
        rc = sig.get("rc") if isinstance(sig.get("rc"), int) else 0
        last_rc = self.last_rc.get(inst)
        if last_rc is not None and rc > last_rc:
            hist += [time.time()] * min(rc - last_rc, 50)
        self.last_rc[inst] = rc
        hist[:] = [t for t in hist if time.time() - t < 3600]
        why = {"CONNECT_FAILED": "every way of connecting failed (this host's network, a firewall or proxy, DNS, or Signal itself)",
               "DNS": "the name chat.signal.org does not resolve", "TIMEOUT": "connections time out",
               "REFUSED": "connections are refused or reset", "TLS": "the secure connection fails",
               "NETWORK": "no route to Signal's servers"}.get(fail, "") if fail else ""
        down_for = (now - since) // 1000 if isinstance(since, int) else None
        info = {"connected_s": down_for if st == "CONNECTED" else None, "keepalive_age_s": (now - ka) // 1000 if isinstance(ka, int) else None,
                "rtt_ms": sig.get("rtt"), "reconnects_1h": len(hist), "last_failure": fail}
        if st == "AUTH_FAILED" or fail == "DEREGISTERED":
            res = ("down", "DEVICE_UNLINKED", "Signal says this device is no longer linked or registered (unlinked "
                   "on the phone, or the number was registered elsewhere): no messages arrive until it is linked again")
        elif st == "EXPIRED" or fail == "EXPIRED":
            res = ("down", "CLIENT_EXPIRED", "Signal refuses this signal-cli version as too old: update it (image/check-update)")
        elif st == "CONNECTED":
            if isinstance(ka, int) and now - ka > SIGNAL_DOWN_S * 1000:
                res = ("down", "SIGNAL_UNREACHABLE", f"connected, but Signal has not answered a keep-alive since {utc(ka)} UTC")
            elif len(hist) > FLAP_PER_H:
                res = ("warn", "SIGNAL_FLAPPING", f"the connection to Signal dropped {len(hist)} times in the last hour")
            elif isinstance(sig.get("skew_at"), int) and now - sig["skew_at"] < 3600_000:
                res = ("warn", "CLOCK_SKEW", "this host's clock is more than a day off from Signal's")
            else:
                res = ("up", "OK", "")
        elif st == "STARTING":                           # no connection event seen at all: can't tell, not "down"
            res = (("warn", "STARTING", "signal-cli just started and is connecting to Signal's servers")
                   if boot and now - boot < START_GRACE * 1000 * 2 else
                   ("warn", "SIGNAL_UNKNOWN", "signal-cli's health report has seen no connection to Signal's servers "
                    "at all, so their state is unknown"))
        elif down_for is not None and down_for > SIGNAL_DOWN_S:
            res = ("down", "SIGNAL_UNREACHABLE", f"can't reach Signal's servers since {utc(since)} UTC"
                   + (f": {why}" if why else "") + "; new messages wait on Signal's servers")
        else:
            res = ("warn", "SIGNAL_RECONNECTING", "lost the connection to Signal's servers, reconnecting" + (f" ({why})" if why else ""))
        self.set_cli(inst, res[0], info, res[2], res[1], {"active": [res[1]] if res[1] != "OK" else []}, comp="signal-servers")

    # ---- subscriber status packets ----
    def sub_name(self, ip, cid):
        """A subscriber's container name, from where its status came (Docker's DNS), else its container ID.
        Remembered by container ID, not by address: two subscribers restarting together can swap addresses, and a
        name kept per address then stayed on the wrong one (2026-10-20: each box showed the other's rooms)."""
        names = self.__dict__.setdefault("cid_names", {})
        key = cid or ip                                  # an older subscriber sends no cid: its address, as before
        if key not in names:
            try:
                n = socket.gethostbyaddr(ip)[0].split(".")[0]
                names[key] = n if name_ok(n) and n != ip.split(".")[0] else None
            except OSError:
                names[key] = None
            if len(names) > 200:                         # recreated containers leave old IDs behind
                names.pop(next(iter(names)))
        return names[key] or str(cid or ip)[:12]

    @staticmethod
    def sub_inst(via):
        """The signal-cli a subscriber reads: the one whose URL has the host in its SIGNAL_URL, else the only one."""
        for name, url in TARGETS.items():
            if via and urlsplit(url).hostname == via:
                return name
        return next(iter(TARGETS)) if len(TARGETS) == 1 else (via or "?")

    def status(self, data, addr):
        try:
            m = json.loads(data.decode("utf-8"))
            inst, name = m.get("inst") or self.sub_inst(m.get("via")), m.get("name") or self.sub_name(addr[0], m.get("cid"))
            if not (name_ok(inst) and name_ok(name)):
                raise ValueError("bad instance or name")
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log.warning("Ignored a status packet from %s that is not valid: %r", addr[0], e)
            return
        key = (inst, name)
        s = self.subs.get(key)
        if s is None:
            s = self.subs[key] = {"state": None, "recent": Recent()}
            log.info("Subscriber %s (%s) reported for the first time, from %s.", name, inst, addr[0])
        s["seen"] = time.time()
        num = lambda k: m.get(k) if isinstance(m.get(k), int) else None
        info = {k: m.get(k) for k in ("stream", "last_event_age_s", "sent", "ignored", "queue", "errors", "warnings",
                                       "cert_days", "up_s", "stream_code", "rx_age", "tx_age", "q_age")
                if isinstance(m.get(k), (int, float, str)) or m.get(k) is None}
        p = m.get("p") if isinstance(m.get("p"), dict) else {}
        s["recent"].update(p, fresh=(num("up_s") or 0) < 60)
        info["missed"] = p.get("EVENTS_MISSED", 0)
        s["info"] = info
        flow = m.get("flow") if isinstance(m.get("flow"), dict) else {}
        lw = flow.get("lw") if isinstance(flow.get("lw"), list) and len(flow["lw"]) == 2 else None
        active = []                                              # every problem that applies now, worst first
        for c in (m.get("cond") or [])[:5]:
            if c == "SENDER_DEAD":
                active.append((c, "down", "its sender stopped: nothing is being sent (restart the subscriber)"))
        q, q_age, tx_age = num("queue") or 0, num("q_age"), num("tx_age")
        if q and q_age is not None and q_age > 120 and tx_age is not None and tx_age > 60:
            active.append(("SENDER_STALLED", "down", f"nothing sent for {tx_age} s while {q} message(s) wait"))
        active += counted(SUB_COUNTED, s["recent"], {"lw": f"between {utc(lw[0])} and {utc(lw[1])} UTC" if lw else "earlier"})
        if q > 100:
            active.append(("QUEUE_BACKLOG", "warn", f"{q} messages waiting to be sent"))
        elif q_age is not None and q_age > 300:
            active.append(("QUEUE_SLOW", "warn", f"the oldest waiting message is {q_age // 60} min old"))
        down = [a for a in active if a[1] == "down"]
        if m.get("stream") == "starting" and isinstance(m.get("up_s"), int) and m["up_s"] < START_GRACE:
            state, text, code = "warn", "starting up, not connected to signal-cli yet", "STARTING"
        elif m.get("stream") != "connected":                   # running, but its link is down: WARN, not DOWN
            why = STREAM_WORDS.get(m.get("stream_code")) or "not connected"
            state, text, code = "warn", f"running, but not connected to signal-cli: {why}", "NOT_CONNECTED"
        elif down:
            state, text, code = down[0][1], down[0][2], down[0][0]
        elif isinstance(m.get("cert_days"), int) and m["cert_days"] < CERT_WARN:
            state, text, code = "warn", f"its certificate expires in {m['cert_days']} days", "CERT_EXPIRING"
        elif active:
            state, text, code = active[0][1], active[0][2], active[0][0]
        else:
            state, text, code = "up", "", "OK"
        extra = {"active": [a[0] for a in active], "p": p, "cid": str(m.get("cid") or "")[:12],
                 "flow": {k: flow.get(k) for k in ("sess", "m", "m_age", "read") if isinstance(flow.get(k), (int, str))}}
        rooms = [r for r in (flow.get("rooms") or [])[:12] if isinstance(r, list) and len(r) == 4    # per room:
                 and isinstance(r[0], str) and len(r[0]) <= 32 and isinstance(r[1], str) and len(r[1]) <= 16   # [room,
                 and isinstance(r[2], int) and (r[3] is None or isinstance(r[3], int))]          # session, last #, age]
        if rooms:
            extra["flow"]["rooms"] = rooms
        if isinstance(flow.get("rf"), str):                      # its name in routes.conf
            extra["flow"]["rf"] = flow["rf"][:64]
        groups = [g for g in (flow.get("g") or [])[:12] if isinstance(g, list) and len(g) == 4    # per group: [ID start,
                  and isinstance(g[0], str) and len(g[0]) <= 8 and isinstance(g[1], str) and len(g[1]) <= 32   # room, sent,
                  and isinstance(g[2], int) and (g[3] is None or isinstance(g[3], int))]          # seconds since]
        if groups:
            extra["flow"]["g"] = groups
        if lw:
            extra["flow"]["lw"] = lw
        self.set_sub(key, state, text, code, extra)

    def set_sub(self, key, state, text, code, extra=None):
        s = self.subs[key]
        old, s["state"], s["text"], s["code"] = s.get("code"), state, text, code
        if extra is not None:
            s["extra"] = extra
        if code != old:
            (log.info if state == "up" else log.warning)("Subscriber %s (reads %s) is %s%s.", key[1], key[0], state.upper(),
                                                       f": {text}" if text else "")
            self.report(key[0], "subscriber", key[1], state, s.get("info", {}), text, code, s.get("extra"))

    # ---- heartbeat ----
    def disks(self):
        out, low = {}, []
        for i, path in enumerate(DISKS):
            try:
                u = shutil.disk_usage(path)
                pct = round(100 * u.free / u.total, 1)
                out["disk_free_pct" if i == 0 else f"disk{i}_free_pct"] = pct
                if pct < DISK_LOW:
                    low.append(f"{path} has {pct}% free")
            except OSError as e:
                low.append(f"can't check {path} ({e.strerror})")
        return out, low

    async def heartbeat_loop(self):
        while True:
            for key, s in self.subs.items():                     # silent subscribers
                if time.time() - s["seen"] > SUB_TIMEOUT and s["state"] != "down":
                    self.set_sub(key, "down", f"no status for {SUB_TIMEOUT} s (stopped, or can't reach signal-monitor)",
                                 "NO_STATUS")
            disk, low = self.disks()
            cert = own_cert_days()
            expiring = cert is not None and cert < CERT_WARN
            facts_down = bool(getattr(self, "facts", None) and self.facts.failing)
            text = "; ".join(low + ([f"its own certificate expires in {cert} days"] if expiring else [])
                             + (["can't read container facts (exit codes, restarts, memory)"] if facts_down else []))
            code = "DISK_LOW" if low else "CERT_EXPIRING" if expiring else "FACTS_UNAVAILABLE" if facts_down else "OK"
            hb = {"t": "hb", "up_s": int(time.time() - STARTED), "state": "warn" if text else "up", "text": text,
                  "code": code, "info": {**disk, "cert_days": cert, "parts": len(self.cli) + len(self.subs), "ver": 3}}
            if getattr(self, "facts", None) and self.facts.fingerprint():
                hb["cfh"] = self.facts.fingerprint()
            send(hb)
            for c in self.cli.values():
                self.report(c["inst"], c["comp"], c["comp"], c["state"], c["info"], c["text"], c["code"], c.get("extra"))
            for (inst, name), s in self.subs.items():
                self.report(inst, "subscriber", name, s["state"], s.get("info", {}), s.get("text", ""), s.get("code"), s.get("extra"))
            await asyncio.sleep(HEARTBEAT)


def iso_ms(s):
    """Docker's RFC 3339 time (nanoseconds, Z) as ms since 1970; None for Docker's zero time."""
    if not s or s.startswith("0001-"):
        return None
    try:
        return int(datetime.fromisoformat(s[:26].rstrip("Z") + "+00:00").timestamp() * 1000)
    except ValueError:
        return None


def container_of(host):
    """The container name behind a compose service (or container) name on this project's network, or None: from
    container-facts (works while it is stopped), else Docker's DNS (reverse lookup: "<container>.<network>")."""
    if FACTS_URL:
        try:
            with urllib.request.urlopen(f"{FACTS_URL}/facts", timeout=5) as r:
                for c in json.load(r).get("containers", []):
                    if host in (c.get("service"), c.get("name")) and name_ok(c.get("name", "")):
                        return c["name"]
        except (OSError, ValueError):
            pass
    try:
        name = socket.gethostbyaddr(socket.gethostbyname(host))[0].split(".")[0]
        return name if name_ok(name) else None
    except OSError:
        return None


def name_targets():
    """Fill TARGETS from SIGNAL_TARGETS. Names are fixed for this run (they are part of each tile's key), so wait up
    to 30 s for container-facts at start; a name that can't be found falls back to the URL's host, with a warning."""
    for t in TARGET_LIST:
        name, eq, url = t.partition("=")
        if eq and not name.startswith("http"):
            TARGETS[name.strip()] = url.strip()
            continue
        host = urlsplit(t).hostname or t
        for i in range(10):
            found = container_of(host)
            if found:
                break
            time.sleep(3)
        if not found:
            log.warning("Could not find the container behind %s (container-facts and Docker's DNS gave nothing): "
                        "calling it %r. Restart signal-monitor when it is up to use its container name.", t, host)
        TARGETS[found or host] = t


def read_facts():
    """This project's containers from container-facts, compact (short keys: one datagram holds several)."""
    with urllib.request.urlopen(f"{FACTS_URL}/facts", timeout=5) as r:
        data = json.load(r)
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return [{"n": c["name"][:64], "s": c.get("state"), "x": c.get("exit_code"), "o": bool(c.get("oom_killed")),
             "r": c.get("restarts", 0), "st": iso_ms(c.get("started")), "fi": iso_ms(c.get("finished")),
             "h": c.get("health"), "img": str(c.get("image") or "")[:80], "id": str(c.get("id") or "")[:12],
             "m": num(c.get("mem_pct")), "c": num(c.get("cpu_pct")), "pi": num(c.get("pids_pct"))}
            for c in data.get("containers", []) if name_ok(c.get("name", ""))]


HIGH_MEM, HIGH_PIDS, HIGH_CPU, CPU_FOR_S = 85, 85, 90, 300     # % of the container's own limit; CPU for 5 min


def bands(c, cpu_since):
    """Which resources are high now (the news is a change in this, not every new figure)."""
    out = []
    if (c.get("m") or 0) >= HIGH_MEM:
        out.append("mem")
    if (c.get("pi") or 0) >= HIGH_PIDS:
        out.append("pids")
    if cpu_since and time.time() - cpu_since >= CPU_FOR_S:
        out.append("cpu")
    return out


def facts_fingerprint(containers):
    """8 hex characters over each container's name, state and restart count: sent in every heartbeat, so the console
    can tell when it missed a change. The console's monitor.py computes the same."""
    rows = sorted([c["n"], c["s"], c.get("r", 0)] for c in containers)
    return hashlib.sha1(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()[:8]


class Facts:
    """Polls container-facts every FACTS_EVERY s and sends only news, one container per cf message:
    a container stopped, crashed, was restarted by Docker, came back, appeared or was removed. Each news item is
    sent REPEATS times (UDP has no acknowledgement). Once at start: the containers that aren't running, and the
    names (with restart counts) of those that are. Nothing is sent while nothing changes; the heartbeat carries
    facts_fingerprint() so the console notices if it missed something."""
    REPEATS = (0, 10, 30)                                        # seconds after the change

    def __init__(self):
        self.known, self.failing, self.started = {}, False, False
        self.pending = []                                        # (due time, message)
        self.cpu_since = {}                                      # container -> since when its CPU has been high

    def fingerprint(self):
        return facts_fingerprint(self.known.values()) if self.started else None

    def news(self, msg):
        """Queue a message's copies. Newer news about a container drops the unsent copies of older news about it,
        so a late copy can never undo a newer change (e.g. "running" arriving after "exited")."""
        now = time.time()
        names = {c.get("n") for c in msg.get("c", [])} if not msg.get("start") else set()
        if names:
            self.pending = [(t, m) for t, m in self.pending
                            if m.get("start") or not names & {c.get("n") for c in m.get("c", [])}]
        self.pending += [(now + d, msg) for d in self.REPEATS]

    def flush(self):
        now = time.time()
        due = [m for t, m in self.pending if t <= now]
        self.pending = [(t, m) for t, m in self.pending if t > now]
        for m in due:
            send(dict(m))                                        # send() adds host/boot/seq/ts to a copy

    async def loop(self):
        while True:
            try:
                now_list = await asyncio.to_thread(read_facts)
                if self.failing:
                    log.info("Reading container facts again.")
                self.failing = False
                current = {c["n"]: c for c in now_list}
                for name, c in current.items():                   # CPU counts only when high for CPU_FOR_S
                    if (c.get("c") or 0) >= HIGH_CPU:
                        self.cpu_since.setdefault(name, time.time())
                    else:
                        self.cpu_since.pop(name, None)
                    c["b"] = bands(c, self.cpu_since.get(name))
                if not self.started:
                    self.started = True
                    self.known = current
                    down = [c for c in now_list if c["s"] != "running" or c["b"]]   # not running, or something high
                    run = {c["n"]: [c["r"], c.get("id") or ""] for c in now_list if c["s"] == "running"}   # restarts, ID
                    self.news({"t": "cf", "start": True, "run": run, "c": down[:6]})
                    log.info("Container facts: %d running, %d not running%s.", len(run), len(down),
                             f" ({', '.join(c['n'] for c in down)})" if down else "")
                else:
                    for name, c in current.items():
                        old = self.known.get(name)
                        if old is None or any(old.get(k) != c.get(k) for k in ("s", "x", "o", "r", "st", "fi", "img", "b")):
                            self.news({"t": "cf", "c": [c]})
                            log.info("Container %s: %s%s%s.", name, c["s"], f" (exit code {c['x']})" if c["s"] != "running" else "",
                                     f", high: {', '.join(c['b'])}" if c["b"] else "")
                    for name in set(self.known) - set(current):
                        self.news({"t": "cf", "c": [{"n": name, "s": "removed"}]})
                        log.info("Container %s was removed.", name)
                    self.known = current
            except (OSError, ValueError) as e:
                if not self.failing:
                    log.warning("Could not read container facts from %s (%s). Retrying every %d s.", FACTS_URL, e, FACTS_EVERY)
                self.failing = True
            for _ in range(FACTS_EVERY):                         # send repeats on time while waiting
                self.flush()
                await asyncio.sleep(1)


class StatusProtocol(asyncio.DatagramProtocol):
    def __init__(self, mon):
        self.mon = mon

    def datagram_received(self, data, addr):
        self.mon.status(data, addr)


async def main():
    if not TARGET_LIST:
        log.error("No signal-cli to watch. Set SIGNAL_TARGETS, e.g. https://signal:8443")
        raise SystemExit(1)
    await asyncio.to_thread(name_targets)
    mon = Monitor()
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: StatusProtocol(mon), local_addr=STATUS)
    log.info("Starting up as host %s (this run: %s). Watching %s.", HOST, BOOT,
             ", ".join(f"{k} at {v}" for k, v in TARGETS.items()))
    log.info("Listening for subscriber status on UDP port %d. Reporting to %s:%d over UDP every %d s.",
             STATUS[1], DEST[0], DEST[1], HEARTBEAT)
    send({"t": "ev", "comp": "signal-monitor", "code": "MONITOR_STARTED", "sev": "info",
          "text": f"signal-monitor on {HOST} started."})
    loops = [mon.check_loop(), mon.heartbeat_loop()]
    if FACTS_URL:
        mon.facts = Facts()
        loops.append(mon.facts.loop())
        log.info("Reading container facts from %s every %d s.", FACTS_URL, FACTS_EVERY)
    await asyncio.gather(*loops)


def stop_now(*_):
    """docker stop sends SIGTERM; stop at once (as Ctrl-C would) instead of being killed 10 s later."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_now)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
