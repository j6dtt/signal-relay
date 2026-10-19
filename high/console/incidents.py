"""Incidents: periods of trouble, each with a likely cause worked out by fixed rules.

One incident per scope at a time: "high" (this side's parts) or "low/<host>" (one low-side host, its signal-cli
instances, subscribers and diode link). It opens when a part in the scope stops being UP, collects every part that
has a problem while it is open, and closes once all of them have been UP for CLOSE_AFTER_S.

The likely cause comes from rules in diagnose() below, in code on purpose: a model asked "why is signal-cli UNKNOWN?"
looked at signal-cli, not at the silent signal-monitor that explains it. runbook.json says what each cause means
and what to check; this file only decides which one applies. Nothing here runs a check or changes anything.
"""
import json, logging, time
from store import now_ms

STALE_S, DOWN_S = 90, 180                 # same as monitor.py
CLOSE_AFTER_S = 60                        # all clear this long before an incident closes (no flapping)
STARTING_GRACE_S = 90                     # "starting up" alone opens an incident only if it lasts this long
BAD = ("warn", "stale", "unknown", "down")
# Most specific and most serious first: the first finding is the likely cause, the others are listed as "also".
PRIORITY = ["MONRX_DOWN", "LEFT_SILENT", "MONITOR_ONLY_SILENT", "CONTAINER_OOM", "CONTAINER_RESTARTING",
            "CONTAINER_CRASHED", "CONTAINER_STOPPED",
            "DEVICE_UNLINKED", "CLIENT_EXPIRED", "RECEIVE_STALLED", "SIGNAL_UNREACHABLE",
            "CLI_UNREACHABLE", "CLI_TIMEOUT", "CLI_TLS_FAILED", "CLI_HTTP_ERROR", "CLI_DOWN", "CLI_GATEWAY_CLOSED",
            "STORAGE_ERROR",
            "SUB_STOPPED", "SENDER_DEAD", "SENDER_STALLED", "SEND_FAILED", "SUB_GATEWAY_CLOSED", "SUB_TLS_FAILED",
            "SUB_UNREACHABLE", "SUB_NOT_CONNECTED", "EVENTS_MISSED", "CLI_RESTART_LOSS", "SUBSCRIBER_TWICE",
            "ROOM_UNSERVED", "ROOM_MISMATCH",
            "RX_STALLED", "RECEIVER_DOWN", "CHAT_NOT_ARRIVING", "FORWARD_FAILED", "DELIVER_FAILED", "DB_ERROR",
            "CHECKS_FAILING", "MESSAGE_LOST", "MESSAGE_BROKEN", "RX_OVERFLOW", "MESSAGE_SKIPPED", "SENDER_ERROR",
            "EVENT_DROPPED", "DECRYPT_FAILED", "UNTRUSTED_IDENTITY", "HANDLE_ERROR", "WRITE_FAILED", "STREAM_STALLED",
            "SIGNAL_RECONNECTING", "SIGNAL_FLAPPING", "SIGNAL_UNKNOWN", "DIODE_LOSS", "JVM_MEMORY", "FORWARD_SLOW", "LOOP_LAG",
            "QUEUE_BACKLOG", "QUEUE_SLOW", "GATEWAY_FULL", "GATEWAY_REFUSED", "RPC_BLOCKED", "BAD_PACKET",
            "NOTICE_FAILED", "INGEST_FAILED", "AI_FAILED", "PACKET_TOO_BIG", "STARTING", "CLOCK_SKEW",
            "MONITOR_DISK_LOW", "DISK_LOW", "CONTAINER_MEM_HIGH", "CONTAINER_CPU_HIGH", "CONTAINER_PIDS_HIGH",
            "FACTS_UNAVAILABLE", "CERT_EXPIRING", "OTHER", "UNCLEAR"]
CLI_CODES = {"UNREACHABLE": "CLI_UNREACHABLE", "GATEWAY_CLOSED": "CLI_GATEWAY_CLOSED", "TLS_FAILED": "CLI_TLS_FAILED",
             "TIMEOUT": "CLI_TIMEOUT", "HTTP_ERROR": "CLI_HTTP_ERROR"}
SUB_STREAM = {"GATEWAY_CLOSED": "SUB_GATEWAY_CLOSED", "TLS_FAILED": "SUB_TLS_FAILED", "UNREACHABLE": "SUB_UNREACHABLE"}
log = logging.getLogger("console")
# WARN-only, well known: no automatic AI question ("Ask anyway" instead)
QUIET = ("STARTING", "CERT_EXPIRING", "MONITOR_DISK_LOW", "QUEUE_BACKLOG", "QUEUE_SLOW", "DISK_LOW", "CLOCK_SKEW",
         "PACKET_TOO_BIG", "BAD_PACKET", "NOTICE_FAILED", "GATEWAY_REFUSED", "RPC_BLOCKED", "FORWARD_SLOW", "LOOP_LAG",
         "SIGNAL_RECONNECTING", "SIGNAL_UNKNOWN", "AI_FAILED", "FACTS_UNAVAILABLE", "CONTAINER_CPU_HIGH")
# Always worth an AI question: the rules can't settle it, or messages may be lost (how many? what next?)
SPECIAL = ("UNCLEAR", "CONTAINER_OOM", "CONTAINER_RESTARTING", "CONTAINER_CRASHED", "DIODE_LOSS", "OTHER",
           "EVENTS_MISSED", "CLI_RESTART_LOSS", "CHAT_NOT_ARRIVING", "MESSAGE_LOST", "SEND_FAILED", "FORWARD_FAILED")
KNOWN = set()                             # every cause code the runbook explains (set by Incidents from runbook.json)


def container_finding(p, now):
    """(cause code, evidence) from a part's container facts, or None if they don't explain anything."""
    c = p.get("container") or {}
    if not c:
        return None
    name, x = c.get("n"), c.get("x")
    when = f" at {utc(c['fi'])} UTC" if c.get("fi") else ""
    if c.get("s") == "restarting" or (c.get("s") == "running" and c.get("r", 0) > 0 and c.get("st") and now - c["st"] < 600_000):
        return "CONTAINER_RESTARTING", f"container {name} has been restarted by Docker {c.get('r')} time(s); last start {utc(c.get('st'))} UTC"
    if c.get("s") not in ("exited", "dead"):
        return None
    if c.get("o"):
        return "CONTAINER_OOM", f"container {name} was killed for using too much memory (OOM){when}"
    if x in (0, 143):
        return "CONTAINER_STOPPED", f"container {name} was stopped cleanly{when} (exit code {x}): stopped on purpose, not a crash"
    if x == 137:
        return "CONTAINER_STOPPED", f"container {name} was killed{when} (exit code 137, SIGKILL; not out of memory): docker kill, or a stop that took over 10 s"
    return "CONTAINER_CRASHED", f"container {name} crashed{when} (exit code {x})"


def utc(ms):
    import time as _t
    return _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime(ms / 1000)) if ms else "?"


def ago(ms, now):
    s = max(0, (now - ms) / 1000)
    return f"{round(s)} s" if s < 120 else f"{round(s / 60)} min" if s < 7200 else f"{round(s / 3600)} h"


def label(p):
    sub = p.get("name", "")
    name = ((sub if "subscriber" in sub else f"subscriber {sub}") if p.get("comp") == "subscriber" else
            "signal-cli's link to Signal" if p.get("comp") == "signal-servers" else
            p.get("name") or "receiver" if p.get("comp") == "receiver" else p.get("comp", p["key"]))
    return name + (f" ({p['inst']})" if p.get("inst") not in (None, "signal-cli") else "")


def scope_of(key):
    if key.startswith("high/"):
        return "high"
    parts = key.split("/")
    return f"low/{parts[1]}" if key.startswith("low/") and len(parts) > 2 else None


def silent(p, now):
    return (now - p.get("last", now)) / 1000 > STALE_S


def known(code, fallback="UNCLEAR"):
    """The part's own code as the cause, if the runbook explains it."""
    return code if code in KNOWN else fallback


class Finding:
    def __init__(self, code):
        self.code, self.evidence, self.parts = code, [], []

    def add(self, text, key=None):
        if text and text not in self.evidence:
            self.evidence.append(text)
        if key and key not in self.parts:
            self.parts.append(key)
        return self


def diagnose(scope, parts, links, traffic_at, now):
    """Findings for one scope, likely cause first. parts: key -> part (with its shown state); links: host -> summary
    (with "state"); traffic_at: when chat packets last arrived at any receiver (ms) or None."""
    found = {}

    def f(code):
        return found.setdefault(code, Finding(code))

    def p_(key):
        return parts.get(key)

    monrx = p_("high/monitor-receiver")
    monrx_bad = bool(monrx and monrx["state"] in ("stale", "down"))
    if scope == "high":
        if monrx_bad:
            f("MONRX_DOWN").add(f"monitor-receiver last heard {ago(monrx['last'], now)} ago", monrx["key"])
        for rx in sorted((p for p in parts.values() if p.get("comp") == "receiver"), key=lambda p: p["key"]):
            if rx["state"] in ("stale", "down") and silent(rx, now):
                f("RECEIVER_DOWN").add(f"{label(rx)}'s counters last arrived {ago(rx['last'], now)} ago", rx["key"])
        for p in sorted((p for k, p in parts.items() if scope_of(k) == "high"), key=lambda p: p["key"]):
            if p["state"] in ("warn", "down") and not silent(p, now) and p.get("code") not in (None, "OK"):
                fd = f(known(p["code"])).add(f"{label(p)}: {p.get('text') or p['state'].upper()}", p["key"])
                if p["code"] == "CHAT_NOT_ARRIVING":
                    fd.add("monitoring packets from the low side still arrive, so it is the chat port (6510), its "
                           "diode path, or the receiver")
                for other in (p.get("active") or [])[1:4]:
                    if other in KNOWN and other != p["code"]:
                        f(other).add(f"{label(p)} also: {other.replace('_', ' ').lower()}", p["key"])
    else:
        host = scope.split("/", 1)[1]
        mine = {k: p for k, p in parts.items() if k.startswith(scope + "/")}
        mon = mine.get(f"{scope}/signal-monitor")
        link = links.get(host) or {}
        if mon and mon["state"] in ("stale", "down") and silent(mon, now):
            heard = f"signal-monitor last heard {ago(mon['last'], now)} ago"
            if monrx_bad:
                f("MONRX_DOWN").add(heard, mon["key"]).add(
                    f"monitor-receiver is {monrx['state'].upper()} too (last heard {ago(monrx['last'], now)} ago), so "
                    "nothing from the low side could get through", monrx["key"])
            elif traffic_at and traffic_at > mon["last"] + 35_000:
                f("MONITOR_ONLY_SILENT").add(heard, mon["key"]).add(
                    f"chat packets still arrived {ago(traffic_at, now)} ago, after the last monitoring packet")
            else:
                f("LEFT_SILENT").add(heard, mon["key"]).add(
                    "no chat packets have arrived since either" if traffic_at else "no chat packets seen yet")
            unknown = [label(p) for p in mine.values() if p["state"] == "unknown"]
            if unknown:
                found[next(iter(found))].add(f"UNKNOWN because of that: {', '.join(sorted(unknown))}")
        else:
            if mon and mon["state"] == "warn" and not (mon.get("code") in (None, "OK") and (mon.get("container") or {}).get("b")):
                code = {"DISK_LOW": "MONITOR_DISK_LOW"}.get(mon.get("code")) or known(mon.get("code"))
                f(code).add(f"signal-monitor: {mon.get('text') or 'WARN'}", mon["key"])
            insts = sorted({p["inst"] for p in mine.values() if p.get("inst")})
            for inst in insts:
                cli = mine.get(f"{scope}/{inst}/signal-cli")
                subs = [p for p in mine.values() if p.get("inst") == inst and p.get("comp") == "subscriber"]
                cli_down = bool(cli and cli["state"] in ("stale", "down"))
                cf = container_finding(cli, now) if cli_down else None
                if cf:
                    f(cf[0]).add(cf[1], cli["key"]).add(f"signal-cli ({inst}): {cli.get('text') or 'DOWN'}")
                elif cli and cli["state"] == "down":
                    fd = f(CLI_CODES.get(cli.get("code")) or known(cli.get("code"), "CLI_DOWN")).add(
                        f"signal-cli ({inst}): {cli.get('text') or 'DOWN'}", cli["key"])
                    connected = [label(s) for s in subs if s["state"] == "up"]
                    lost = [label(s) for s in subs if s["state"] == "down" and s.get("code") == "NOT_CONNECTED"]
                    if cli.get("code") == "GATEWAY_CLOSED" and connected:
                        fd.add(f"{', '.join(connected)} still connected, so signal-cli itself works: only "
                               "signal-monitor's certificate is refused")
                    if lost:
                        fd.add(f"{', '.join(lost)} lost the stream too (expected while signal-cli is down)",
                               next(s["key"] for s in subs if label(s) == lost[0]))
                elif cli and cli["state"] == "warn" and not (cli.get("code") in (None, "OK") and (cli.get("container") or {}).get("b")):
                    f(known(cli.get("code"))).add(f"signal-cli ({inst}): {cli.get('text') or 'WARN'}", cli["key"])
                srv = mine.get(f"{scope}/{inst}/signal-servers")
                if srv and srv["state"] in ("warn", "down", "stale") and not cli_down:
                    fd = f(known(srv.get("code"))).add(f"signal-cli ({inst}) and Signal's servers: "
                                                         f"{srv.get('text') or srv['state'].upper()}", srv["key"])
                    working = [label(x) for x in subs if x["state"] == "up"]
                    if working and srv["state"] == "down":
                        fd.add(f"{', '.join(working)} still connected to signal-cli: signal-cli runs, only its link "
                               "to Signal is broken, so no new messages come in")
                elif srv and srv["state"] == "unknown" and cli_down and found:
                    found[next(iter(found))].add("signal-cli's link to Signal is unknown while signal-cli is down", srv["key"])
                for s in subs:
                    if s["state"] not in BAD or s["state"] == "unknown":
                        continue
                    if cli_down and s.get("code") == "NOT_CONNECTED":
                        found[next(iter(found))].add(f"{label(s)} lost the stream too (expected while signal-cli is down)", s["key"]) if found else None
                        continue                       # explained by signal-cli above
                    cf = container_finding(s, now) if s["state"] == "down" else None
                    if cf:
                        f(cf[0]).add(cf[1], s["key"]).add(f"{label(s)}: {s.get('text') or 'DOWN'}")
                        continue
                    code = s.get("code")
                    if code in (None, "OK") and (s.get("container") or {}).get("b"):
                        continue                       # only its container's limits: see the container finding below
                    if code == "NO_STATUS":
                        c = "SUB_STOPPED"
                    elif code == "NOT_CONNECTED":
                        c = SUB_STREAM.get((s.get("info") or {}).get("stream_code"), "SUB_NOT_CONNECTED")
                    else:
                        c = known(code)
                    fd = f(c).add(f"{label(s)}: {s.get('text') or s['state'].upper()}", s["key"])
                    for other in (s.get("active") or [])[1:4]:
                        if other in KNOWN and other != c:
                            f(other).add(f"{label(s)} also: {other.replace('_', ' ').lower()}", s["key"])
                    if c.startswith("SUB_") and c != "SUB_STOPPED" and cli and cli["state"] == "up":
                        fd.add(f"signal-monitor's own check of signal-cli ({inst}) works")
            for p in mine.values():                       # a container near its limits (container facts)
                for b in (p.get("container") or {}).get("b") or []:
                    if p["state"] == "warn":
                        c = p["container"]
                        f(f"CONTAINER_{b.upper()}_HIGH").add(f"{label(p)}: container {c.get('n')} " + {
                            "mem": f"uses {c.get('m')}% of its memory limit", "pids": f"uses {c.get('pi')}% of its process limit",
                            "cpu": f"has used over 90% CPU for 5 min ({c.get('c')}% now)"}[b], p["key"])
            for p in mine.values():
                if p.get("flapping"):
                    holder = next((x for x in found.values() if p["key"] in x.parts), None)
                    (holder or f("UNCLEAR").add("", p["key"])).add(
                        f"{label(p)} is flapping (changing state again and again, {p.get('flap_count', '?')} changes logged as one)")
            if link.get("state") in ("warn", "down") and link.get("loss_1h"):
                f("DIODE_LOSS").add(f"{link['loss_1h']}% of monitoring packets lost in the last hour")
    bad = [k for k, p in parts.items() if scope_of(k) == scope and p["state"] in BAD]
    if scope != "high" and not found and link.get("state") in ("stale", "down"):
        # only the diode link is late (its parts not yet STALE): the same as the host going silent
        quiet = f"{ago(link['last_ms'], now)} ago" if link.get("last_ms") else "not yet"
        f("LEFT_SILENT").add(f"the last monitoring packet from {host} arrived {quiet}")
    if bad and not found:
        f("UNCLEAR").add("parts with a problem: " + ", ".join(label(parts[k]) for k in sorted(bad)))
    return sorted(found.values(), key=lambda x: PRIORITY.index(x.code) if x.code in PRIORITY else len(PRIORITY))


class Incidents:
    def __init__(self, store, runbook, event):
        self.store, self.runbook, self.event = store, runbook, event     # event(part, code, sev, text)
        KNOWN.clear()
        KNOWN.update(k for k in runbook if not k.startswith("_"))
        self.open = {i["scope"]: i for i in store.incidents(open_only=True)}
        self.dirty = False

    def title(self, code):
        return (self.runbook.get(code) or {}).get("title") or code.replace("_", " ").lower()

    def where(self, scope):
        return "the high side" if scope == "high" else f"the low side ({scope.split('/', 1)[1]})"

    def wants_ai(self, inc, parts, now):
        """The cause to ask the AI about now, or None. Asked once per cause, at most 3 times per incident: when the
        rules can't settle it, several things are wrong, a container crashed, the diode loses packets, it happened
        before, or something has been DOWN/UNKNOWN for a minute. Not for WARN-only known causes."""
        code = inc["cause"]["code"]
        asked = inc.get("asked", [])
        if code in asked or len(asked) >= 3 or inc.get("closed"):
            return None
        down = [k for k in inc["parts"] if k in parts and parts[k]["state"] in ("down", "unknown")
                and now - parts[k].get("since", now) >= 60_000]
        if code in SPECIAL or inc.get("also") or down or self.repeats(inc) or any(parts.get(k, {}).get("flapping") for k in inc["parts"]):
            return None if code in QUIET and not down else code
        return None

    def repeats(self, inc, days=7):
        """Earlier incidents with the same cause in the same scope (last `days`)."""
        since = inc["opened"] - days * 86400_000
        return [i for i in self.store.incidents(since_ms=since, limit=200)
                if i["id"] != inc["id"] and i["scope"] == inc["scope"] and i["cause"]["code"] == inc["cause"]["code"]
                and i["opened"] >= since]

    def mark_asked(self, inc_id, code):
        inc = next((i for i in self.open.values() if i["id"] == inc_id), None)
        if inc:
            inc["asked"] = inc.get("asked", []) + [code]
            self._save(inc)

    def history(self, code=None, part=None, days=30):
        """Past incidents matching a cause and/or a part: for the AI's get_history tool."""
        since = now_ms() - days * 86400_000
        rows = [i for i in self.store.incidents(since_ms=since, limit=500) if i["opened"] >= since
                and (not code or i["cause"]["code"] == code or any(a["code"] == code for a in i.get("also", [])))
                and (not part or part in i.get("parts", []))]
        out = [{"id": i["id"], "scope": i["scope"], "cause": i["cause"]["code"], "opened": i["opened"],
                "closed": i.get("closed"), "minutes": round(((i.get("closed") or now_ms()) - i["opened"]) / 60000, 1),
                "hour_utc": int(i["opened"] // 3600_000 % 24), "weekday": time.strftime("%a", time.gmtime(i["opened"] / 1000))}
               for i in rows]
        return {"days": days, "count": len(out), "incidents": out[:50]}

    def update(self, parts, links, traffic_at):
        now = now_ms()
        scopes = {scope_of(k) for k in parts} | set(self.open)
        scopes.discard(None)
        for scope in sorted(scopes):
            bad = sorted(k for k, p in parts.items() if scope_of(k) == scope and p.get("state") in BAD)
            host = scope.split("/", 1)[1] if scope.startswith("low/") else None
            if host and (links.get(host) or {}).get("state") in ("warn", "stale", "down"):
                bad.append(f"link/{host}")
            inc = self.open.get(scope)
            if not bad:
                if inc:
                    inc.setdefault("clear_since", now)
                    if now - inc["clear_since"] >= CLOSE_AFTER_S * 1000:
                        self._close(inc, now)
                    else:
                        self._save(inc)
                continue
            findings = diagnose(scope, parts, links, traffic_at, now) or [Finding("UNCLEAR").add("parts with a problem: "
                                                                            + ", ".join(bad))]
            cause = findings[0]
            body = {"code": cause.code, "title": self.title(cause.code), "evidence": cause.evidence}
            also = [{"code": x.code, "title": self.title(x.code), "evidence": x.evidence} for x in findings[1:4]]
            if not inc:
                opened = min([parts[k].get("since", now) for k in bad if k in parts] or [now])
                if all(x.code == "STARTING" for x in findings) and now - opened < STARTING_GRACE_S * 1000:
                    continue                           # a normal start: only an incident if it doesn't clear
                inc = {"scope": scope, "opened": opened, "closed": None, "parts": bad, "cause": body, "also": also,
                       "history": [{"at": now, "code": cause.code}]}
                inc["id"] = self.store.add_incident(inc)
                self.store.put_incident(inc)
                self.open[scope] = inc
                self.event(f"incident/{inc['id']}", "INCIDENT_OPENED", "warning",
                           f"Incident #{inc['id']} on {self.where(scope)}: likely cause: {body['title']}.")
                self.dirty = True
                continue
            inc.pop("clear_since", None)
            inc["parts"] = sorted(set(inc["parts"]) | set(bad))
            rank = lambda c: PRIORITY.index(c) if c in PRIORITY else len(PRIORITY)
            if cause.code != inc["cause"]["code"] and rank(cause.code) > rank(inc["cause"]["code"]):
                # milder than what it had (e.g. "starting up" while recovering): keep the incident's cause, say "now"
                inc["now"] = {"code": cause.code, "title": body["title"]}
                self._save(inc)
                continue
            inc.pop("now", None)
            changed = cause.code != inc["cause"]["code"]
            inc["cause"], inc["also"] = body, also
            if changed:
                inc["history"] = (inc["history"] + [{"at": now, "code": cause.code}])[-20:]
                self.event(f"incident/{inc['id']}", "INCIDENT_CAUSE", "info",
                           f"Incident #{inc['id']} on {self.where(scope)}: likely cause is now: {body['title']}.")
            self._save(inc)

    def _save(self, inc):
        clean = {k: v for k, v in inc.items() if k != "_saved"}
        blob = json.dumps(clean, sort_keys=True)
        if blob != inc.get("_saved"):                     # write only when something changed
            self.store.put_incident(clean)
            inc["_saved"] = blob
            self.dirty = True

    def _close(self, inc, now):
        inc["closed"] = inc.pop("clear_since", now)
        inc.pop("_saved", None)
        self.store.put_incident(inc)
        del self.open[inc["scope"]]
        self.event(f"incident/{inc['id']}", "INCIDENT_CLOSED", "info",
                   f"Incident #{inc['id']} on {self.where(inc['scope'])} closed after "
                   f"{ago(inc['opened'], inc['closed'])} (likely cause was: {inc['cause']['title']}).")
        self.dirty = True

    def summary(self, limit=10):
        """Open incidents and the latest closed ones, for the page."""
        out = [{k: v for k, v in i.items() if k != "_saved"} for i in self.open.values()]
        closed = [i for i in self.store.incidents(limit=limit + len(out)) if i.get("closed")][:limit]
        return sorted(out, key=lambda i: -i["opened"]) + closed

    def detail(self, id_, store_events):
        """One incident with its runbook entry and the events of its parts, for the API."""
        inc = next((i for i in self.open.values() if i["id"] == id_), None) or self.store.incident(id_)
        if not inc:
            return None
        inc = {k: v for k, v in inc.items() if k != "_saved"}
        start, end = inc["opened"] - 5 * 60_000, inc.get("closed") or now_ms()
        keys = set(inc["parts"]) | {f"incident/{id_}"}
        evs = [e for e in store_events(start, 2000) if e["at"] <= end and (e["part"] in keys or e["part"].startswith(inc["scope"]))]
        return {**inc, "runbook": self.runbook.get(inc["cause"]["code"]), "events": evs[-300:],
                "repeats": len(self.repeats(inc))}
