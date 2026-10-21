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
the console): a counted problem of the part's own keeps it WARN/DOWN for STATE_S after it last happened (colour =
now). A count that goes up while the part already shows that code is logged as one more event ("1 more message
skipped").
Boxes and connections (high/docs/monitoring-redesign.md, 2026-10-20): a box shows only its own health; each link between
parts has its own state (connections()), which the page's wires show. Before a box is blamed, a second witness is
asked: signal-monitor's check of signal-cli failing while a subscriber still reads signal-cli is the check's path,
not signal-cli; a subscriber with no status while its container runs is the status path, not the subscriber.
The chat path check: each subscriber reports the last message number it sent and when; the receiver reports, per
sender, the highest number it rebuilt or gave up on. Sent more than ARRIVE_S ago and still not there: the chat
port (6510) or its diode route is losing messages, even when monitoring (6511) gets through: the chat route's
connection shows it, not the receiver.
"""
import calendar, hashlib, json, logging, re, shutil, time
from collections import deque
from store import now_ms
from incidents import Incidents, LOSS_OPEN

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
RECENT_S = 600                     # counted problems are remembered this long (counts for the Log and the Analyst)
LINK_RECENT_S = 300                # the Monitoring route's colour: loss over the last 5 min (was the hour)
LINK_WARN, LINK_DOWN = 2, 20       # % lost in that window (WARN needs 2 packets lost: one alone is a Log line)
STATE_S = 60                       # ... but colour their part only this long after they last happened: colour = now
RES = {"mem": "memory", "cpu": "CPU", "pids": "processes"}
CHECK_FAILED = {"UNREACHABLE", "GATEWAY_CLOSED", "TLS_FAILED", "TIMEOUT", "HTTP_ERROR"}   # signal-cli's check didn't answer
ARRIVE_S = 30                      # a sent message not at the receiver after this long is missing (one takes < 1 s)
GIVE_UP_S = 900                    # ... and after this long it is counted as lost and the check moves on
DISK_LOW_PCT = 10
REMOVE_AFTER_S = 3600              # a box can be removed by hand once nothing has been heard from it for this long
GONE_WAIT_S = 120                  # a removed container's boxes go after this long, unless it is back (compose recreate)
SIM_MARKER = "signal-sim-on-"     # a tools/sim test's marker container: while it exists, incidents and events say "test"
PATH_CODES = ("MESSAGE_LOST", "PACKETS_MISSING", "MESSAGE_BROKEN", "BAD_PACKET", "ROOM_MISMATCH")   # a room's route WARN
BACK_S = 30                        # after a silent signal-monitor is back, its parts wait this long for fresh news
MRX_LATE_S = 40                    # monitor-receiver -> console WARN after this long with nothing (its heartbeat: 30 s)
TEST_PREFIX = "signal-sim-"        # tools/sim's test containers: their boxes go soon after they are removed (TEST_GONE_S:
TEST_GONE_S = 20                   # not at once, as a test may recreate the same name), and their made-up groups get no window
# Counted problems of a high-side part's OWN that change its state, worst first: (code, state, text for n of them).
# Not here, on purpose: what happens on its links (FORWARD_FAILED/SLOW: the link to Gotham; DELIVER_FAILED,
# NOTICE_FAILED: the link to the console), what reached it (BAD_PACKET, ROOM_MISMATCH: the sender or the diode's
# routes) and lost messages (MESSAGE_LOST): counted and logged, shown on the connection, not on the box.
RECEIVER_COUNTED = [
    ("MESSAGE_BROKEN", "warn", "{n} message(s) arrived complete but could not be rebuilt just now"),
    ("RX_OVERFLOW", "warn", "the kernel dropped {n} packet(s) just now because the receive buffer was full"),
    ("OTHER", "warn", "{n} unexpected error(s) in its log just now (see its Docker log)"),
]
CONSOLE_COUNTED = [
    ("DB_ERROR", "down", "{n} database error(s) just now (disk full, locked or damaged?): chat or monitoring may not be saved"),
    ("INGEST_FAILED", "warn", "it failed {n} time(s) just now to take in what the receiver or monitor-receiver sent"),
]
# AI_FAILED: the Analyst (relay-assist, its own container) is shown by a dot on its button, not on the console's box.
# Why a subscriber's stream from signal-cli is down, in plain words (as signal-monitor says it; for older ones too).
STREAM_WORDS = {"UNREACHABLE": "can't connect to signal-cli (refused, no route, or the name doesn't resolve)",
                "TLS_FAILED": "the secure connection to signal-cli failed (a certificate problem)",
                "GATEWAY_CLOSED": "signal-cli's gateway closed the connection (is this subscriber's certificate allowed?)",
                "HTTP_ERROR": "signal-cli's gateway answered with an error",
                "TIMEOUT": "signal-cli didn't answer in time",
                "LOST": "the connection to signal-cli dropped"}
RECONNECT_S = 20                   # a subscriber's stream down this long: its link is DOWN (before: WARN, reconnecting)
SLOW_CHECK_MS = 2000               # signal-monitor's check of signal-cli slower than this: the check's link is WARN
log = logging.getLogger("console")


def utc_s(ms):
    return time.strftime("%H:%M:%S", time.gmtime(ms / 1000))


def utc_d(ms):
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))


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

    def n(self, code, within=RECENT_S):
        now = time.time()
        return sum(d for t, d in self.ups.get(code, []) if now - t < within)

    def active(self, rules):
        """The rules that apply now: their problem happened within STATE_S (colour = now)."""
        return [(c, st, t.format(n=self.n(c, STATE_S))) for c, st, t in rules if self.n(c, STATE_S)]


class Monitor:
    def __init__(self, store, publish, runbook=None):
        self.store, self.publish, self.runbook = store, publish, runbook or {}
        self.parts = store.parts()
        self.seqs = store.get("mon_seq", {})           # host -> {"boot", "seq"}: for counting lost packets
        self.events = deque(store.mon_events(limit=300), maxlen=300)
        self.started = time.time()
        self.dirty = True
        self.links, self.links_at = {}, 0
        self.link_recent = {}                            # host -> [(rx ms, expected, delay ms)] over LINK_RECENT_S
        self.link_rx = {}                                # host -> when its last monitoring packet arrived (ms)
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
        self.owns = {}                                  # part key -> (state, code, text) from own(), while it applies
        self.analyst = {"ok": None, "at": None, "error": ""}   # the Analyst's latest call: its button's dot
        self.sim = None                                 # (test name, until s) while tools/sim runs a test here
        self.mrx_post_at = None                         # when monitor-receiver last got anything to us (POST /monitor)
        self.conn_seen = {}                             # connection key -> [state, since ms]: changes go in the Log
        self.daily = store.get("mon_daily", {})         # "YYYY-MM-DD" -> counts per room, group, receiver (get_delivery)
        self.daily_last = store.get("mon_daily_last", {})   # cumulative counters last seen, to count the difference

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
            title = (self.runbook.get(code) or {}).get("title") or code.replace("_", " ").lower()
            if code in LOSS_OPEN:                        # lost messages: an incident even if all is well again at once
                self.incidents.loss(key, code, d, f"{self.title(p)}: {title} ({d})", self.test_running())
                self.dirty = True
            if code not in COUNTED or (code == p.get("code") and code != old_code):
                continue                                 # unknown noise, or the state change just announced it
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
            active += r.active(RECEIVER_COUNTED)          # messages not arriving: the chat route's connection
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
                        text = (f"{m - max(done, counted_)} message(s) sent by {self.title(p)}"
                                f"{f' to {self.room_title(room)}' if room else ''} (up to #{m}, at {utc_s(sent_at)} UTC) "
                                f"never reached {self.title(rxs[k])}: they are lost.")
                        self._event(k, "CHAT_LOST", "error", text)
                        route = f"high/~link/route/{room}/{rxs[k].get('name')}" if room else k
                        self.incidents.loss(route, "CHAT_LOST", m - max(done, counted_), text, self.test_running())
                        self._day("rooms", room, "never_arrived", m - max(done, counted_))
                elif wait > ARRIVE_S:
                    for k in where:
                        missing.setdefault(k, []).append((p, m, done, sent_at, room))
            if p.get("to") != to_all:
                p["to"], self.dirty = to_all, True
        self._subscriber_checks()
        return missing

    def _subscriber_checks(self):
        """The console's own checks on subscribers (it sees all hosts and every receiver, signal-monitor doesn't):
        a room no receiver serves, and the same routes.conf name running in two places. Kept as "checks" on the
        part, shown over what the subscriber reported (see shown())."""
        live = {k: p for k, p in self.parts.items() if p.get("comp") == "subscriber" and p.get("state") not in ("stale", "down")}
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
            # a room no receiver serves: the chat route's connection (ROOM_UNSERVED), not the subscriber
            if p.get("checks", []) != checks:
                p["checks"], self.dirty = checks, True

    def shown(self, p):
        """A part as the page, the bell and incidents see it: its own health where a second witness changes what
        signal-monitor said (own()); a console check on a subscriber that reported UP shows as its code and text (the
        subscriber's own report wins when it has a problem itself)."""
        own = self.owns.get(p["key"])                     # set by effective() on every check (None while silent)
        if own and own[0] == p.get("state"):
            return {**p, "code": own[1], "text": own[2], "active": [own[1]] + [a for a in (p.get("active") or []) if a != own[1]]}
        c = p.get("checks")
        if not c or p.get("reported", "up") != "up" or p.get("state") in ("stale", "down", "unknown"):
            return p
        return {**p, "code": c[0][0], "text": c[0][2], "active": [x[0] for x in c] + list(p.get("active") or [])}

    # ---- messages from monitor-receiver ----
    def handle(self, m):
        """One message (dict). Returns None if accepted, else a short reason."""
        self.mrx_post_at = now_ms()                      # anything from monitor-receiver: its link to us works
        t = m.get("t")
        if t == "rx_hb":
            old = ((self.parts.get("high/monitor-receiver") or {}).get("info") or {}).get("not_delivered")
            new = (m.get("info") or {}).get("not_delivered") if isinstance(m.get("info"), dict) else None
            extra = {"nd_at": now_ms()} if isinstance(old, int) and isinstance(new, int) and new > old else {}
            self.touch("high/monitor-receiver", m.get("info"), side="high", comp="monitor-receiver",
                       name="monitor-receiver", **extra)
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
            # no counts at all (signal-cli's check failed, e.g. while it is frozen): keep the last ones, or its next
            # report would count everything since it started again as new problems
            p_new = codes_counts(m.get("p")) if isinstance(m.get("p"), dict) else old.get("p") or {}
            more = Recent(old.get("p")).update(p_new) if old else {}
            self._part(key, side="low", host=host, inst=inst, comp=comp, name=name, last=now_ms(),
                       reported=state, info=small(m.get("info")), text=clean(m.get("text"), 200),
                       code=CODE.sub("", clean(m.get("code"), 40).upper()) or None,
                       active=codes_list(m.get("active")), p=p_new,
                       flow=small({k: v for k, v in m["flow"].items() if k not in ("rooms", "lw")}) if isinstance(m.get("flow"), dict) else {},
                       cid=clean(m.get("cid"), 12) or None, heard=m.get("heard") if isinstance(m.get("heard"), int) else None)
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
            q = self.parts[key]
            if q.get("code") != old_code or "code_since" not in q:
                q["code_since"] = now_ms()                # for the links' grace times (a stream down 20 s: DOWN)
            if more.get("SEND_FAILED"):
                q["send_failed_at"] = now_ms()           # the chat route's colour: sends failing now
            self.store.put_part(key, q)                  # with the lists set above
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
        q = self.link_recent.setdefault(host, [])
        # back after a silence (the link was out, already shown as STALE/DOWN): the packets missed meanwhile are that
        # outage, not loss on a working link, so they don't keep the last 5 min red (the hourly history keeps them)
        before, self.link_rx[host] = self.link_rx.get(host), rx
        recent_exp = 1 if before and rx - before > STALE_S * 1000 else expected
        q.append((rx, recent_exp, delay if -3600_000 < delay < 3600_000 else None))
        while q and q[0][0] < rx - LINK_RECENT_S * 1000:
            q.pop(0)
        if expected > 1:
            log.warning("%d monitoring packet(s) from %s never arrived.", expected - 1, host)

    # ---- states ----
    def effective(self, p, now):
        """(state shown for a part, True if it comes from silence rather than from what the part reported)."""
        self.owns[p["key"]] = None
        if p["comp"] == "console":
            return p.get("reported", "up"), False
        age = (now - p["last"]) / 1000
        if p["side"] == "low" and p["comp"] != "signal-monitor":
            mon = self.parts.get(f"low/{p['host']}/signal-monitor")
            if mon and mon["state"] in ("stale", "down", "unknown"):
                return "unknown", True
            if mon and mon.get("back_at") and p["last"] < mon["back_at"] and now - mon["back_at"] < BACK_S * 1000:
                return "unknown", True                   # its signal-monitor is just back: wait for its news, no DOWN flash
        if p["comp"] == "signal-servers":                # its news comes with signal-cli's check: none while that fails
            cli = self.parts.get(f"low/{p['host']}/{p['inst']}/signal-cli")
            if cli and (cli["state"] in ("stale", "unknown") or cli.get("code") in CHECK_FAILED):
                return "unknown", True
        if age > STALE_S and p["side"] == "low" and p["comp"] != "signal-monitor" and self._lossy(p.get("host")):
            return "unknown", True                       # its news may be what the lossy diode dropped: not its fault
        if age > DOWN_S:
            return "down", True
        if age > STALE_S:
            return "stale", True
        own = self.own(p) if p["side"] == "low" else None
        if own:
            self.owns[p["key"]] = own
            return own[0], False
        if p.get("reported", "up") == "up" and (p.get("container") or {}).get("b"):
            return "warn", False                         # its container is near its limits (container facts)
        if p.get("reported", "up") == "up" and p.get("checks"):
            return p["checks"][0][1], False              # a console check (no receiver for a room, running twice)
        return p.get("reported", "up"), False

    def _lossy(self, host):
        """True while the host's monitoring route loses packets (as for the Monitoring marker's WARN)."""
        q = [x for x in self.link_recent.get(host, []) if x[0] >= now_ms() - LINK_RECENT_S * 1000]
        exp = sum(x[1] for x in q)
        lost = exp - len(q)
        return lost >= 2 and exp and 100 * lost / exp >= LINK_WARN

    def own(self, p):
        """A low-side box's own health where what signal-monitor reported blames it for a link, or for something a
        second witness contradicts: (state, code, text), else None (what it reported stands). See the module text."""
        c = p.get("container") or {}
        code = p.get("code")
        if c.get("s") == "paused":
            return "down", "CONTAINER_PAUSED", f"frozen: its container {c.get('n')} is paused (docker pause)"
        heard = p.get("heard")       # when signal-monitor last heard the subscriber itself (not its repeat), low host's clock
        if c.get("s") in ("exited", "dead") and p["comp"] == "subscriber" and p.get("reported") != "down" \
                and c.get("fi") and isinstance(heard, int) and heard < c["fi"]:   # nothing heard from it since: else it is back
            # container facts say it ended: DOWN now, not when its missing status or check catches up (35 s and more)
            return "down", "NO_STATUS", f"its container {self.how_it_ended(c)}"
        if p["comp"] == "subscriber":
            if code == "NOT_CONNECTED":                   # its link is down, it runs: WARN (older signal-monitors said DOWN)
                text = p.get("text") or ""
                if not text.startswith("running"):
                    text = ("running, but not connected to signal-cli: "
                            + (STREAM_WORDS.get((p.get("info") or {}).get("stream_code")) or "not connected"))
                return "warn", code, text
            if code == "NO_STATUS" and c.get("s") == "running":
                return ("unknown", "SUB_STATUS_LOST", f"its container {c.get('n')} runs, but its status doesn't reach "
                        "signal-monitor (UDP 6520), so its state is unknown")
            if code == "NO_STATUS" and c.get("s") in ("exited", "dead"):
                return "down", code, f"its container {self.how_it_ended(c)}"
        if p["comp"] == "signal-cli" and code in CHECK_FAILED:
            if c.get("s") in ("exited", "dead"):
                return "down", code, f"its container {self.how_it_ended(c)}"
            readers = [s for s in self.parts.values() if s.get("comp") == "subscriber" and s.get("host") == p.get("host")
                       and s.get("inst") == p.get("inst") and s.get("reported") in ("up", "warn")
                       and s.get("code") not in ("NOT_CONNECTED", "NO_STATUS", "STARTING", "SUB_STATUS_LOST")
                       and (now_ms() - s.get("last", 0)) / 1000 < STALE_S]
            if readers:                                   # a second witness: its stream works, so signal-cli runs
                return ("unknown", "MONITOR_CHECK_FAILED", f"signal-monitor's check doesn't get through "
                        f"({p.get('text') or code}), but {self.title(readers[0])} still reads it, so it runs")
        return None

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
            if p["comp"] == "signal-monitor" and old in ("stale", "down", "unknown") and new not in ("stale", "down", "unknown"):
                p["back_at"] = now                       # back from silence (BACK_S): not any other change, e.g. WARN -> UP
            self.store.put_part(key, p)
            self.dirty = True
            if self._flapping(key, p, old, new):
                continue                                 # one "flapping" event, not one per change
            why = ({"stale": f"nothing heard for {STALE_S} s", "down": f"nothing heard for {DOWN_S // 60} min",
                    "unknown": "signal-cli's check doesn't get through, so its link to Signal is unknown" if p["comp"] == "signal-servers"
                    else "the diode is losing monitoring packets, so its news doesn't all arrive" if self._lossy(p.get("host"))
                    and (self.parts.get(f"low/{p.get('host')}/signal-monitor") or {}).get("state") == "up"
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
                c = p.get("container") or {}                # its container's use of its own limits (container facts)
                info.update({f: c[k] for k, f in (("m", "mem_pct"), ("c", "cpu_pct"), ("pi", "pids_pct"))
                             if isinstance(c.get(k), (int, float))})
                rows[key] = {"s": p["state"], **info}
        for host, l in self.link_summary().items():
            rows[f"link/{host}"] = {"s": l.get("state"), "loss_1h": l.get("loss_1h"), "delay_ms": l.get("delay_ms")}
        self.store.add_metrics(minute, rows)
        self._count_daily()

    def title(self, p):
        # "(on sighost)", or "(signal-cli-2 on sighost)" for a part of a signal-cli whose container isn't plain "signal-cli"
        inst = p.get("inst") if p.get("inst") not in (None, "signal-cli") else None
        where = f" ({inst} on {p['host']})" if inst else f" (on {p['host']})" if p.get("host") else ""
        if p["comp"] == "container":
            return f"container {p['name']} (on {p['host']})"
        name = p["name"] if p["comp"] in ("subscriber", "receiver") else LABEL.get(p["comp"], p["comp"])
        sub = p["comp"] == "subscriber" and "subscriber" not in name      # "signal-subscriber" says it already
        return f"{'subscriber ' if sub else ''}{name}{where}"

    def test_running(self):
        """The tools/sim test running now: from the sim on the high side (POST /sim), or a low host's marker
        container (signal-sim-on-<test>-<epoch>); else None."""
        if self.sim and time.time() < self.sim[1]:
            return self.sim[0]
        for cs in (self.containers or {}).values():
            for name, c in (cs or {}).items():
                if name.startswith(SIM_MARKER) and (c or {}).get("s", "running") == "running":
                    return name[len(SIM_MARKER):].rsplit("-", 1)[0] or "sim"
        return None

    def _event(self, part, code, sev, text, notify=False):
        ev = {"at": now_ms(), "part": part, "code": code, "sev": sev, "text": text, "notify": notify}
        test = self.test_running()
        if test:
            ev["test"] = test                            # a tools/sim test runs: labelled, left out of history
        ev["id"] = self.store.add_mon_event({**ev, "data": {"notify": notify, **({"test": test} if test else {})}})
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

    LOOSE = ("CONTAINER_RESTARTING", "CONTAINER_CRASHED", "CONTAINER_OOM")

    def _step_loose(self):
        """A container of ours in trouble that has no box, because it never reported (a new subscriber that crashes at
        start, e.g. on a wrong routes.conf): a box of its own, low/<host>/container/<name>, so it shows on the diagram
        and opens an incident. It goes when the container runs normally again, is removed, or gets a real box."""
        from incidents import container_finding
        now = now_ms()
        for host, known in self.containers.items():
            held = set()
            for p in self.parts.values():
                if p.get("host") == host and p.get("comp") != "container":
                    held.update(x for x in ((p.get("container") or {}).get("n"), p.get("name"), p.get("inst"),
                                            p.get("comp") if p.get("comp") == "signal-monitor" else None) if x)
            for name, c in known.items():
                key = f"low/{host}/container/{name}"
                cf = None if name in held else container_finding({"container": c}, now)
                if cf and cf[0] in self.LOOSE:
                    old = self.parts.get(key)
                    if old and old.get("code") == cf[0] and old.get("container") == c:
                        old["last"] = now                # nothing new: no write, no push
                        continue
                    self._part(key, side="low", host=host, comp="container", name=name, container=c,
                                        last=now, reported="down", code=cf[0], text=f"{cf[1]}; it never reported, so it has no other box")
                elif key in self.parts:
                    self._gone_loose(key, name, host, "gets its own box now" if name in held else "runs normally again")
        for key, p in list(self.parts.items()):
            if p.get("comp") == "container" and p.get("name") not in self.containers.get(p.get("host"), {}):
                self._gone_loose(key, p["name"], p["host"], "was removed")

    def _gone_loose(self, key, name, host, why):
        self.forget(key)
        self._event(key, "PART_REMOVED", "info", f"Container {name} on {host} {why}: its own box is gone.")

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
            # the last 5 min (colour = now): the hour's figure stays a number, for the tooltip and the Analyst
            q = [x for x in self.link_recent.get(host, []) if x[0] >= now - LINK_RECENT_S * 1000]
            exp, ds = sum(x[1] for x in q), [x[2] for x in q if x[2] is not None]
            lost = exp - len(q)
            l["loss_recent"] = round(100 * lost / exp, 1) if exp else None
            l["lost_recent"], l["recent_s"] = lost, LINK_RECENT_S
            l["delay_recent_ms"] = round(sum(ds) / len(ds)) if ds else l.get("delay_ms")
            # silence first (loss only counts packets that arrived), then the loss rate
            lr = l["loss_recent"]
            l["state"] = ("unknown" if quiet is None else "down" if quiet > DOWN_S else "stale" if quiet > STALE_S
                          else "up" if lr is None else "down" if lr > LINK_DOWN
                          else "warn" if lr > LINK_WARN and lost >= 2 else "up")
            l["text"] = (f"silent for {int(quiet)} s" if quiet and quiet > STALE_S else
                         f"{lr}% of monitoring packets lost in the last 5 min" if l["state"] in ("warn", "down") else
                         "getting through")
        return self.links

    def arrived(self, session, gid, name, room):
        """One event delivered to the console by a receiver: counted per route (its sender run's session) and group,
        before any duplicate check, so a group sent to two rooms counts once per route."""
        if gid:
            if name and self.group_names.get(gid) != name:
                self.group_names[gid] = name[:80]
                self.store.put("mon_groups", dict(list(self.group_names.items())[-100:]))
            if session:                                  # a real arrival (not a name read back from history at start)
                self._day("groups", gid[:8], "arrived", 1)
                if room:
                    self._day("rooms", room, "arrived", 1)
                a = self.arrivals.setdefault(f"{session}/{gid[:8]}", [0, 0])
                a[0], a[1] = a[0] + 1, now_ms()
                if len(self.arrivals) > 400:
                    self.arrivals = dict(sorted(self.arrivals.items(), key=lambda kv: kv[1][1])[-300:])
                self.store.put("mon_arrivals", self.arrivals)
                self.dirty = True

    # ---- what the relay is (for the Analyst's get_setup): nothing secret ----
    def setup(self, settings):
        rooms = self.rooms_summary()
        names = {gid[:8]: n for gid, n in self.group_names.items()}
        hosts = {}
        for p in self.parts.values():
            if p.get("side") != "low" or not p.get("host"):
                continue
            h = hosts.setdefault(p["host"], {"signal_cli": [], "subscribers": [], "containers": []})
            if p.get("comp") == "signal-cli":
                h["signal_cli"].append({"name": p.get("inst"), "state": p.get("state"),
                                        "certificate_days_left": (p.get("info") or {}).get("cert_days")})
            elif p.get("comp") == "subscriber":
                h["subscribers"].append({"name": p.get("name"), "state": p.get("state"),
                                         "rooms": [self.room_title(r[0]) for r in self.streams(p) if r[0]],
                                         "groups": [names.get(g[0], g[0]) for g in (p.get("flow") or {}).get("g") or [] if isinstance(g, list)]})
            elif p.get("comp") == "signal-monitor":
                i = p.get("info") or {}
                h["signal_monitor"] = {"version": i.get("ver"), "certificate_days_left": i.get("cert_days"),
                                       "disk_free_pct": i.get("disk_free_pct")}
        for host, cs in (self.containers or {}).items():
            for name, c in (cs or {}).items():
                if name.startswith(SIM_MARKER):
                    continue
                hosts.setdefault(host, {"signal_cli": [], "subscribers": [], "containers": []})["containers"].append(
                    {"name": name, "state": c.get("s"), "image": c.get("img"),
                     "started_utc": utc_d(c["st"]) if c.get("st") else None, "docker_restarts": c.get("r"),
                     "memory_pct_of_limit": c.get("m")})
        receivers = [{"name": p.get("name"), "state": p.get("state"), "rooms": [self.room_title(r) for r in p.get("rooms") or []],
                      "forwards_to": (p.get("info") or {}).get("forward_to", "Gotham Chat") or "nothing (FORWARD_TO off)",
                      "started_utc": utc_d(p["started"]) if p.get("started") else None}
                     for p in self.receivers().values()]
        return {"low_side_hosts": hosts, "high_side_host": settings.get("host"),
                "rooms": [{"room": r.get("title"), "short_name": name, "receivers": [k.rsplit("/", 1)[-1] for k in r.get("receivers") or []]}
                          for name, r in rooms.items()],
                "receivers": receivers, "settings": {k: v for k, v in settings.items() if k != "host"},
                "note": "rooms are named by the Signal groups they carry; short names only for the configuration"}

    # ---- outages, worked out from the state changes in the Log (for the Analyst's get_outages) ----
    def name_of(self, key, conns=None):
        p = self.parts.get(key)
        if p and p.get("comp"):
            return self.title(p)
        c = next((c for c in conns or [] if c["key"] == key), None)
        if c:
            return c["label"]
        if key.startswith("link/"):
            return f"Monitoring from {key[5:]}"
        return key.rsplit("/", 1)[-1]

    def outages(self, what="", days=7):
        """Periods a box was DOWN/UNKNOWN/STALE or a connection DOWN, per item: how many, total minutes,
        availability %, the longest; tests (tools/sim) counted apart. what: a part key, or words in its name."""
        now = now_ms()
        start = now - days * 86400_000
        conns = self.connections()
        bad_box, bad_link = ("down", "unknown", "stale"), ("down",)
        open_, periods = {}, []
        for at, key, state, test in self.store.state_events(start):
            bad = state in (bad_link if "~link" in key else bad_box)
            if bad and key not in open_:
                open_[key] = (at, state, test)
            elif not bad and key in open_:
                s0, st, t = open_.pop(key)
                periods.append((key, s0, at, st, t))
        removed, gone = {}, set()                        # a box taken off the diagram: its outage ended there
        for e in self.store.mon_events(start, 1000, code="PART_REMOVED") + self.store.mon_events(start, 1000, code="HOST_REMOVED"):
            removed[e["part"]] = e["at"]
        live = set(self.parts) | {c["key"] for c in conns}
        for key, (s0, st, t) in open_.items():           # still going, unless the box is gone
            end = removed.get(key) or next((at for k, at in removed.items() if k.startswith("link/") and
                                            key.startswith(f"low/{k[5:]}/")), None)
            if end and end > s0:
                periods.append((key, s0, end, st, t))
                gone.add(key)
            elif key in live:
                periods.append((key, s0, None, st, t))
        w = (what or "").lower().strip()
        def match(key):
            return not w or w == key.lower() or w in self.name_of(key, conns).lower() or w in key.lower()
        periods = [p for p in periods if match(p[0])]
        mins = lambda p: round(((p[2] or now) - p[1]) / 60000, 1)
        items = {}
        for p in periods:
            if p[4]:
                continue
            it = items.setdefault(p[0], {"name": self.name_of(p[0], conns), "key": p[0], "outages": 0, "minutes": 0.0})
            it["outages"] += 1
            it["minutes"] = round(it["minutes"] + mins(p), 1)
        for it in items.values():
            it["availability_pct"] = round(100 - 100 * it["minutes"] / (days * 1440), 3)
        real = sorted((p for p in periods if not p[4]), key=mins, reverse=True)
        row = lambda p: {"name": self.name_of(p[0], conns), "key": p[0], "state": p[3].upper(),
                         "from_utc": utc_d(p[1]), "to_utc": utc_d(p[2]) if p[2] else "still going",
                         "minutes": mins(p), **({"test": p[4]} if p[4] else {}),
                         **({"box_removed": "this box was taken off the diagram (its container no longer exists)"}
                            if p[0] in gone or p[0] not in live else {})}
        tests = [p for p in periods if p[4]]
        return {"days": days, "window_from_utc": utc_d(start), "now_utc": utc_d(now), "what": what or "everything",
                "count": len(real), "items": sorted(items.values(), key=lambda x: -x["minutes"]),
                "longest": [row(p) for p in real[:5]],
                "latest": [row(p) for p in sorted(real, key=lambda p: -p[1])[:10]],
                "tests_left_out": len(tests),
                "longest_test": row(max(tests, key=mins)) if tests else None,
                "note": "box outages = DOWN, UNKNOWN or STALE; connection outages = DOWN; from the Log's state "
                        "changes (connections since 2026-10-20)"}

    # ---- analytics for the Analyst (get_analysis): worked out here, so the model copies figures, never adds up ----
    def analysis(self, kind, what="", days=7, at=None, minutes=15, bucket="day"):
        now = now_ms()
        days = max(1, min(int(days or 7), 30))
        start = now - days * 86400_000
        real = lambda since: [i for i in self.store.incidents(since_ms=since, limit=2000) if i["opened"] >= since and not i.get("test")]
        lasted = lambda i: ((i.get("closed") or now) - i["opened"]) / 60000
        if kind == "trend":
            step = 3600_000 if bucket == "hour" else 86400_000
            first = start - start % step
            keys = [first + n * step for n in range(int((now - first) // step) + 1)]
            label = (lambda t: time.strftime("%Y-%m-%d %H:00", time.gmtime(t / 1000))) if step == 3600_000 else \
                    (lambda t: time.strftime("%Y-%m-%d", time.gmtime(t / 1000)))
            vals = dict.fromkeys(keys, 0.0)
            w = (what or "incidents").lower()
            if w in ("incidents", "incident_minutes"):
                for i in real(start):
                    b = i["opened"] - i["opened"] % step
                    if b in vals:
                        vals[b] += 1 if w == "incidents" else lasted(i)
            elif w in ("sent", "arrived", "lost", "never_arrived", "forwarded", "forward_failed"):
                if step == 3600_000:
                    return {"error": "message counts are kept per day: use bucket day"}
                for day, d in self.daily.items():
                    t = calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000
                    if t in vals:
                        sec = "receivers" if w.startswith("forward") else "rooms"
                        vals[t] += sum(v.get(w, 0) for v in (d.get(sec) or {}).values())
            else:
                return {"error": "what: incidents, incident_minutes, sent, arrived, lost, never_arrived, forwarded or forward_failed"}
            series = [{"period": label(k), "value": round(v, 1)} for k, v in vals.items()]
            half = len(series) // 2
            a = sum(x["value"] for x in series[:half]) / max(1, half)
            b = sum(x["value"] for x in series[half:]) / max(1, len(series) - half)
            direction = "flat" if abs(b - a) <= max(0.5, 0.2 * max(a, b)) else "rising" if b > a else "falling"
            vs = [x["value"] for x in series]
            return {"kind": "trend", "what": w, "bucket": "hour" if step == 3600_000 else "day", "series": series,
                    "total": round(sum(vs), 1), "max": max(vs), "max_at": series[vs.index(max(vs))]["period"],
                    "average": round(sum(vs) / len(vs), 2), "direction": direction,
                    "direction_means": "second half of the window vs the first half"}
        if kind == "compare":
            prev_start = start - days * 86400_000
            cur = real(start)
            prev = [i for i in real(prev_start) if i["opened"] < start]
            def sums(incs):
                causes = {}
                for i in incs:
                    causes[i["cause"]["code"]] = causes.get(i["cause"]["code"], 0) + 1
                return {"incidents": len(incs), "incident_minutes": round(sum(lasted(i) for i in incs), 1),
                        "messages_lost_in_incidents": sum(sum(x.get("n") or 0 for x in i.get("loss") or []) for i in incs),
                        "per_cause": causes}
            def msgs(a, b):
                t = {}
                for day, d in self.daily.items():
                    ts = calendar.timegm(time.strptime(day, "%Y-%m-%d")) * 1000
                    if a <= ts < b:
                        for v in (d.get("rooms") or {}).values():
                            for k in ("sent", "arrived", "lost", "never_arrived"):
                                t[k] = t.get(k, 0) + v.get(k, 0)
                return t
            c, p_ = {**sums(cur), "messages": msgs(start, now)}, {**sums(prev), "messages": msgs(prev_start, start)}
            change = {k: {"now": c[k], "before": p_[k], "change": round(c[k] - p_[k], 1),
                          "change_pct": round(100 * (c[k] - p_[k]) / p_[k], 1) if p_[k] else None}
                      for k in ("incidents", "incident_minutes", "messages_lost_in_incidents")}
            return {"kind": "compare", "this_period": f"last {days} day(s)", "before": f"the {days} day(s) before",
                    "now": c, "previous": p_, "change": change,
                    "note": "message counts exist only since this console version started counting (get_delivery)"}
        if kind == "around":
            t = at or now
            a, b = t - minutes * 60_000, t + minutes * 60_000
            evs = self.store.mon_events(a, 1000, until_ms=b)
            rows = [{"at_utc": utc_d(e["at"]) + time.strftime(":%S", time.gmtime(e["at"] / 1000)), "what": self.name_of(e["part"]),
                     "code": e["code"], "sev": e["sev"], "text": e["text"], **({"test": e["data"]["test"]} if (e.get("data") or {}).get("test") else {})}
                    for e in evs]
            firsts = {}
            for r in rows:
                if r["sev"] in ("warning", "error", "critical"):
                    firsts.setdefault(r["what"], r["at_utc"])
            return {"kind": "around", "from_utc": utc_d(a), "to_utc": utc_d(b), "events": rows[:200],
                    "first_problem_per_part": sorted(({"what": k, "first_at_utc": v} for k, v in firsts.items()), key=lambda x: x["first_at_utc"]),
                    "note": "the part whose problem came first is often the cause; the order only suggests, it doesn't prove"}
        if kind == "reliability":
            incs = sorted(real(start), key=lambda i: i["opened"])
            w = (what or "cause").lower()
            groups = {}
            for i in incs:
                if w == "part":
                    for k in i.get("parts") or []:
                        groups.setdefault(self.name_of(k), []).append(i)
                else:
                    groups.setdefault(i["cause"]["code"], []).append(i)
            out = []
            for name, g in groups.items():
                gaps = [(y["opened"] - (x.get("closed") or x["opened"])) / 3600_000 for x, y in zip(g, g[1:])]
                out.append({"what": name, **({"title": g[0]["cause"]["title"]} if w != "part" else {}), "count": len(g),
                            "minutes_total": round(sum(lasted(i) for i in g), 1),
                            "mean_minutes_to_recover": round(sum(lasted(i) for i in g) / len(g), 1),
                            "longest_minutes": round(max(lasted(i) for i in g), 1),
                            "mean_hours_between": round(sum(gaps) / len(gaps), 1) if gaps else None,
                            "last_utc": utc_d(g[-1]["opened"])})
            return {"kind": "reliability", "by": "part" if w == "part" else "cause", "days": days,
                    "items": sorted(out, key=lambda x: (-x["count"], -x["minutes_total"])),
                    "note": "mean_minutes_to_recover = average incident length; mean_hours_between = average time from one "
                            "incident's end to the next one's start; tests left out"}
        if kind == "busiest":
            incs = real(start)
            hours, wd = [0] * 24, {}
            for i in incs:
                hours[int(i["opened"] // 3600_000 % 24)] += 1
                d = time.strftime("%a", time.gmtime(i["opened"] / 1000))
                wd[d] = wd.get(d, 0) + 1
            msg_wd = {}
            for day, d in self.daily.items():
                n = sum(v.get("sent", 0) for v in (d.get("rooms") or {}).values())
                k = time.strftime("%a", time.strptime(day, "%Y-%m-%d"))
                msg_wd[k] = msg_wd.get(k, 0) + n
            top = sorted(range(24), key=lambda h: -hours[h])[:3]
            return {"kind": "busiest", "days": days, "incidents": len(incs),
                    "incidents_per_hour_utc": {f"{h:02d}": n for h, n in enumerate(hours) if n},
                    "busiest_hours_utc": [f"{h:02d}:00" for h in top if hours[h]],
                    "incidents_per_weekday": wd, "messages_sent_per_weekday": msg_wd or None,
                    "note": "a few incidents are not a pattern: see get_history's pattern"}
        return {"error": "kind: trend, compare, around, reliability or busiest"}

    # ---- daily counts (counts only, 31 days): per room, group and receiver, for the Analyst's get_delivery ----
    def _day(self, section, name, field, n):
        if not n or not name:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime(time.time()))
        x = self.daily.setdefault(day, {}).setdefault(section, {}).setdefault(str(name)[:40], {})
        x[field] = x.get(field, 0) + n
        for old in sorted(self.daily)[:-31]:
            del self.daily[old]

    def _diff(self, key, n):
        """How much a cumulative counter went up since last seen (a smaller number = it restarted from 0)."""
        if not isinstance(n, int):
            return 0
        last = self.daily_last.get(key)
        self.daily_last[key] = n
        return 0 if last is None else n - last if n >= last else n

    def _count_daily(self):
        """Once a minute: what the subscribers sent and the receivers handed on since the last minute."""
        for p in self.parts.values():
            if p.get("comp") == "subscriber" and not (p.get("name") or "").startswith(TEST_PREFIX):
                sess = {r[0]: r[1] for r in self.streams(p)}
                for g in (p.get("flow") or {}).get("g") or []:
                    if isinstance(g, list) and len(g) == 4 and isinstance(g[2], int) and sess.get(g[1]):
                        d = self._diff(f"sent/{sess[g[1]]}/{g[0]}", g[2])
                        self._day("groups", g[0], "sent", d)
                        self._day("rooms", g[1], "sent", d)
            elif p.get("comp") == "receiver":
                i, name, run = p.get("info") or {}, p.get("name"), p.get("started")
                for field, val in (("forwarded", i.get("forwarded")), ("forward_failed", (p.get("p") or {}).get("FORWARD_FAILED")),
                                   ("delivered", i.get("delivered")), ("deliver_failed", (p.get("p") or {}).get("DELIVER_FAILED"))):
                    self._day("receivers", name, field, self._diff(f"rx/{name}/{field}", val))
                lost = self._diff(f"rx/{name}/{run}/lost", i.get("lost"))
                self._day("receivers", name, "lost", lost)
                per_room = p.get("room_stats") or {}
                if per_room:                                    # each room's own count (a receiver may serve several)
                    for room, st in per_room.items():
                        if isinstance(st, dict):
                            self._day("rooms", room, "lost", self._diff(f"rx/{name}/{run}/lost/{room}", st.get("lost")))
                elif len(p.get("rooms") or []) == 1:            # an older receiver without per-room counts
                    self._day("rooms", p["rooms"][0], "lost", lost)
        self.daily_last = dict(list(self.daily_last.items())[-500:])
        self.store.put("mon_daily", self.daily)
        self.store.put("mon_daily_last", self.daily_last)

    def delivery(self, days=7):
        """Per day (UTC), newest first: per room, per group (by name) and per receiver: sent, arrived, lost,
        handed on (forwarded = my_forward returned without an error) and failed. Counts only; totals over the days."""
        names = {gid[:8]: n for gid, n in self.group_names.items()}
        titles = self.room_titles()
        out, total = [], {}
        wanted = [time.strftime("%Y-%m-%d", time.gmtime(time.time() - n * 86400)) for n in range(days)]
        for day in wanted:
            if day not in self.daily:                    # before counting started, or the console was down all day
                out.append({"day": day, "no_counts": "nothing was counted this day: say the data can't tell"})
                continue
            d = self.daily[day]
            z = lambda v, keys: {**dict.fromkeys(keys, 0), **v}       # every figure, 0 when none (not "missing")
            rk = ("sent", "arrived", "lost", "never_arrived")
            xk = ("forwarded", "forward_failed", "delivered", "deliver_failed", "lost")
            rxs = {**{p.get("name"): {} for p in self.receivers().values() if p.get("name")}, **(d.get("receivers") or {})}
            row = {"day": day,
                   "rooms": {titles.get(r, r): z(v, rk) for r, v in (d.get("rooms") or {}).items()},
                   "groups": {names.get(g, g): z(v, ("sent", "arrived")) for g, v in (d.get("groups") or {}).items()},
                   "receivers": {n: z(v, xk) for n, v in rxs.items()}}
            for sec in ("rooms", "groups", "receivers"):
                for name, v in row[sec].items():
                    t = total.setdefault(sec, {}).setdefault(name, {})
                    for k, n in v.items():
                        t[k] = t.get(k, 0) + n
            out.append(row)
        return {"days": days, "days_with_counts": sum(1 for r in out if "no_counts" not in r), "total": total, "per_day": out,
                "counting_since_utc": utc_d(min(int(calendar.timegm(time.strptime(k, "%Y-%m-%d")) * 1000) for k in self.daily)) if self.daily else None,
                "note": "sent/arrived count every event relayed (messages, reactions, edits, receipts); lost = never "
                        "rebuilt on the high side; forwarded = handed to the destination (Gotham Chat) without an "
                        "error, per receiver (a receiver with FORWARD_TO off forwards nothing); counts start at "
                        "counting_since_utc: say so when asked about earlier days"}

    def groups_summary(self, rooms=None, conns=None):
        """Per Signal group, for its window on the page: each route it takes (room), sent this run by the subscriber
        vs arrived here, and that route's state; plus the lost messages of its rooms (a lost message's group is
        unknown). Group IDs and names and counts only, never content. A route's state follows its wires: the worst of
        its own arrival check and its connections (subscriber -> room, room -> each receiver), so a group's dot never
        says UP while its route on the diagram is red, and a group whose subscriber is down shows it."""
        rooms = rooms if rooms is not None else self.rooms_summary()
        conns = {c["key"]: c for c in (conns if conns is not None else self.connections(rooms))}
        names = {gid[:8]: (gid, name) for gid, name in self.group_names.items()}
        out = {}
        order = ["up", "idle", "warn", "stale", "unknown", "down"]
        for k, p in self.parts.items():
            if p.get("comp") != "subscriber" or not p.get("state") or (p.get("name") or "").startswith(TEST_PREFIX):
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
                wires = [conns.get(f"low/{p.get('host')}/~link/send/{p.get('name')}/{room}")]
                wires += [c for key, c in conns.items() if key.startswith(f"high/~link/route/{room}/")]
                for c in filter(None, wires):
                    if order.index(c["state"]) > order.index(state):
                        state = c["state"]
                out.setdefault(gid, {"name": name, "routes": [], "lost": {}})["routes"].append(
                    {"room": room, "title": r.get("title") or room, "sent": n, "arrived": got, "last_sent_age_s": age, "last_arrival": last or None,
                     "receivers": r.get("receivers", []), "state": state, "from": k})
                if r.get("lost"):
                    out[gid]["lost"][room] = r["lost"]
        for gid, name in self.group_names.items():           # groups seen before, not sent by any subscriber now
            out.setdefault(gid, {"name": name, "routes": [], "lost": {}})
        for g in out.values():
            states = [r["state"] for r in g["routes"]]
            g["state"] = (max(states, key=order.index) if states else "idle")
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
                                           "senders": [], "receivers": [], "misrouted": {}, "missing": 0})
        for k, p in self.parts.items():
            if p.get("comp") != "subscriber":
                continue
            if p.get("state") in ("stale", "down"):       # not sending: listed as a sender only (its routes: "nothing can be sent")
                for name, *_ in self.streams(p):
                    if name:
                        room(name)["senders"].append(k)
                continue
            for name, s_, m, m_age in self.streams(p):
                if not name:
                    continue
                r = room(name)
                r["subs"].append(k)
                r["senders"].append(k)
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

    # ---- connections: each link's own state (the page's wires; high/docs/monitoring-redesign.md, section 4) ----
    def connections(self, rooms=None):
        """[{key, kind, from, to, state, code, text, label}]: one per link between parts. A link is UP when what
        flows on it gets through now; UNKNOWN when an end is silent (nothing to tell from); otherwise its own reason.
        Keys start with the side they belong to (low/<host>/..., high/...), so incidents can take them as parts."""
        now, out = now_ms(), []
        shown = {k: self.shown(p) for k, p in self.parts.items() if p.get("state")}
        rooms = rooms if rooms is not None else self.rooms_summary()
        quiet = lambda p: not p or p["state"] in ("stale", "unknown") or (p["state"] == "down" and (now - p.get("last", 0)) / 1000 > STALE_S)
        def name(p):                                     # short: the diagram already shows which host
            if not p:
                return "?"
            return (p.get("name") if p.get("comp") in ("subscriber", "receiver") else
                    p.get("inst") if p.get("comp") == "signal-cli" and p.get("inst") else LABEL.get(p.get("comp"), p.get("comp")))

        def add(key, kind, a, b, state, code, text, label):
            out.append({"key": key, "kind": kind, "from": a, "to": b, "state": state, "code": code, "text": text,
                        "label": label, "comp": "connection", "name": label, "last": now})
        for k, cli in shown.items():
            if cli.get("comp") != "signal-cli":
                continue
            host, inst = cli["host"], cli["inst"]
            mon = shown.get(f"low/{host}/signal-monitor")
            srv = shown.get(f"low/{host}/{inst}/signal-servers")
            if srv:
                add(f"low/{host}/~link/keepalive/{inst}", "keepalive", srv["key"], k, srv["state"],
                    None if srv["state"] == "unknown" else srv.get("code"),
                    "can't tell while signal-cli's check doesn't get through" if srv["state"] == "unknown" else srv.get("text") or "",
                    f"{name(cli)} to Signal's servers")
            # signal-monitor -> signal-cli: the health check's own path
            c = cli.get("code")
            st = (("unknown", None, "signal-monitor is silent") if quiet(mon) else
                  ("down", c, cli.get("text") or "the check fails") if c in CHECK_FAILED or c == "MONITOR_CHECK_FAILED" else
                  ("warn", c, "signal-cli is starting") if c == "STARTING" else
                  ("warn", "CHECK_SLOW", f"the check took {cli['info']['check_ms']} ms")
                  if isinstance((cli.get("info") or {}).get("check_ms"), int) and cli["info"]["check_ms"] > SLOW_CHECK_MS else
                  ("up", "OK", "answers"))
            add(f"low/{host}/~link/check/{inst}", "check", f"low/{host}/signal-monitor", k, *st,
                f"signal-monitor to {name(cli)} (health check)")
            for sk, sub in shown.items():
                if sub.get("comp") != "subscriber" or sub.get("host") != host or sub.get("inst") != inst:
                    continue
                sc, since = sub.get("code"), (now - (sub.get("code_since") or now)) / 1000
                cli_off = cli["state"] == "down" and cli.get("code") != "MONITOR_CHECK_FAILED"
                st = (("unknown", None, f"{name(sub)} isn't reporting") if quiet(sub) or sc == "SUB_STATUS_LOST" else
                      ("down", "CLI_DOWN", f"{name(cli)} is DOWN") if cli_off else
                      ("down", "NO_STREAM", f"{name(sub)} is stopped") if sub["state"] == "down" and sc in ("NO_STATUS", "CONTAINER_PAUSED") else
                      (("warn" if since < RECONNECT_S else "down"), "NOT_CONNECTED",
                       (sub.get("text") or "not connected").replace("running, but not connected to signal-cli: ", "")
                       + ("; reconnecting" if since < RECONNECT_S else f"; for {int(since)} s"))
                      if sc == "NOT_CONNECTED" else
                      ("warn", "STARTING", "connecting") if sc == "STARTING" else
                      ("up", "OK", "connected"))
                add(f"low/{host}/~link/events/{sub['name']}", "events", k, sk, *st, f"{name(cli)} to {name(sub)} (events)")
                st = (("unknown", None, "signal-monitor is silent") if quiet(mon) else
                      ("down", "NO_STATUS", "no status for 35 s" + (": its container runs" if sc == "SUB_STATUS_LOST" else
                                                                     ": it is stopped" if sub["state"] == "down" else ""))
                      if sc in ("NO_STATUS", "SUB_STATUS_LOST") else ("up", "OK", "status arriving"))
                add(f"low/{host}/~link/status/{sub['name']}", "status", sk, f"low/{host}/signal-monitor", *st,
                    f"{name(sub)} to signal-monitor (status)")
                # subscriber -> each room it sends to
                for room, _, _, _ in self.streams(sub):
                    if not room:
                        continue
                    r = rooms.get(room) or {}
                    failing = now - (sub.get("send_failed_at") or 0) < STATE_S * 1000
                    st = (("unknown", None, f"nothing can be sent: {name(sub)} is DOWN") if sub["state"] == "down" else
                          ("unknown", None, f"can't tell: {name(sub)} is {sub['state'].upper()}")
                          if sub["state"] in ("stale", "unknown") else
                          ("down", "SEND_FAILED", "its UDP sends fail now: messages don't leave the host") if failing else
                          ("down", "ROOM_UNSERVED", "no receiver serves this room: its messages go nowhere (a diode route "
                           "missing, or the receiver not set up)") if not r.get("receivers") and self.receivers() else
                          ("up", "OK", "sending"))
                    add(f"low/{host}/~link/send/{sub['name']}/{room}", "send", sk, f"room/{room}", *st,
                        f"{name(sub)} to {self.room_title(room)}")
        # room -> receiver: does what was sent arrive?
        for room, r in rooms.items():
            senders = [shown[k] for k in r.get("subs") or [] if k in shown]
            for rk in r.get("receivers") or []:
                rx = shown.get(rk)
                miss = [x for x in (self.chat_missing.get(rk) or []) if x[4] == room]
                if quiet(rx):
                    st = ("unknown", None, f"{name(rx)} isn't reporting")
                elif rx["state"] == "down":
                    st = ("unknown", None, f"{name(rx)} is DOWN: {rx.get('text') or ''}".rstrip(": "))
                elif miss:
                    sub, m, done, sent_at, _ = miss[0]
                    st = ("down", "CHAT_NOT_ARRIVING", f"{name(sub)} sent message #{m} at {utc_s(sent_at)} UTC; the last "
                          f"that arrived from that run is #{done}" + (f" (and {len(miss) - 1} more)" if len(miss) > 1 else "")
                          + ". Monitoring still gets through, so the chat port or its diode route loses messages")
                elif path_bad := [c for c in PATH_CODES if rk in self.recent and self.recent[rk].n(c, STATE_S)]:
                    # junk, gaps or broken messages at its receiver just now (colour = now): the way in, not the receiver
                    n = self.recent[rk].n(path_bad[0], STATE_S)
                    title = (self.runbook.get(path_bad[0]) or {}).get("title") or path_bad[0].replace("_", " ").lower()
                    st = ("warn", path_bad[0], f"{title} ({n} just now)")
                elif r.get("senders") and not senders:
                    st = ("unknown", None, "nothing can be sent: its subscriber is not running")
                else:
                    st = ("up", "OK", "arriving")
                add(f"high/~link/route/{room}/{rx['name'] if rx else rk.split('/')[-1]}", "route", f"room/{room}", rk, *st,
                    f"{self.room_title(room)} to {name(rx)}")
        # the high side's own links
        for rk, rx in shown.items():
            if rx.get("comp") != "receiver":
                continue
            i = rx.get("info") or {}
            silent = quiet(rx)
            if i.get("forward_to") != "":
                last = i.get("forward_last")
                st = (("unknown", None, f"{name(rx)} isn't reporting") if silent else
                      ("down", "FORWARD_FAILED", "the latest hand-off to my_forward failed") if last == "failed" else
                      ("warn", "FORWARD_SLOW", "the latest hand-off to my_forward was slow (over 2 s)") if last == "slow" else
                      ("idle", None, "nothing handed over since it started") if not i.get("forwarded") else
                      ("up", "OK", "the latest hand-off worked"))
                add(f"high/~link/forward/{rx['name']}", "forward", rk, f"gotham/{rk}", *st,
                    f"{name(rx)} to {i.get('forward_to') or 'Gotham Chat'}")
            st = (("unknown", None, f"{name(rx)} isn't reporting") if silent else
                  ("down", "DELIVER_FAILED", "its latest delivery to the console failed") if i.get("deliver_last") == "failed" else
                  ("up", "OK", "delivering"))
            add(f"high/~link/deliver/{rx['name']}", "deliver", rk, "high/console", *st, f"{name(rx)} to the console")
        # signal-monitor -> the diode's CHAT|MONITOR route -> monitor-receiver: silence, then loss in the last 5 min
        for host, l in self.link_summary().items():
            add(f"low/{host}/~link/monitor", "monitor", f"low/{host}/signal-monitor", "high/monitor-receiver",
                "warn" if l["state"] == "stale" else l["state"],
                "DIODE_LOSS" if l["state"] in ("warn", "down") and "lost" in (l.get("text") or "") else None,
                l.get("text") or "", f"Monitoring from {host}")
        mrx = shown.get("high/monitor-receiver")
        if mrx:
            i = mrx.get("info") or {}
            st = (("unknown", None, "monitor-receiver isn't reporting") if quiet(mrx) else
                  ("warn", "MONRX_WAITING", f"{i['waiting']} monitoring message(s) waiting: the console doesn't take them")
                  if isinstance(i.get("waiting"), int) and i["waiting"] > 0 else
                  ("warn", "MONRX_REFUSED", "the console refused monitoring messages just now")
                  if now - (mrx.get("nd_at") or 0) < STATE_S * 1000 else
                  # it can't tell us it is waiting while we are out of its reach: its heartbeat (every 30 s) is late
                  ("warn", "MONRX_WAITING", f"nothing from monitor-receiver for {int((now - self.mrx_post_at) / 1000)} s "
                   "(it sends at least every 30 s): its messages aren't getting to the console")
                  if self.mrx_post_at and now - self.mrx_post_at > MRX_LATE_S * 1000 else ("up", "OK", "passing on"))
            add("high/~link/ingest/monitor-receiver", "ingest", "high/monitor-receiver", "high/console", *st,
                "monitor-receiver to the console")
        for c in out:
            seen = self.conn_seen.get(c["key"])
            c["since"] = seen[1] if seen and seen[0] == c["state"] else now
        return out

    def state(self):
        rooms = self.rooms_summary()
        conns = self.connections(rooms)
        return {"parts": sorted((self.shown(p) for p in self.parts.values() if p["state"]), key=lambda p: p["key"]),
                "connections": conns,
                "rooms": rooms,
                "room_titles": self.room_titles(),
                "groups": self.groups_summary(rooms, conns),
                "links": self.link_summary(),
                "incidents": self.incidents.summary(),
                "containers": self.containers,
                "facts_ok": self.facts_ok,
                "analyst": self.analyst,
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
        for step in (self._step_chat, self._step_console, self._step_gone, self._step_loose, self.evaluate, self._settle, self.metrics,
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

    def _link_changes(self, conns):
        """A connection's change of state goes in the Log like a box's ("... (events) is DOWN: ... (was UP)."), so
        a cut link is in the history too. Not into or out of UNKNOWN (an end went silent: its box says so) or IDLE."""
        now = now_ms()
        grace = time.time() - self.started < STALE_S
        for c in conns:
            old = self.conn_seen.get(c["key"])
            if not old:
                self.conn_seen[c["key"]] = [c["state"], now]
            elif old[0] != c["state"]:
                was = old[0]
                self.conn_seen[c["key"]] = [c["state"], now]
                if not grace and not {was, c["state"]} & {"unknown", "idle"}:
                    self._event(c["key"], f"LINK_{c['state'].upper()}", SEV[c["state"]],
                                f"{c['label']} is {c['state'].upper()}" + (f": {c['text']}" if c.get("text") and c["state"] != "up" else "")
                                + f" (was {was.upper()}).")
            c["since"] = self.conn_seen[c["key"]][1]
        for k in set(self.conn_seen) - {c["key"] for c in conns}:
            del self.conn_seen[k]

    def _step_incidents(self):
        shown = {k: self.shown(p) for k, p in self.parts.items() if p["state"]}
        conns = self.connections()
        self._link_changes(conns)
        for c in conns:                              # links with a problem take part like boxes (not the ones a box
            if c["state"] in ("warn", "down") and c["kind"] in ("send", "route", "forward", "deliver", "ingest"):
                shown[c["key"]] = c                  # already explains: events, status, check, keep-alive)
        traffic = [p.get("traffic_at") for p in self.receivers().values() if p.get("traffic_at")]
        self.incidents.update(shown, self.link_summary(), max(traffic) if traffic else None, self.test_running())
        if self.incidents.dirty:
            self.incidents.dirty, self.dirty = False, True
