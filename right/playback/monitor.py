"""Playback's monitoring: the parts of the relay, their states, the event timeline and the diode's health.

Messages come from monitor-receiver (POST /monitor), which passes on what signal-monitor sends across the diode:
  hb  signal-monitor's own heartbeat, every 30 s       {"t":"hb","host","boot","seq","ts","up_s","info"}
  st  one part's state (signal-cli, a subscriber)     {"t":"st",...,"inst","comp","name","state","info","text"}
      every 30 s, and at once when it changes
  ev  something that isn't a state (an error line)    {"t":"ev",...,"inst","comp","name","code","sev","text"}
plus monitor-receiver's own heartbeat: {"t":"rx_hb","ts","info"}. The receiver's counters (POST /stats) are its
heartbeat.

The left side reports states only; this module turns every change of state into an event, in one place. A lost
packet can't lose an alert: the next status carries the state again. A part that stops reporting goes STALE after
STALE_S and DOWN after DOWN_S. If a host's signal-monitor is silent, the parts it reports on are UNKNOWN, not DOWN:
the problem may be the monitor, the host or the diode.

Part keys:  right/receiver   right/monitor-receiver   right/playback
            left/<host>/signal-monitor   left/<host>/<instance>/signal-cli   left/<host>/<instance>/subscriber/<name>
"""
import logging, re, time
from collections import deque
from store import now_ms, BUCKET_MS

STALE_S, DOWN_S = 90, 180
STATES = ("up", "warn", "down")                      # what the left side may report
BAD = ("warn", "stale", "unknown", "down")
SEV = {"up": "info", "warn": "warning", "stale": "warning", "unknown": "warning", "down": "error"}
NAME = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
LABEL = {"signal-monitor": "signal-monitor", "signal-cli": "signal-cli", "subscriber": "subscriber",
         "receiver": "receiver", "monitor-receiver": "monitor-receiver", "playback": "playback"}
log = logging.getLogger("playback")


def clean(s, n=300):
    """Untrusted text from across the diode: a string, printable, at most n characters."""
    s = "" if s is None else str(s)
    return "".join(c if c.isprintable() else " " for c in s)[:n]


def small(info):
    """Keep a status's info dict small and flat: short keys, numbers/short strings/booleans only."""
    out = {}
    if isinstance(info, dict):
        for k, v in list(info.items())[:20]:
            if isinstance(k, str) and NAME.match(k):
                out[k] = v if isinstance(v, (int, float, bool)) or v is None else clean(v, 120)
    return out


class Monitor:
    def __init__(self, store, publish):
        self.store, self.publish = store, publish
        self.parts = store.parts()
        self.seqs = store.get("mon_seq", {})           # host -> {"boot", "seq"}: for counting lost packets
        self.events = deque(store.mon_events(limit=300), maxlen=300)
        self.started = time.time()
        self.dirty = True
        self.links, self.links_at = {}, 0
        self._part("right/playback", side="right", comp="playback", name="playback", state="up",
                   info={"started": now_ms()}, last=now_ms())

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

    def touch(self, key, info=None, **fields):
        """A heartbeat from a part checked directly (receiver, monitor-receiver, a signal-monitor)."""
        self._part(key, last=now_ms(), info=small(info) if info is not None else
                   self.parts.get(key, {}).get("info", {}), **fields)

    # ---- messages from monitor-receiver ----
    def handle(self, m):
        """One message (dict). Returns None if accepted, else a short reason."""
        t = m.get("t")
        if t == "rx_hb":
            self.touch("right/monitor-receiver", m.get("info"), side="right", comp="monitor-receiver",
                       name="monitor-receiver")
            return None
        host = m.get("host")
        if t not in ("hb", "st", "ev") or not (isinstance(host, str) and NAME.match(host)):
            return "unknown message type or bad host name"
        try:
            seq, ts, rx = int(m["seq"]), int(m["ts"]), int(m.get("rx") or now_ms())
            boot = clean(m["boot"], 32)
        except (KeyError, TypeError, ValueError):
            return "missing seq/ts/boot"
        self._count(host, boot, seq, ts, rx)
        mkey = f"left/{host}/signal-monitor"
        if t == "hb":
            info = small(m.get("info"))
            info["up_s"] = m.get("up_s") if isinstance(m.get("up_s"), int) else None
            self.touch(mkey, info, side="left", host=host, comp="signal-monitor", name="signal-monitor",
                       reported=m.get("state") if m.get("state") in STATES else "up", text=clean(m.get("text"), 200))
            return None
        if mkey not in self.parts:                       # a status before the first heartbeat still means alive
            self.touch(mkey, {}, side="left", host=host, comp="signal-monitor", name="signal-monitor")
        else:
            self.parts[mkey]["last"] = now_ms()
        inst, comp, name = m.get("inst"), m.get("comp"), m.get("name") or m.get("comp")
        if t == "ev" and comp == "signal-monitor":       # about the monitor itself (e.g. it started)
            sev = m.get("sev") if m.get("sev") in ("info", "warning", "error", "critical") else "warning"
            code = re.sub(r"[^A-Z0-9_]", "", clean(m.get("code"), 40).upper()) or "EVENT"
            self._event(mkey, code, sev, clean(m.get("text")), notify=sev in ("error", "critical"))
            return None
        if not all(isinstance(x, str) and NAME.match(x) for x in (inst, comp, name)) or comp not in ("signal-cli", "subscriber"):
            return "bad instance/component/name"
        key = f"left/{host}/{inst}/signal-cli" if comp == "signal-cli" else f"left/{host}/{inst}/subscriber/{name}"
        if t == "st":
            state = m.get("state")
            if state not in STATES:
                return "bad state"
            self._part(key, side="left", host=host, inst=inst, comp=comp, name=name, last=now_ms(),
                       reported=state, info=small(m.get("info")), text=clean(m.get("text"), 200))
            self.evaluate()
            return None
        sev = m.get("sev") if m.get("sev") in ("info", "warning", "error", "critical") else "warning"
        code = re.sub(r"[^A-Z0-9_]", "", clean(m.get("code"), 40).upper()) or "EVENT"
        if key not in self.parts:
            self._part(key, side="left", host=host, inst=inst, comp=comp, name=name, last=now_ms())
        self._event(key, code, sev, clean(m.get("text")), notify=sev in ("error", "critical"))
        return None

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
        if p["comp"] == "playback":
            return "up", False
        age = (now - p["last"]) / 1000
        if p["side"] == "left" and p["comp"] != "signal-monitor":
            mon = self.parts.get(f"left/{p['host']}/signal-monitor")
            if mon and mon["state"] in ("stale", "down", "unknown"):
                return "unknown", True
        if age > DOWN_S:
            return "down", True
        if age > STALE_S:
            return "stale", True
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
            why = ({"stale": f"nothing heard for {STALE_S} s", "down": f"nothing heard for {DOWN_S // 60} min",
                    "unknown": "its signal-monitor is silent, so its state is unknown"}.get(new) if silent
                   else p.get("text")) or ""
            text = f"{self.title(p)} is {new.upper()}" + (f": {why}" if why else "") + (f" (was {old.upper()})." if old else ".")
            self._event(key, f"{p['comp'].upper().replace('-', '_')}_{new.upper()}", SEV[new], text,
                        notify=new in ("down", "unknown") or (new == "up" and old in ("down", "unknown", "stale")))

    def title(self, p):
        where = f" ({p['inst']} on {p['host']})" if p.get("inst") else f" (on {p['host']})" if p.get("host") else ""
        name = p["name"] if p["comp"] == "subscriber" else LABEL.get(p["comp"], p["comp"])
        return f"{'subscriber ' if p['comp'] == 'subscriber' else ''}{name}{where}"

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
            self.dirty = True

    # ---- for the page ----
    def link_summary(self):
        if time.time() - self.links_at > 30:
            now = now_ms()
            self.links = {}
            for host in sorted({p["host"] for p in self.parts.values() if p.get("side") == "left" and p.get("host")}):
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
        for host, l in self.links.items():          # fresh every time: when the last monitoring packet arrived
            l["last_ms"] = self.parts.get(f"left/{host}/signal-monitor", {}).get("last")
        return self.links

    def state(self):
        return {"parts": sorted((p for p in self.parts.values() if p["state"]), key=lambda p: p["key"]),
                "links": self.link_summary(),
                "now": now_ms()}

    def snapshot(self):
        return {**self.state(), "events": list(self.events)}

    def tick(self):
        """Every few seconds: re-check ages, and push the state to browsers if anything changed."""
        self.parts["right/playback"]["last"] = now_ms()
        self.evaluate()
        if self.dirty:
            self.dirty = False
            self.publish({"op": "mon", **self.state()})
