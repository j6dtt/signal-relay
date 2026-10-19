"""The console's monitoring: the parts of the relay, their states, the event timeline and the diode's health.

Messages come from monitor-receiver (POST /monitor), which passes on what signal-monitor sends across the diode:
  hb  signal-monitor's own heartbeat, every 30 s       {"t":"hb","host","boot","seq","ts","up_s","info"}
  st  one part's state (signal-cli, a subscriber)     {"t":"st",...,"inst","comp","name","state","info","text"}
      every 30 s, and at once when it changes
  ev  something that isn't a state (an error line)    {"t":"ev",...,"inst","comp","name","code","sev","text"}
plus monitor-receiver's own heartbeat: {"t":"rx_hb","ts","info"}. The receiver's counters (POST /stats) are its
heartbeat.

The low side reports states only; this module turns every change of state into an event, in one place. A lost
packet can't lose an alert: the next status carries the state again. A part that stops reporting goes STALE after
STALE_S and DOWN after DOWN_S. If a host's signal-monitor is silent, the parts it reports on are UNKNOWN, not DOWN:
the problem may be the monitor, the host or the diode.

Part keys:  high/receiver/<container name>   high/monitor-receiver   high/console
            low/<host>/signal-monitor   low/<host>/<instance>/signal-cli   low/<host>/<instance>/subscriber/<name>
            low/<host>/<instance>/signal-servers   (signal-cli's link to Signal's servers; image r2 and later)

Application problems (2026-10-18): every app counts its warnings and errors by fixed code ("p", cumulative, so a
lost packet loses nothing) and says what is wrong right now ("active" on the low side, "cond" from the receiver).
The low side decides its parts' states itself (signal-monitor); for the high side this module does (receiver,
the console): a counted problem keeps its part WARN/DOWN for RECENT_S after it last happened. A count that goes up
while the part already shows that code is logged as one more event ("1 more message skipped").
The chat path check: each subscriber reports the last message number it sent and when; the receiver reports, per
sender, the highest number it rebuilt or gave up on. Sent more than ARRIVE_S ago and still not there: the chat
port (6510) or the receiver is losing messages, even when monitoring (6511) gets through.
"""
import hashlib, json, logging, re, shutil, time
from collections import deque
from store import now_ms, BUCKET_MS
from incidents import Incidents

STALE_S, DOWN_S = 90, 180
FLAP_N, FLAP_S = 10, 600                             # more changes than this in this many seconds = flapping
FACTS_EVERY_S = 15                                   # signal-monitor reads container facts this often (FACTS_EVERY)
STATES = ("up", "warn", "down")                      # what the low side may report
BAD = ("warn", "stale", "unknown", "down")
SEV = {"up": "info", "warn": "warning", "stale": "warning", "unknown": "warning", "down": "error"}
NAME = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
CODE = re.compile(r"[^A-Z0-9_]")
LABEL = {"signal-monitor": "signal-monitor", "signal-cli": "signal-cli", "subscriber": "subscriber",
         "receiver": "receiver", "monitor-receiver": "monitor-receiver", "console": "console",
         "signal-servers": "Signal servers"}
RECENT_S = 600                     # a counted problem keeps its part WARN/DOWN this long after it last happened
RES = {"mem": "memory", "cpu": "CPU", "pids": "processes"}
CHECK_FAILED = {"UNREACHABLE", "GATEWAY_CLOSED", "TLS_FAILED", "TIMEOUT", "HTTP_ERROR"}   # signal-cli's check didn't answer
ARRIVE_S = 60                      # a sent message not at the receiver after this long is missing
GIVE_UP_S = 900                    # ... and after this long it is counted as lost and the check moves on
DISK_LOW_PCT = 10
REMOVE_AFTER_S = 3600              # a box can be removed by hand once nothing has been heard from it for this long
GONE_WAIT_S = 120                  # a removed container's boxes go after this long, unless it is back (compose recreate)
TEST_PREFIX = "signal-sim-"        # tools/sim's test containers: their boxes go soon after they are removed (TEST_GONE_S:
TEST_GONE_S = 20                   # not at once, as a test may recreate the same name), and their made-up groups get no window
# Counted problems that change a high-side part's state, worst first: (code, state, text for n of them).
RECEIVER_COUNTED = [
    ("FORWARD_FAILED", "down", "my_forward failed {n} time(s) in the last 10 min: those records did not reach the destination (prod: Gotham)"),
    ("DELIVER_FAILED", "down", "{n} message(s) could not be delivered to the console and were dropped"),
    ("MESSAGE_LOST", "warn", "{n} message(s) lost on the way in the last 10 min (not all their packets arrived)"),
    ("MESSAGE_BROKEN", "warn", "{n} message(s) arrived complete but could not be rebuilt"),
    ("RX_OVERFLOW", "warn", "the kernel dropped {n} packet(s) because the receive buffer was full"),
    ("FORWARD_SLOW", "warn", "my_forward was slow {n} time(s) (over 2 s): nothing is received while it runs"),
    ("BAD_PACKET", "warn", "{n} packet(s) that are not relay packets arrived on the chat port"),
    ("ROOM_MISMATCH", "warn", "{n} packet(s) for a room it doesn't serve arrived and were dropped (the diode's routes?)"),
    ("NOTICE_FAILED", "warn", "{n} lost-message notice(s) could not be passed to the console"),
    ("OTHER", "warn", "{n} unexpected error(s) in its log (see its Docker log)"),
]
CONSOLE_COUNTED = [
    ("DB_ERROR", "down", "{n} database error(s) in the last 10 min (disk full, locked or damaged?): chat or monitoring may not be saved"),
    ("INGEST_FAILED", "warn", "it failed {n} time(s) to take in what the receiver or monitor-receiver sent"),
    ("AI_FAILED", "warn", "the Analyst failed {n} time(s) in the last 10 min"),
]
log = logging.getLogger("console")


def utc_s(ms):
    return time.strftime("%H:%M:%S", time.gmtime(ms / 1000))


def clean(s, n=300):
    """Untrusted text from across the diode: a string, printable, at most n characters."""
    s = "" if s is None else str(s)
    return "".join(c if c.isprintable() else " " for c in s)[:n]


def old_id(known, name):
    """A container's ID from what is known already (the start summary lists running ones by name only)."""
    return (known.get(name) or {}).get("id")


def codes_counts(p):
    """A problem-count dict from across the diode: fixed codes -> whole numbers, at most 40."""
    out = {}
    if isinstance(p, dict):
        for k, v in list(p.items())[:40]:
            if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10**9:
                k = CODE.sub("", k.upper())[:40]
                if k:
                    out[k] = v
    return out


def codes_list(a):
    return [CODE.sub("", str(c).upper())[:40] for c in a[:10]] if isinstance(a, list) else []


# Counted problems worth one event per occurrence (the others are only counted), and those that mean lost messages.
COUNTED = {"SEND_FAILED", "EVENTS_MISSED", "CLI_RESTART_LOSS", "MESSAGE_SKIPPED", "SENDER_ERROR", "WRITE_FAILED",
           "STREAM_STALLED", "PACKET_TOO_BIG", "OTHER", "STORAGE_ERROR", "EVENT_DROPPED", "DECRYPT_FAILED",
           "UNTRUSTED_IDENTITY", "HANDLE_ERROR", "GATEWAY_FULL", "GATEWAY_REFUSED", "RPC_BLOCKED",
           "FORWARD_FAILED", "DELIVER_FAILED", "MESSAGE_LOST", "MESSAGE_BROKEN", "RX_OVERFLOW", "FORWARD_SLOW",
           "BAD_PACKET", "NOTICE_FAILED", "DB_ERROR", "INGEST_FAILED", "AI_FAILED", "SENDER_DEAD", "RX_STALLED"}
LOSS = {"SEND_FAILED", "EVENTS_MISSED", "CLI_RESTART_LOSS", "MESSAGE_SKIPPED", "SENDER_ERROR", "EVENT_DROPPED",
        "FORWARD_FAILED", "DELIVER_FAILED", "MESSAGE_LOST", "MESSAGE_BROKEN", "RX_OVERFLOW", "DB_ERROR", "STORAGE_ERROR",
        "CHAT_LOST"}


def small(info):
    """Keep a status's info dict small and flat: short keys, numbers/short strings/booleans only."""
    out = {}
    if isinstance(info, dict):
        for k, v in list(info.items())[:20]:
            if isinstance(k, str) and NAME.match(k):
                out[k] = v if isinstance(v, (int, float, bool)) or v is None else clean(v, 120)
    return out


class Recent:
    """Cumulative problem counts in, "how many in the last RECENT_S" out (see signal-monitor's Recent)."""
    def __init__(self, last=None, ups=None):
        self.last = last
        self.ups = {k: [tuple(x) for x in v] for k, v in (ups or {}).items()}    # saved with the part: survives a restart

    def update(self, counts, fresh=False):
        """Returns {code: how many more} for this update."""
        now = time.time()
        counts = {k: v for k, v in (counts or {}).items() if isinstance(k, str) and isinstance(v, int)}
        if self.last is None and not fresh:
            self.last = counts
            return {}
        last, more = self.last or {}, {}
        restarted = any(v < last.get(k, 0) for k, v in counts.items())
        for code, n in counts.items():
            d = n if restarted else n - last.get(code, 0)
            if d > 0:
                more[code] = d
                self.ups.setdefault(code, []).append((now, d))
        self.last = counts
        for code in list(self.ups):
            self.ups[code] = [x for x in self.ups[code] if now - x[0] < RECENT_S]
            if not self.ups[code]:
                del self.ups[code]
        return more

    def n(self, code):
        now = time.time()
        return sum(d for t, d in self.ups.get(code, []) if now - t < RECENT_S)

    def active(self, rules):
        return [(c, st, t.format(n=self.n(c))) for c, st, t in rules if self.n(c)]


class Monitor:
    def __init__(self, store, publish, runbook=None):
        self.store, self.publish, self.runbook = store, publish, runbook or {}
        self.parts = store.parts()
        self.seqs = store.get("mon_seq", {})           # host -> {"boot", "seq"}: for counting lost packets
        self.events = deque(store.mon_events(limit=300), maxlen=300)
        self.started = time.time()
        self.dirty = True
        self.links, self.links_at = {}, 0
        self.removed = store.get("mon_removed", {})       # part key -> when removed; its "still down" reports are ignored
        self.arrivals = store.get("mon_arrivals", {})     # "<session>/<group ID start>" -> [events arrived, last (ms)]
        self.group_names = store.get("mon_groups", {})    # group ID -> name (from the events' groupInfo): no content
        self.gone = {}                                     # (host, container) -> when container-facts said removed
        self._part("high/console", side="high", comp="console", name="console", state="up",
                   info={"started": now_ms()}, last=now_ms())
        self.incidents = Incidents(store, self.runbook, lambda part, code, sev, text: self._event(part, code, sev, text))
        self.containers = store.get("mon_containers", {})   # host -> container name -> facts (from container-facts)
        self.facts_ok = {}                              # host -> False when the heartbeat's fingerprint disagrees
        self.changes = {}                               # part key -> recent change times (s), for the flapping guard
        self.metrics_at = 0
        self.recent = {}                                # high-side part key -> Recent
        self.health = {"p": {}, "tick_ok_all": time.time(), "tick_fail": 0, "lag": 0.0, "lag_at": 0}   # the console's own
        self.disk_path = None                           # the console's database folder, for its disk check
        self.alive_at = 0
        self.accounted = store.get("mon_accounted", {})    # sender session -> highest message number counted as lost

    # ---- parts ----
    def _part(self, key, **fields):
        p = self.parts.get(key)
        if p is None:
            p = self.parts[key] = {"key": key, "state": fields.get("state"), "since": now_ms(),   # None: not shown yet
                                   "reported": "up", "last": now_ms(), "info": {}, "text": ""}
            new = True
        else:
            new = False
        for k, v in fields.items():
            if k != "state" or new:
                p[k] = v
        self.store.put_part(key, p)
        self.dirty = True
        return p, new

    def _more(self, key, old_code, more):
        """Counted problems that went up but didn't change the part's code (already shown, or a worse one shown):
        one event each, so every occurrence is in the log and the incident's timeline."""
        p = self.parts.get(key)
        if not p or not more:
            return
        for code, d in more.items():
            if code not in COUNTED or (code == p.get("code") and code != old_code):
                continue                                 # unknown noise, or the state change just announced it
            title = (self.runbook.get(code) or {}).get("title") or code.replace("_", " ").lower()
            self._event(key, code, "error" if code in LOSS else "warning",
                        f"{self.title(p)}: {title} ({d} more).")

    def touch(self, key, info=None, **fields):
        """A heartbeat from a part checked directly (receiver, monitor-receiver, a signal-monitor)."""
        if key.startswith("high/receiver/") and isinstance(info, dict):   # chat traffic: evidence the low side and diode work
            old = (self.parts.get(key) or {}).get("info", {}).get("packets")
            new = info.get("packets")
            if isinstance(new, int) and isinstance(old, int) and new > old:
                fields["traffic_at"] = now_ms()
        self._part(key, last=now_ms(), info=small(info) if info is not None else
                   self.parts.get(key, {}).get("info", {}), **fields)

    # ---- the receiver (its counters, POST /stats, are its heartbeat) and the console itself ----
    def receiver(self, stats, name):
        """Counters from one receiver (name: its container name, found by the console from its address)."""
        key = f"high/receiver/{name}"
        self._still_removed(key, "up")                    # it sent counters: it is alive, so back on the diagram
        old = self.parts.get(key) or {}
        old_code = old.get("code")
        p = codes_counts(stats.get("p"))
        if isinstance(stats.get("rx_drops"), int):
            p["RX_OVERFLOW"] = stats["rx_drops"]          # the kernel's count for the receiver's own network
        started = stats.get("started") if isinstance(stats.get("started"), int) else None
        r = self.recent.setdefault(key, Recent(old.get("p"), old.get("ups")))
        more = r.update(p, fresh=bool(started and now_ms() - started < 60_000))
        sess = {}
        for s_, row in list((stats.get("sess") or {}).items())[:16] if isinstance(stats.get("sess"), dict) else []:
            if isinstance(s_, str) and len(s_) <= 64 and isinstance(row, list) and len(row) == 3 and all(isinstance(x, int) for x in row):
                sess[s_] = row
        flat = {k: v for k, v in stats.items() if k not in ("p", "sess", "cond", "started", "sess_since", "name", "rooms",
                                                             "room_stats", "wrong_room", "sess_room")}
        rooms = [r for r in stats.get("rooms") or [] if isinstance(r, str) and len(r) <= 32][:20]
        if rooms:                                         # on its box: the rooms it serves, by their real names
            flat["rooms"] = ", ".join(self.room_title(r) for r in rooms if r)
        room_stats = {r: small(v) for r, v in list((stats.get("room_stats") or {}).items())[:20]
                      if isinstance(r, str) and len(r) <= 32 and isinstance(v, dict)}
        wrong = {r: n for r, n in list((stats.get("wrong_room") or {}).items())[:20]
                 if isinstance(r, str) and len(r) <= 32 and isinstance(n, int)}
        sess_room = {s_: r for s_, r in list((stats.get("sess_room") or {}).items())[:64]
                     if isinstance(s_, str) and len(s_) <= 64 and isinstance(r, str) and len(r) <= 32}
        since = stats.get("sess_since") if isinstance(stats.get("sess_since"), int) else started
        self.touch(key, flat, side="high", comp="receiver", name=name, p=p, sess=sess, started=started, ups=r.ups,
                   rooms=rooms, room_stats=room_stats, wrong_room=wrong, sess_room=sess_room,
                   sess_since=since,
                   cond=codes_list(stats.get("cond")), stalled_in=clean(stats.get("stalled_in"), 60))
        self._right_state(key)
        self.evaluate()
        self._more(key, old_code, more)

    def _right_state(self, key):
        """A high-side part's own state (receiver, the console): conditions first, then counted problems."""
        p = self.parts.get(key)
        if not p:
            return
        info, r, active = p.get("info") or {}, self.recent.setdefault(key, Recent(p.get("p"), p.get("ups"))), []
        if p.get("comp") == "receiver":
            if "RX_STALLED" in (p.get("cond") or []):
                active.append(("RX_STALLED", "down", f"its receiving loop is stuck (in {p.get('stalled_in') or '?'}): nothing is received"))
            missing = self.chat_missing.get(key) or []
            if missing:                                   # clock times only: the text must not change on every check
                sub, m, done, sent_at, room = missing[0]
                more = len(missing) - 1
                active.append(("CHAT_NOT_ARRIVING", "down", f"{self.title(sub)} sent message #{m}"
                               f"{f' to {self.room_title(room)}' if room else ''} at {utc_s(sent_at)} UTC; "
                               f"its last from that run is #{done}" + (f" (and {more} more)" if more else "")
                               + ". Monitoring still gets through, so the chat port or the receiver is losing messages"))
            active += r.active(RECEIVER_COUNTED)
        else:
            h = self.health
            if h["tick_fail"] and time.time() - h["tick_ok_all"] > 15:
                active.append(("CHECKS_FAILING", "down", f"part of its monitoring checks failed {h['tick_fail']} time(s) "
                               "in a row: the Monitor tab may be out of date (see the console's log)"))
            active += r.active(CONSOLE_COUNTED)
            if h["lag"] > 2 and time.time() - h["lag_at"] < 300:
                active.append(("LOOP_LAG", "warn", f"it was blocked for {h['lag']:.1f} s (slow disk?): pages and intake wait meanwhile"))
        disk = info.get("disk_free_pct")
        if isinstance(disk, (int, float)) and disk < DISK_LOW_PCT:
            active.append(("DISK_LOW", "warn", f"its disk has {disk}% free"))
        active.sort(key=lambda a: a[1] != "down")         # DOWN first, otherwise in the order above
        new = ((active[0][1], active[0][0], active[0][2]) if active else ("up", "OK", ""), [a[0] for a in active])
        if new != ((p.get("reported"), p.get("code"), p.get("text")), p.get("active")):
            (p["reported"], p["code"], p["text"]), p["active"] = new
            self.store.put_part(key, p)
            self.dirty = True

    @staticmethod
    def streams(p):
        """A subscriber's per-room streams [room, session, last message number, its age], checked (stored parts
        from before may hold anything)."""
        rooms = (p.get("flow") or {}).get("rooms")
        return [r for r in rooms if isinstance(r, list) and len(r) == 4] if isinstance(rooms, list) else []

    def room_titles(self):
        """{room: its real name}: the names of the Signal groups the subscribers send to it ("Signal Relay"; several
        joined with " + "). "Monitoring" for the monitoring route. A room nobody reports sending to keeps its short name."""
        names = {gid[:8]: name for gid, name in self.group_names.items()}
        rooms = {}
        for p in self.parts.values():
            if p.get("comp") == "subscriber":
                for g8, room, _, _ in (p.get("flow") or {}).get("g") or []:
                    if isinstance(room, str) and isinstance(g8, str) and names.get(g8):
                        rooms.setdefault(room, [])
                        if names[g8] not in rooms[room]:
                            rooms[room].append(names[g8])
        return {"MONITOR": "Monitoring", **{r: " + ".join(sorted(n)) for r, n in rooms.items()}}

    def room_title(self, room):
        return self.room_titles().get(room, room)

    def receivers(self):
        return {k: p for k, p in self.parts.items() if p.get("comp") == "receiver"}

    def _chat_path(self):
        """Messages a subscriber sent that no receiver has rebuilt (or given up on) after ARRIVE_S, per receiver key.
        Each room a subscriber sends to has its own session; it is followed at the receiver(s) that have seen that
        session, else at the receivers that serve the room, else at every receiver. Also sets each subscriber's
        "to": the receivers it feeds (for the diagram's wires)."""
        rxs = self.receivers()
        missing = {}
        now = now_ms()
        for key, p in list(self.parts.items()):
            if p.get("comp") != "subscriber":
                continue
            f = p.get("flow") or {}
            streams = self.streams(p) or [[None, f.get("sess"), f.get("m"), f.get("m_age")]]   # older subscribers: one
            to_all = []
            for room, s_, m, m_age in streams:
                holders = [k for k, r in sorted(rxs.items()) if isinstance(s_, str) and s_ in (r.get("sess") or {})]
                serving = [k for k, r in sorted(rxs.items()) if room is not None and room in (r.get("rooms") or [])]
                live = [k for k in serving if rxs[k].get("state") not in ("stale", "down")]
                serving = live or serving                 # a receiver gone silent doesn't take the wire from a live one
                to = holders or serving or sorted(rxs)
                to_all += [k for k in to if k not in to_all]
                if not to or not (isinstance(s_, str) and isinstance(m, int) and m > 0 and isinstance(m_age, int)):
                    continue
                if "SEND_FAILED" in (p.get("active") or []):
                    continue                              # it never left the host: the subscriber's own SEND_FAILED says so
                sent_at = p["last"] - m_age * 1000
                done = max((rxs[k].get("sess") or {}).get(s_, [0])[0] for k in to)
                # since when the receivers' progress is known: their start, or earlier if they kept a progress file
                started = min(rxs[k].get("sess_since") or rxs[k].get("started") or 0 for k in to)
                counted_ = self.accounted.get(s_, 0)
                if sent_at < started + 5000 or done >= m or counted_ >= m:
                    continue                              # before the receiver started, arrived, or already counted
                wait = (now - sent_at) / 1000
                where = to[:1] if holders else to
                if wait > GIVE_UP_S:
                    self.accounted[s_] = m
                    self.store.put("mon_accounted", dict(list(self.accounted.items())[-50:]))
                    for k in where:
                        self._event(k, "CHAT_LOST", "error",
                                    f"{m - max(done, counted_)} message(s) sent by {self.title(p)}"
                                    f"{f' to {self.room_title(room)}' if room else ''} (up to #{m}, at {utc_s(sent_at)} UTC) "
                                    f"never reached {self.title(rxs[k])}: they are lost.")
                elif wait > ARRIVE_S:
                    for k in where:
                        missing.setdefault(k, []).append((p, m, done, sent_at, room))
            if p.get("to") != to_all:
                p["to"], self.dirty = to_all, True
        self._subscriber_checks(rxs)
        return missing

    def _subscriber_checks(self, rxs):
        """The console's own checks on subscribers (it sees all hosts and every receiver, signal-monitor doesn't):
        a room no receiver serves, and the same routes.conf name running in two places. Kept as "checks" on the
        part, shown over what the subscriber reported (see shown())."""
        live = {k: p for k, p in self.parts.items() if p.get("comp") == "subscriber" and p.get("state") not in ("stale", "down")}
        served = {r for x in rxs.values() for r in (x.get("rooms") or [])}
        by_rf = {}
        for k, p in live.items():
            rf = (p.get("flow") or {}).get("rf")
            if rf:
                by_rf.setdefault(rf, []).append(k)
        for k, p in self.parts.items():
            if p.get("comp") != "subscriber":
                continue
            checks = []
            twice = [o for o in by_rf.get((p.get("flow") or {}).get("rf"), []) if o != k] if k in live else []
            if twice:
                other = self.parts[twice[0]]
                checks.append(("SUBSCRIBER_TWICE", "warn", f"its routes ({p['flow']['rf']} in routes.conf) also run in "
                               f"{self.title(other)}: every message is sent twice"))
            unserved = [r[0] for r in self.streams(p) if rxs and r[0] and r[0] not in served]
            if unserved:
                checks.append(("ROOM_UNSERVED", "warn", f"no receiver serves room{'s' if len(unserved) > 1 else ''} "
                               f"{', '.join(self.room_title(u) for u in unserved)}: its messages there go nowhere (a diode route missing, or the "
                               "receiver not set up)"))
            if p.get("checks", []) != checks:
                p["checks"], self.dirty = checks, True

    def shown(self, p):
        """A part as the page, the bell and incidents see it: a console check on a subscriber that reported UP
        shows as its code and text (the subscriber's own report wins when it has a problem itself)."""
        c = p.get("checks")
        if not c or p.get("reported", "up") != "up" or p.get("state") in ("stale", "down", "unknown"):
            return p
        return {**p, "code": c[0][0], "text": c[0][2], "active": [x[0] for x in c] + list(p.get("active") or [])}

    # ---- messages from monitor-receiver ----
    def handle(self, m):
        """One message (dict). Returns None if accepted, else a short reason."""
        t = m.get("t")
        if t == "rx_hb":
            self.touch("high/monitor-receiver", m.get("info"), side="high", comp="monitor-receiver",
                       name="monitor-receiver")
            return None
        host = m.get("host")
        if t not in ("hb", "st", "ev", "cf") or not (isinstance(host, str) and NAME.match(host)):
            return "unknown message type or bad host name"
        try:
            seq, ts, rx = int(m["seq"]), int(m["ts"]), int(m.get("rx") or now_ms())
            boot = clean(m["boot"], 32)
        except (KeyError, TypeError, ValueError):
            return "missing seq/ts/boot"
        self._count(host, boot, seq, ts, rx)
        mkey = f"low/{host}/signal-monitor"
        if t == "cf":
            if mkey in self.parts:
                self.parts[mkey]["last"] = now_ms()
            return self._containers(host, m.get("c"), m.get("run") if m.get("start") else None)
        if t == "hb":
            self._still_removed(mkey, "up")
            if isinstance(m.get("cfh"), str):
                self._check_facts(host, m["cfh"][:16])
            info = small(m.get("info"))
            info["up_s"] = m.get("up_s") if isinstance(m.get("up_s"), int) else None
            self.touch(mkey, info, side="low", host=host, comp="signal-monitor", name="signal-monitor",
                       reported=m.get("state") if m.get("state") in STATES else "up", text=clean(m.get("text"), 200),
                       code=CODE.sub("", clean(m.get("code"), 40).upper()) or None)
            return None
        if mkey not in self.parts:                       # a status before the first heartbeat still means alive
            self.touch(mkey, {}, side="low", host=host, comp="signal-monitor", name="signal-monitor")
        else:
            self.parts[mkey]["last"] = now_ms()
        inst, comp, name = m.get("inst"), m.get("comp"), m.get("name") or m.get("comp")
        if t == "ev" and comp == "signal-monitor":       # about the monitor itself (e.g. it started)
            sev = m.get("sev") if m.get("sev") in ("info", "warning", "error", "critical") else "warning"
            code = re.sub(r"[^A-Z0-9_]", "", clean(m.get("code"), 40).upper()) or "EVENT"
            self._event(mkey, code, sev, clean(m.get("text")), notify=sev in ("error", "critical"))
            return None
        if not all(isinstance(x, str) and NAME.match(x) for x in (inst, comp, name)) or \
                comp not in ("signal-cli", "subscriber", "signal-servers"):
            return "bad instance/component/name"
        key = (f"low/{host}/{inst}/subscriber/{name}" if comp == "subscriber" else f"low/{host}/{inst}/{comp}")
        if t == "st":
            state = m.get("state")
            if state not in STATES:
                return "bad state"
            # signal-monitor repeats a vanished signal-cli's last "Signal servers" state: it follows its signal-cli
            follows = comp == "signal-servers" and f"low/{host}/{inst}/signal-cli" in self.removed
            if self._still_removed(key, "down" if follows else state):
                return None                               # signal-monitor still lists it as down: it stays removed
            old = self.parts.get(key) or {}
            old_code = old.get("code")                   # before _part updates the same dict
            p_new = codes_counts(m.get("p"))
            more = Recent(old.get("p")).update(p_new) if old else {}
            self._part(key, side="low", host=host, inst=inst, comp=comp, name=name, last=now_ms(),
                       reported=state, info=small(m.get("info")), text=clean(m.get("text"), 200),
                       code=CODE.sub("", clean(m.get("code"), 40).upper()) or None,
                       active=codes_list(m.get("active")), p=p_new,
                       flow=small({k: v for k, v in m["flow"].items() if k not in ("rooms", "lw")}) if isinstance(m.get("flow"), dict) else {},
                       cid=clean(m.get("cid"), 12) or None)
            if isinstance((m.get("flow") or {}).get("lw"), list):
                self.parts[key]["flow"]["lw"] = [x for x in m["flow"]["lw"][:2] if isinstance(x, int)]
            groups = (m.get("flow") or {}).get("g")         # per group: [ID start, room, sent this run, seconds since]
            if isinstance(groups, list):
                self.parts[key]["flow"]["g"] = [[clean(g[0], 8), clean(g[1], 32), g[2], g[3]] for g in groups[:12]
                    if isinstance(g, list) and len(g) == 4 and isinstance(g[2], int) and (g[3] is None or isinstance(g[3], int))]
            rooms = (m.get("flow") or {}).get("rooms")      # per room: [room, session, last message number, its age]
            if isinstance(rooms, list):
                self.parts[key]["flow"]["rooms"] = [
                    [clean(r[0], 32), clean(r[1], 16), r[2], r[3]] for r in rooms[:12]
                    if isinstance(r, list) and len(r) == 4 and isinstance(r[2], int) and (r[3] is None or isinstance(r[3], int))]
            self.store.put_part(key, self.parts[key])     # with the lists set above
            self.evaluate()
            self._more(key, old_code, more)
            return None
        sev = m.get("sev") if m.get("sev") in ("info", "warning", "error", "critical") else "warning"
        code = re.sub(r"[^A-Z0-9_]", "", clean(m.get("code"), 40).upper()) or "EVENT"
        if key in self.removed and key not in self.parts:
            return None                                   # news about a removed box: its log keeps what came before
        if key not in self.parts:
            self._part(key, side="low", host=host, inst=inst, comp=comp, name=name, last=now_ms())
        self._event(key, code, sev, clean(m.get("text")), notify=sev in ("error", "critical"))
        return None

    # ---- container facts (low side: container-facts -> signal-monitor -> here) ----
    @staticmethod
    def how_it_ended(c):
        """Plain words for why a container is not running, from Docker's exit code and OOM flag."""
        x = c.get("x")
        if c.get("o"):
            return "was killed for using too much memory (out of memory, OOM)"
        if x == 0:
            return "stopped cleanly (exit code 0): stopped on purpose (docker stop, compose stop/down) or its program ended"
        if x == 143:
            return "stopped by a stop signal (exit code 143, SIGTERM): docker stop"
        if x == 137:
            return "was killed (exit code 137, SIGKILL): docker kill, or docker stop after its 10 s grace"
        return f"crashed (exit code {x})"

    @staticmethod
    def usage_text(c, b):
        return {"mem": f"memory {c.get('m')}% of its limit", "pids": f"processes {c.get('pi')}% of its limit",
                "cpu": f"CPU over 90% for 5 min (now {c.get('c')}%)"}[b]

    @staticmethod
    def facts_fingerprint(containers):
        """Same as signal-monitor's: name, state and restart count of every container."""
        rows = sorted([c["n"], c["s"], c.get("r", 0)] for c in containers)
        return hashlib.sha1(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()[:8]

    def _check_facts(self, host, fp):
        """The heartbeat says what signal-monitor knows; if it differs, a cf message was missed (all 3 copies)."""
        ok = self.facts_fingerprint(self.containers.get(host, {}).values()) == fp
        if self.facts_ok.get(host) != ok:
            self.facts_ok[host] = ok
            self.dirty = True
            if not ok:
                log.warning("Container facts from %s may be out of date: a change was missed. They will be right again "
                            "at the next change, or when signal-monitor restarts.", host)

    def _containers(self, host, items, run=None):
        """A cf message: news about single containers, or (run given) the summary signal-monitor sends at its start."""
        if not isinstance(items, list):
            return "container facts are not a list"
        known = self.containers.setdefault(host, {})
        if isinstance(run, dict):                        # start summary: these are running, the others are in items
            listed = {c.get("n") for c in items if isinstance(c, dict)}
            for name, restarts in list(run.items())[:40]:
                if isinstance(name, str) and NAME.match(name):
                    old = known.get(name) or {}
                    rid = None
                    if isinstance(restarts, list) and len(restarts) == 2:      # [restarts, container ID] since 2026-10-18
                        restarts, rid = restarts[0], clean(restarts[1], 12) or None
                    items = items + [{**old, "n": name, "s": "running", "r": restarts if isinstance(restarts, int) else 0,
                                      "b": [], "id": rid or old.get("id")}]   # a running one with something high is in items
            for name in [n for n in known if n not in run and n not in listed]:
                del known[name]                          # gone since signal-monitor last ran
                self.gone[(host, name)] = now_ms()
        for c in items[:20]:
            if not (isinstance(c, dict) and isinstance(c.get("n"), str) and NAME.match(c["n"])):
                continue
            num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else None
            c = {"n": c["n"], "s": clean(c.get("s"), 20), "x": c.get("x") if isinstance(c.get("x"), int) else None,
                 "o": bool(c.get("o")), "r": c.get("r") if isinstance(c.get("r"), int) else 0,
                 "st": c.get("st") if isinstance(c.get("st"), int) else None,
                 "fi": c.get("fi") if isinstance(c.get("fi"), int) else None, "h": clean(c.get("h"), 20) or None,
                 "img": clean(c.get("img"), 80) or None, "m": num(c.get("m")), "c": num(c.get("c")), "pi": num(c.get("pi")),
                 "id": clean(c.get("id"), 12) or old_id(known, c["n"]),
                 "b": [b for b in (c.get("b") or []) if b in ("mem", "cpu", "pids")] if isinstance(c.get("b"), list) else []}
            old = known.get(c["n"])
            if old and c["s"] != "removed" and max(c["st"] or 0, c["fi"] or 0) < max(old.get("st") or 0, old.get("fi") or 0):
                continue                                 # an older copy arriving late: every change moves a start or end time
            if c["s"] == "removed":
                known.pop(c["n"], None)
                self.gone.setdefault((host, c["n"]), now_ms())
                continue
            known[c["n"]] = c
            if old is None:
                continue
            key = f"low/{host}/container/{c['n']}"
            for b in set(c["b"]) - set(old.get("b") or []):
                self._event(key, f"CONTAINER_{b.upper()}_HIGH", "warning", f"Container {c['n']} on {host}: {self.usage_text(c, b)}.")
            for b in set(old.get("b") or []) - set(c["b"]):
                self._event(key, f"CONTAINER_{b.upper()}_OK", "info", f"Container {c['n']} on {host}: {RES[b]} back to normal.")
            if c.get("img") and old.get("img") and c["img"] != old["img"]:
                self._event(key, "CONTAINER_IMAGE_CHANGED", "info", f"Container {c['n']} on {host} now runs image {c['img']} "
                            f"(was {old['img']}).")
            if c["s"] != old.get("s"):
                if c["s"] == "running":
                    self._event(key, "CONTAINER_STARTED", "info", f"Container {c['n']} on {host} started.")
                elif c["s"] in ("exited", "dead"):
                    self._event(key, "CONTAINER_OOM" if c["o"] else "CONTAINER_EXITED", "error" if c["o"] or c["x"] not in (0, 143) else "warning",
                                f"Container {c['n']} on {host} {self.how_it_ended(c)}.")
                else:
                    self._event(key, f"CONTAINER_{c['s'].upper()}", "warning", f"Container {c['n']} on {host} is {c['s']}.")
            elif c["r"] > (old.get("r") or 0):
                n = c["r"] - (old.get("r") or 0)          # Docker counts from the last manual start (start resets it)
                self._event(key, "CONTAINER_RESTARTED", "error",
                            f"Container {c['n']} on {host} crashed and Docker restarted it "
                            f"({'once' if n == 1 else f'{n} times'} in the last {FACTS_EVERY_S} s; {c['r']} since it was last started"
                            f"{'' if c['r'] < 3 else ': it keeps failing'}). Its log says why.")
        self.store.put("mon_containers", self.containers)
        self._attach(host)
        return None

    def _attach(self, host):
        """Give each part the facts of its container: a subscriber's container has its name; signal-cli's is
        signal-cli (or the only container starting with signal-cli)."""
        known = self.containers.get(host, {})
        clis = [n for n in known if n.startswith("signal-cli")]
        for p in self.parts.values():
            if p.get("host") != host:
                continue
            name = {"signal-monitor": "signal-monitor",
                    "signal-cli": "signal-cli" if "signal-cli" in known else clis[0] if len(clis) == 1 else None
                    }.get(p.get("comp"), p.get("name"))
            if p.get("cid"):                             # a subscriber says its container ID: matches whatever its name
                name = next((n for n, c in known.items() if c.get("id") == p["cid"]), name)
            if name in known and p.get("container") != known[name]:
                p["container"] = known[name]
                self.store.put_part(p["key"], p)
                self.dirty = True

    def _count(self, host, boot, seq, ts, rx):
        """Packet loss and delay across the diode, from signal-monitor's numbering."""
        last = self.seqs.get(host)
        if last and last["boot"] == boot:
            if seq <= last["seq"]:
                return                                   # a duplicate or out of order: counted already
            expected = seq - last["seq"]
        else:
            expected = 1                                 # first message, or signal-monitor restarted
        self.seqs[host] = {"boot": boot, "seq": seq}
        self.store.put("mon_seq", self.seqs)
        delay = rx - ts
        self.store.add_link(host, rx, 1, expected, delay if -3600_000 < delay < 3600_000 else None)
        if expected > 1:
            log.warning("%d monitoring packet(s) from %s never arrived.", expected - 1, host)

    # ---- states ----
    def effective(self, p, now):
        """(state shown for a part, True if it comes from silence rather than from what the part reported)."""
        if p["comp"] == "console":
            return p.get("reported", "up"), False
        age = (now - p["last"]) / 1000
        if p["side"] == "low" and p["comp"] != "signal-monitor":
            mon = self.parts.get(f"low/{p['host']}/signal-monitor")
            if mon and mon["state"] in ("stale", "down", "unknown"):
                return "unknown", True
        if p["comp"] == "signal-servers":                # its news comes with signal-cli's check: none while that fails
            cli = self.parts.get(f"low/{p['host']}/{p['inst']}/signal-cli")
            if cli and (cli["state"] in ("stale", "unknown") or cli.get("code") in CHECK_FAILED):
                return "unknown", True
        if age > DOWN_S:
            return "down", True
        if age > STALE_S:
            return "stale", True
        if p.get("reported", "up") == "up" and (p.get("container") or {}).get("b"):
            return "warn", False                         # its container is near its limits (container facts)
        if p.get("reported", "up") == "up" and p.get("checks"):
            return p["checks"][0][1], False              # a console check (no receiver for a room, running twice)
        return p.get("reported", "up"), False

    def evaluate(self):
        """Compare every part's shown state with the last one; record each change as an event."""
        now = now_ms()
        grace = time.time() - self.started < STALE_S     # just started: don't call parts down before they could report
        for key in sorted(self.parts, key=lambda k: (k.count("/"), k)):     # monitors before the parts they report on
            p = self.parts[key]
            new, silent = self.effective(p, now)
            if new == p["state"] or (grace and silent):
                continue
            old, p["state"], p["since"] = p["state"], new, now
            self.store.put_part(key, p)
            self.dirty = True
            if self._flapping(key, p, old, new):
                continue                                 # one "flapping" event, not one per change
            why = ({"stale": f"nothing heard for {STALE_S} s", "down": f"nothing heard for {DOWN_S // 60} min",
                    "unknown": "signal-cli is not answering, so its link to Signal is unknown" if p["comp"] == "signal-servers"
                    else "its signal-monitor is silent, so its state is unknown"}.get(new) if silent
                   else self.shown(p).get("text") or "; ".join("its container's " + self.usage_text(p["container"], b)
                                                   for b in (p.get("container") or {}).get("b", []))) or ""
            text = f"{self.title(p)} is {new.upper()}" + (f": {why}" if why else "") + (f" (was {old.upper()})." if old else ".")
            self._event(key, f"{p['comp'].upper().replace('-', '_')}_{new.upper()}", SEV[new], text,
                        notify=new in ("down", "unknown") or (new == "up" and old in ("down", "unknown", "stale")))

    def _flapping(self, key, p, old, new):
        """The flapping guard: True if this change should not get its own event."""
        if old is None:
            return False
        t = time.time()
        recent = [x for x in self.changes.get(key, []) if t - x < FLAP_S] + [t]
        self.changes[key] = recent
        if p.get("flapping"):
            p["flap_count"] = p.get("flap_count", 0) + 1
            return True
        if len(recent) > FLAP_N:
            p["flapping"], p["flap_count"] = True, len(recent)
            self.store.put_part(key, p)
            self._event(key, f"{p['comp'].upper().replace('-', '_')}_FLAPPING", "warning",
                        f"{self.title(p)} is flapping: {len(recent)} changes of state in {FLAP_S // 60} min (now "
                        f"{new.upper()}). Further changes are not logged one by one until it settles.")
            return True
        return False

    def _settle(self):
        """A flapping part that hasn't changed for FLAP_S is logged once more, with where it ended up."""
        t = time.time()
        for key, p in self.parts.items():
            if p.get("flapping") and t - max(self.changes.get(key, [0])) >= FLAP_S:
                n = p.pop("flap_count", 0)
                p["flapping"] = False
                self.store.put_part(key, p)
                self.dirty = True
                self._event(key, f"{p['comp'].upper().replace('-', '_')}_SETTLED", "info",
                            f"{self.title(p)} settled at {str(p['state']).upper()} after {n} changes while flapping.")

    def metrics(self):
        """Once a minute: each part's state and figures, kept 48 h, so the AI can see the run-up to a problem."""
        now = now_ms()
        if now - self.metrics_at < 60_000:
            return
        self.metrics_at = now
        minute = now - now % 60_000
        rows = {}
        for key, p in self.parts.items():
            if p.get("state"):
                info = {k: v for k, v in (p.get("info") or {}).items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
                rows[key] = {"s": p["state"], **info}
        for host, l in self.link_summary().items():
            rows[f"link/{host}"] = {"s": l.get("state"), "loss_1h": l.get("loss_1h"), "delay_ms": l.get("delay_ms")}
        self.store.add_metrics(minute, rows)

    def title(self, p):
        # "(on sighost)", or "(signal-cli-2 on sighost)" for a part of a signal-cli whose container isn't plain "signal-cli"
        inst = p.get("inst") if p.get("inst") not in (None, "signal-cli") else None
        where = f" ({inst} on {p['host']})" if inst else f" (on {p['host']})" if p.get("host") else ""
        name = p["name"] if p["comp"] in ("subscriber", "receiver") else LABEL.get(p["comp"], p["comp"])
        sub = p["comp"] == "subscriber" and "subscriber" not in name      # "signal-subscriber" says it already
        return f"{'subscriber ' if sub else ''}{name}{where}"

    def _event(self, part, code, sev, text, notify=False):
        ev = {"at": now_ms(), "part": part, "code": code, "sev": sev, "text": text, "notify": notify}
        ev["id"] = self.store.add_mon_event({**ev, "data": {"notify": notify}})
        self.events.append(ev)
        (log.warning if sev in ("warning", "error", "critical") else log.info)("Monitoring: %s", text)
        self.publish({"op": "mon_event", "event": ev})

    def forget(self, key):
        """Drop a part that no longer exists (e.g. a removed subscriber)."""
        if self.parts.pop(key, None):
            self.store.remove_part(key)
            self.recent.pop(key, None)
            self.dirty = True

    # ---- removing boxes: automatically when container-facts says a container was removed, or by hand ----
    def _still_removed(self, key, state):
        """True if key was removed and this report says it is still down (ignore it). Any other report brings it back."""
        if key not in self.removed:
            return False
        if state == "down":
            return True
        del self.removed[key]
        self.store.put("mon_removed", self.removed)
        return False

    def _remove(self, keys, part, code, text):
        for k in keys:
            self.forget(k)
            self.removed[k] = now_ms()
        self.removed = dict(sorted(self.removed.items(), key=lambda kv: kv[1])[-200:])
        self.store.put("mon_removed", self.removed)
        self.links_at = 0                                 # a removed host's diode link goes too
        self._event(part, code, "info", text)

    def removable(self, key):
        """(keys to remove, None) if the box can be removed by hand, else (None, why not). A low-side host's
        signal-monitor (or its diode link) takes the whole host with it."""
        host = key[5:] if key.startswith("link/") else None
        if key.startswith("low/") and key.endswith("/signal-monitor") and key.count("/") == 2:
            host = key.split("/")[1]
        if host:
            keys = [k for k, p in self.parts.items() if p.get("side") == "low" and p.get("host") == host]
            p = self.parts.get(f"low/{host}/signal-monitor")
        else:
            keys, p = [key], self.parts.get(key)
        if not p or not keys:
            return None, "there is no such box"
        if key == "high/console":
            return None, "the console can't remove itself"
        quiet = (now_ms() - (p.get("last") or 0)) / 1000
        if quiet < REMOVE_AFTER_S:
            return None, (f"only a box not heard from for over {REMOVE_AFTER_S // 3600} h can be removed "
                          f"(this one was heard {int(quiet // 60)} min ago)")
        return keys, None

    def remove_by_hand(self, key, user):
        keys, why = self.removable(key)
        if not keys:
            return why
        p = self.parts.get(key) or self.parts.get(f"low/{key[5:]}/signal-monitor")
        if len(keys) > 1:
            host = p["host"]
            self._remove(keys, f"low/{host}/signal-monitor", "HOST_REMOVED",
                         f"Removed host {host} from the diagram, with its {len(keys)} boxes (by {user}). Its history stays.")
        else:
            self._remove(keys, key, "PART_REMOVED", f"Removed {self.title(p)} from the diagram (by {user}). Its history stays.")
        return None

    def _step_gone(self):
        """Boxes of containers that container-facts reported removed, once they have not come back for GONE_WAIT_S."""
        now = now_ms()
        for (host, name), at in list(self.gone.items()):
            if name in self.containers.get(host, {}):
                del self.gone[(host, name)]               # back (e.g. recreated by docker compose up)
                continue
            if now - at < (TEST_GONE_S if name.startswith(TEST_PREFIX) else GONE_WAIT_S) * 1000:
                continue
            del self.gone[(host, name)]
            keys = [k for k, p in self.parts.items() if p.get("side") == "low" and p.get("host") == host and (
                (p.get("comp") == "subscriber" and (p.get("name") == name or (p.get("container") or {}).get("n") == name))
                or (p.get("comp") in ("signal-cli", "signal-servers") and p.get("inst") == name))]
            if keys:
                first = self.parts[keys[0]]
                self._remove(keys, keys[0], "PART_REMOVED",
                             f"Removed {self.title(first)} from the diagram: its container {name} was removed."
                             + (f" ({len(keys)} boxes)" if len(keys) > 1 else "") + " Its history stays.")

    # ---- for the page ----
    def link_summary(self):
        if time.time() - self.links_at > 30:
            now = now_ms()
            self.links = {}
            for host in sorted({p["host"] for p in self.parts.values() if p.get("side") == "low" and p.get("host")}):
                rows = self.store.link(host, now - 24 * 3600_000)
                hours = []
                for h in range(24):
                    start = now - (24 - h) * 3600_000
                    rs = [r for r in rows if start <= r["bucket"] < start + 3600_000]
                    exp = sum(r["expected"] for r in rs)
                    hours.append(round(100 * (exp - sum(r["received"] for r in rs)) / exp, 1) if exp else None)
                last = [r for r in rows if r["bucket"] >= now - 3600_000]
                exp, rec = sum(r["expected"] for r in last), sum(r["received"] for r in last)
                dn = sum(r["delay_n"] for r in last)
                self.links[host] = {"loss_1h": round(100 * (exp - rec) / exp, 1) if exp else None,
                                    "delay_ms": round(sum(r["delay_sum"] for r in last) / dn) if dn else None,
                                    "hours": hours, "packets_1h": rec}
            self.links_at = time.time()
        now = now_ms()
        for host, l in self.links.items():          # fresh every time: when the last monitoring packet arrived
            l["last_ms"] = self.parts.get(f"low/{host}/signal-monitor", {}).get("last")
            quiet = (now - l["last_ms"]) / 1000 if l["last_ms"] else None
            # silence first (loss only counts packets that arrived), then the loss rate; the page shows the same
            l["state"] = ("unknown" if quiet is None else "down" if quiet > DOWN_S else "stale" if quiet > STALE_S
                          else "unknown" if l["loss_1h"] is None else "up" if l["loss_1h"] < 1
                          else "warn" if l["loss_1h"] < 5 else "down")
        return self.links

    def arrived(self, session, gid, name, room):
        """One event delivered to the console by a receiver: counted per route (its sender run's session) and group,
        before any duplicate check, so a group sent to two rooms counts once per route."""
        if gid:
            if name and self.group_names.get(gid) != name:
                self.group_names[gid] = name[:80]
                self.store.put("mon_groups", dict(list(self.group_names.items())[-100:]))
            if session:
                a = self.arrivals.setdefault(f"{session}/{gid[:8]}", [0, 0])
                a[0], a[1] = a[0] + 1, now_ms()
                if len(self.arrivals) > 400:
                    self.arrivals = dict(sorted(self.arrivals.items(), key=lambda kv: kv[1][1])[-300:])
                self.store.put("mon_arrivals", self.arrivals)
                self.dirty = True

    def groups_summary(self):
        """Per Signal group, for its window on the page: each route it takes (room), sent this run by the subscriber
        vs arrived here, and that route's state; plus the lost messages of its rooms (a lost message's group is
        unknown). Group IDs and names and counts only, never content."""
        rooms = self.rooms_summary()
        names = {gid[:8]: (gid, name) for gid, name in self.group_names.items()}
        out = {}
        for k, p in self.parts.items():
            if p.get("comp") != "subscriber" or p.get("state") in ("stale", "down") or (p.get("name") or "").startswith(TEST_PREFIX):
                continue
            sess = {r[0]: r[1] for r in self.streams(p)}
            for g8, room, n, age in (p.get("flow") or {}).get("g") or []:
                if not isinstance(g8, str) or not isinstance(n, int):
                    continue
                gid, name = names.get(g8, (g8, g8))
                got, last = self.arrivals.get(f"{sess.get(room)}/{g8}", [0, 0])
                state = ("idle" if not n else "up" if got >= n else
                         "up" if isinstance(age, int) and age < ARRIVE_S else "down")
                r = rooms.get(room) or {}
                out.setdefault(gid, {"name": name, "routes": [], "lost": {}})["routes"].append(
                    {"room": room, "title": r.get("title") or room, "sent": n, "arrived": got, "last_sent_age_s": age, "last_arrival": last or None,
                     "receivers": r.get("receivers", []), "state": state, "from": k})
                if r.get("lost"):
                    out[gid]["lost"][room] = r["lost"]
        for gid, name in self.group_names.items():           # groups seen before, not sent by any subscriber now
            out.setdefault(gid, {"name": name, "routes": [], "lost": {}})
        for g in out.values():
            states = [r["state"] for r in g["routes"]]
            g["state"] = ("down" if "down" in states else "up" if "up" in states else "idle")
        return out

    def rooms_summary(self):
        """One entry per chat room seen (sent by a subscriber, or served by a receiver), for its tile in the diode
        band: messages sent this run vs rebuilt at the receivers, its senders and receivers, and its state:
        idle before any traffic, down if messages are not arriving, warn if no receiver serves it or a
        receiver got its packets by mistake, else up."""
        rxs = self.receivers()
        live_rx = {k: r for k, r in rxs.items() if r.get("state") not in ("stale", "down")}
        rooms = {}
        def room(name):
            return rooms.setdefault(name, {"sent": 0, "arrived": 0, "lost": 0, "last_age_s": None, "subs": [],
                                           "receivers": [], "misrouted": {}, "missing": 0})
        for k, p in self.parts.items():
            if p.get("comp") != "subscriber" or p.get("state") in ("stale", "down"):
                continue
            for name, s_, m, m_age in self.streams(p):
                if not name:
                    continue
                r = room(name)
                r["subs"].append(k)
                r["sent"] += m or 0
                holders = [x for x in rxs.values() if s_ in (x.get("sess") or {})]
                r["arrived"] += max([(x.get("sess") or {})[s_][0] for x in holders] or [0])
                r["lost"] += max([(x.get("sess") or {})[s_][2] for x in holders] or [0])
                if isinstance(m_age, int) and m and (r["last_age_s"] is None or m_age < r["last_age_s"]):
                    r["last_age_s"] = m_age
        for k, x in rxs.items():
            for name in x.get("rooms") or []:
                if name and (x is live_rx.get(k) or not any(name in (y.get("rooms") or []) for y in live_rx.values())):
                    room(name)["receivers"].append(k)
            if "ROOM_MISMATCH" in (x.get("active") or []):
                for name, n in (x.get("wrong_room") or {}).items():
                    if name and name != "MONITOR":
                        room(name)["misrouted"][k] = n
        for k, missing in (self.chat_missing or {}).items():
            for _, _, _, _, name in missing:
                if name in rooms:
                    rooms[name]["missing"] += 1
        titles = self.room_titles()
        for name, r in rooms.items():
            r["title"] = titles.get(name, name)
            r["state"], r["text"] = (
                ("down", "messages sent to it are not arriving") if r["missing"] else
                ("down", "its receiver is not reporting") if r["receivers"] and not any(k in live_rx for k in r["receivers"]) else
                ("warn", "no receiver serves it") if not r["receivers"] and r["subs"] else
                ("warn", "its packets reached a receiver that doesn't serve it") if r["misrouted"] else
                ("idle", "no messages yet this run") if not r["sent"] else ("up", ""))
        return dict(sorted(rooms.items()))

    def state(self):
        return {"parts": sorted((self.shown(p) for p in self.parts.values() if p["state"]), key=lambda p: p["key"]),
                "rooms": self.rooms_summary(),
                "room_titles": self.room_titles(),
                "groups": self.groups_summary(),
                "links": self.link_summary(),
                "incidents": self.incidents.summary(),
                "containers": self.containers,
                "facts_ok": self.facts_ok,
                "now": now_ms()}

    def snapshot(self):
        return {**self.state(), "events": list(self.events)}

    chat_missing = {}

    def tick(self):
        """Every few seconds: re-check ages and rules, and push the state to browsers if anything changed.
        Each step is guarded: one that fails is counted (CHECKS_FAILING on the console's tile) and the others still run.
        Browsers get "mon_alive" after every full round, so a stuck monitor shows on the page as "last checked"."""
        self.parts["high/console"]["last"] = now_ms()
        failed = 0
        for step in (self._step_chat, self._step_console, self._step_gone, self.evaluate, self._settle, self.metrics,
                     self._step_incidents):
            try:
                step()
            except Exception:
                failed += 1
                log.exception("A monitoring check failed (%s); the others carry on:", step.__name__)
        h = self.health
        if failed:
            h["tick_fail"] += 1
        else:
            h["tick_fail"], h["tick_ok_all"] = 0, time.time()
        if self.dirty:
            self.dirty = False
            self.publish({"op": "mon", **self.state()})
        if not failed and time.time() - self.alive_at >= 10:
            self.alive_at = time.time()
            self.publish({"op": "mon_alive", "at": now_ms()})

    def _step_chat(self):
        self.chat_missing = self._chat_path()
        for key in self.receivers():
            self._right_state(key)

    def _step_console(self):
        pb = self.parts["high/console"]
        if self.disk_path:
            try:
                u = shutil.disk_usage(self.disk_path)
                pb.setdefault("info", {})["disk_free_pct"] = round(100 * u.free / u.total, 1)
            except OSError:
                pass
        r = self.recent.setdefault("high/console", Recent({}, pb.get("ups")))
        more = r.update(self.health["p"])
        old = pb.get("code")
        pb["p"], pb["ups"] = dict(self.health["p"]), r.ups
        self._right_state("high/console")
        self._more("high/console", old, more)

    def _step_incidents(self):
        shown = {k: self.shown(p) for k, p in self.parts.items() if p["state"]}
        traffic = [p.get("traffic_at") for p in self.receivers().values() if p.get("traffic_at")]
        self.incidents.update(shown, self.link_summary(), max(traffic) if traffic else None)
        if self.incidents.dirty:
            self.incidents.dirty, self.dirty = False, True
