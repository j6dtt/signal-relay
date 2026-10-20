"""Incidents: periods of trouble, each with a likely cause worked out by fixed rules.

One incident per problem (since 2026-10-20, docs/monitoring-redesign.md section 5; was one per side). The rules
run per side ("high", "low/<host>"), then findings that share a part or a connection are one problem, wherever they
are (a fault across the diode is one incident). A problem joins the open incident that has one of its parts or its
cause; otherwise it opens its own: at once for DOWN, UNKNOWN or STALE, after WARN_WAIT_S for WARN alone. Lost
messages open a loss incident even when everything works again at once. An incident closes once none of its
problems has been seen for CLOSE_AFTER_S. While a tools/sim test runs, new incidents carry "test".

The likely cause comes from rules in diagnose() below, in code on purpose: a model asked "why is signal-cli UNKNOWN?"
looked at signal-cli, not at the silent signal-monitor that explains it. runbook.json says what each cause means
and what to check; this file only decides which one applies. Nothing here runs a check or changes anything.
"""
import json, logging, time
from store import now_ms

STALE_S, DOWN_S = 90, 180                 # same as monitor.py
CLOSE_AFTER_S = 60                        # all clear this long before an incident closes (no flapping)
STARTING_GRACE_S = 90                     # "starting up" alone opens an incident only if it lasts this long
WARN_WAIT_S = 60                          # WARN alone opens an incident only if it lasts this long
# Lost messages: an incident even if all is well again at once (how many, where, when: in its history)
LOSS_OPEN = ("MESSAGE_LOST", "CHAT_LOST", "EVENTS_MISSED", "CLI_RESTART_LOSS", "SEND_FAILED", "FORWARD_FAILED")
BAD = ("warn", "stale", "unknown", "down")
# Most specific and most serious first: the first finding is the likely cause, the others are listed as "also".
PRIORITY = ["MONRX_DOWN", "LEFT_SILENT", "MONITOR_ONLY_SILENT", "CONTAINER_OOM", "CONTAINER_RESTARTING",
            "CONTAINER_CRASHED", "CONTAINER_STOPPED", "CONTAINER_PAUSED",
            "DEVICE_UNLINKED", "CLIENT_EXPIRED", "RECEIVE_STALLED", "SIGNAL_UNREACHABLE",
            "CLI_UNREACHABLE", "CLI_TIMEOUT", "CLI_TLS_FAILED", "CLI_HTTP_ERROR", "CLI_DOWN", "CLI_GATEWAY_CLOSED",
            "MONITOR_CHECK_FAILED",
            "STORAGE_ERROR",
            "SUB_STOPPED", "SENDER_DEAD", "SENDER_STALLED", "SEND_FAILED", "SUB_GATEWAY_CLOSED", "SUB_TLS_FAILED",
            "SUB_UNREACHABLE", "SUB_NOT_CONNECTED", "SUB_STATUS_LOST", "EVENTS_MISSED", "CLI_RESTART_LOSS", "SUBSCRIBER_TWICE",
            "ROOM_UNSERVED", "ROOM_MISMATCH",
            "RX_STALLED", "RECEIVER_DOWN", "CHAT_NOT_ARRIVING", "FORWARD_FAILED", "DELIVER_FAILED", "DB_ERROR",
            "CHECKS_FAILING", "MESSAGE_LOST", "MESSAGE_BROKEN", "RX_OVERFLOW", "MESSAGE_SKIPPED", "SENDER_ERROR",
            "EVENT_DROPPED", "DECRYPT_FAILED", "UNTRUSTED_IDENTITY", "HANDLE_ERROR", "WRITE_FAILED", "STREAM_STALLED",
            "SIGNAL_RECONNECTING", "SIGNAL_FLAPPING", "SIGNAL_UNKNOWN", "DIODE_LOSS", "JVM_MEMORY", "FORWARD_SLOW", "LOOP_LAG",
            "QUEUE_BACKLOG", "QUEUE_SLOW", "GATEWAY_FULL", "GATEWAY_REFUSED", "RPC_BLOCKED", "BAD_PACKET",
            "NOTICE_FAILED", "INGEST_FAILED", "MONRX_WAITING", "MONRX_REFUSED", "CHECK_SLOW", "AI_FAILED", "PACKET_TOO_BIG", "STARTING", "CLOCK_SKEW",
            "MONITOR_DISK_LOW", "DISK_LOW", "CONTAINER_MEM_HIGH", "CONTAINER_CPU_HIGH", "CONTAINER_PIDS_HIGH",
            "FACTS_UNAVAILABLE", "CERT_EXPIRING", "OTHER", "UNCLEAR"]
CLI_CODES = {"UNREACHABLE": "CLI_UNREACHABLE", "GATEWAY_CLOSED": "CLI_GATEWAY_CLOSED", "TLS_FAILED": "CLI_TLS_FAILED",
             "TIMEOUT": "CLI_TIMEOUT", "HTTP_ERROR": "CLI_HTTP_ERROR"}
SUB_STREAM = {"GATEWAY_CLOSED": "SUB_GATEWAY_CLOSED", "TLS_FAILED": "SUB_TLS_FAILED", "UNREACHABLE": "SUB_UNREACHABLE"}
log = logging.getLogger("console")
# WARN-only, well known: no automatic AI question ("Ask anyway" instead)
QUIET = ("STARTING", "CERT_EXPIRING", "MONITOR_DISK_LOW", "QUEUE_BACKLOG", "QUEUE_SLOW", "DISK_LOW", "CLOCK_SKEW",
         "PACKET_TOO_BIG", "BAD_PACKET", "NOTICE_FAILED", "GATEWAY_REFUSED", "RPC_BLOCKED", "FORWARD_SLOW", "LOOP_LAG",
         "SIGNAL_RECONNECTING", "SIGNAL_UNKNOWN", "AI_FAILED", "FACTS_UNAVAILABLE", "CONTAINER_CPU_HIGH",
         "CHECK_SLOW", "MONRX_REFUSED")
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
    if p.get("comp") == "connection":
        return p.get("label") or p["key"]
    sub = p.get("name", "")
    name = ((sub if "subscriber" in sub else f"subscriber {sub}") if p.get("comp") == "subscriber" else
            "signal-cli's link to Signal" if p.get("comp") == "signal-servers" else
            f"container {p.get('name')}" if p.get("comp") == "container" else
            p.get("name") or "receiver" if p.get("comp") == "receiver" else p.get("comp", p["key"]))
    return name + (f" ({p['inst']})" if p.get("inst") not in (None, "signal-cli") else "")


def scope_of(key):
    if key.startswith("high/"):
        return "high"
    if key.startswith("link/"):                    # a host's monitoring route
        return f"low/{key.split('/', 1)[1]}"
    parts = key.split("/")
    return f"low/{parts[1]}" if key.startswith("low/") and len(parts) > 2 else None


def tokens(key):
    """What a part or connection key is about, so a box and its own wires count as one problem: a connection's ends
    by name (signal-receiver-relay's forward link and signal-receiver-relay itself), per host on the low side."""
    seg = key.split("/")
    if "~link" in seg:
        i, pre = seg.index("~link"), (seg[1] + "#" if seg[0] == "low" else "high#")
        return {key} | {pre + x for x in seg[i + 2:]}
    if seg[0] == "low" and len(seg) >= 4:
        pre = seg[1] + "#"
        return {key} | ({pre + seg[-1]} if seg[3] == "subscriber" else {pre + x for x in seg[2:]})
    if seg[0] == "high" and len(seg) >= 2:
        return {key, "high#" + seg[-1]}
    return {key}


def related(a, b):
    ta = set().union(*(tokens(k) for k in a)) if a else set()
    return bool(ta & (set().union(*(tokens(k) for k in b)) if b else set()))


def scope_for(keys):
    """An incident's scope from its parts: one side ("high", "low/<host>"), or "relay" when it spans both."""
    sc = {scope_of(k) for k in keys} - {None}
    return sc.pop() if len(sc) == 1 else "relay"


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
            if p["state"] in ("warn", "down") and not silent(p, now) and p.get("code") not in (None, "OK") \
                    and (p.get("comp") != "connection" or p["code"] in KNOWN):          # as for the low side's wires
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
                    lost = [label(s) for s in subs if s.get("code") == "NOT_CONNECTED"]
                    if cli.get("code") == "GATEWAY_CLOSED" and connected:
                        fd.add(f"{', '.join(connected)} still connected, so signal-cli itself works: only "
                               "signal-monitor's certificate is refused")
                    if lost:
                        fd.add(f"{', '.join(lost)} lost the stream too (expected while signal-cli is down)",
                               next(s["key"] for s in subs if label(s) == lost[0]))
                elif cli and cli["state"] == "warn" and not (cli.get("code") in (None, "OK") and (cli.get("container") or {}).get("b")):
                    f(known(cli.get("code"))).add(f"signal-cli ({inst}): {cli.get('text') or 'WARN'}", cli["key"])
                elif cli and cli["state"] == "unknown" and cli.get("code") == "MONITOR_CHECK_FAILED":
                    f("MONITOR_CHECK_FAILED").add(f"signal-cli ({inst}): {cli.get('text')}", cli["key"])
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
                    if s["state"] == "unknown" and s.get("code") == "SUB_STATUS_LOST":
                        f("SUB_STATUS_LOST").add(f"{label(s)}: {s.get('text')}", s["key"])
                        continue
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
            for p in sorted((p for p in mine.values() if p.get("comp") == "container"), key=lambda p: p["key"]):
                cf = container_finding(p, now) if p["state"] == "down" else None   # a container with no other box
                if cf:
                    f(cf[0]).add(f"{cf[1]}; it never reported (its log says why), so it has no other box", p["key"])
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
            for p in sorted((p for p in mine.values() if p.get("comp") == "connection"), key=lambda p: p["key"]):
                # a chat route from this host; a code the runbook doesn't explain (NO_STREAM, NO_STATUS: its box is
                # stopped) is its box's finding, not an "unclear" one of its own (the catch-all below still covers it)
                if p["state"] in ("warn", "down") and p.get("code") in KNOWN:
                    f(p["code"]).add(f"{label(p)}: {p.get('text') or p['state'].upper()}", p["key"])
            if link.get("state") in ("warn", "down") and link.get("loss_recent"):
                f("DIODE_LOSS").add(f"{link['loss_recent']}% of monitoring packets lost in the last 5 min"
                                    + (f" ({link['loss_1h']}% in the last hour)" if link.get("loss_1h") is not None else ""))
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
        self.open = {i["id"]: i for i in store.incidents(open_only=True)}
        self.bad_since = {}                       # part/connection key -> when first seen not UP (for WARN_WAIT_S)
        self.dirty = False

    def title(self, code):
        return (self.runbook.get(code) or {}).get("title") or code.replace("_", " ").lower()

    def where(self, scope):
        return ("the high side" if scope == "high" else "both sides of the diode" if scope == "relay"
                else f"the low side ({scope.split('/', 1)[1]})")

    def wants_ai(self, inc, parts, now, per_incident=3):
        """The cause to ask the AI about now, or None. Asked once per cause, at most 3 times per incident: when the
        rules can't settle it, several things are wrong, a container crashed, the diode loses packets, it happened
        before, or something has been DOWN/UNKNOWN for a minute. Not for WARN-only known causes."""
        code = inc["cause"]["code"]
        asked = inc.get("asked", [])
        if code in asked or (per_incident and len(asked) >= per_incident) or inc.get("closed"):
            return None
        down = [k for k in inc["parts"] if k in parts and (
                (parts[k]["state"] in ("down", "unknown") and now - parts[k].get("since", now) >= 60_000) or
                (parts[k].get("code") == "NOT_CONNECTED" and now - parts[k].get("code_since", now) >= 60_000))]
        if code in SPECIAL or inc.get("also") or down or self.repeats(inc) or any(parts.get(k, {}).get("flapping") for k in inc["parts"]):
            return None if code in QUIET and not down else code
        return None

    def repeats(self, inc, days=7):
        """Earlier incidents with the same cause on the same parts or side (last `days`), tests left out."""
        since = inc["opened"] - days * 86400_000
        return [i for i in self.store.incidents(since_ms=since, limit=200)
                if i["id"] != inc["id"] and not i.get("test") and i["cause"]["code"] == inc["cause"]["code"]
                and (i["scope"] == inc["scope"] or set(i.get("parts", [])) & set(inc["parts"])) and i["opened"] >= since]

    def mark_asked(self, inc_id, code):
        inc = self.open.get(inc_id)
        if inc:
            inc["asked"] = inc.get("asked", []) + [code]
            self._save(inc)

    def seen(self, inc_id, who):
        """Someone opened it: a closed loss incident leaves the bell."""
        inc = self.open.get(inc_id) or self.store.incident(inc_id)
        if inc and not inc.get("seen"):
            inc["seen"] = {"at": now_ms(), "by": who}
            if inc["id"] in self.open:
                self._save(inc)
            else:
                self.store.put_incident(inc)
            self.dirty = True
        return bool(inc)

    def history(self, code=None, part=None, days=30, sort="newest"):
        """Past incidents (all, or matching a cause and/or a part), for the AI's get_history. Tests are left out
        (counted). pattern: the same cause at least 3 times, the first and the last more than 24 h apart (worked out
        here, so the model doesn't call a busy afternoon a pattern)."""
        now = now_ms()
        since = now - days * 86400_000
        rows = [i for i in self.store.incidents(since_ms=since, limit=1000) if i["opened"] >= since
                and (not code or i["cause"]["code"] == code or any(a["code"] == code for a in i.get("also", [])))
                and (not part or part in i.get("parts", []) or any(part in k for k in i.get("parts", [])))]
        tests = [i for i in rows if i.get("test")]
        real = [i for i in rows if not i.get("test")]
        lasted = lambda i: round(((i.get("closed") or now) - i["opened"]) / 60000, 1)
        def one(i):
            lost = sum(x.get("n") or 0 for x in i.get("loss") or [])
            return {"id": i["id"], "where": self.where(i["scope"]), "cause": i["cause"]["code"], "title": i["cause"]["title"],
                    "opened_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(i["opened"] / 1000)),
                    "closed_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(i["closed"] / 1000)) if i.get("closed") else "still open",
                    "minutes": lasted(i), "hour_utc": int(i["opened"] // 3600_000 % 24),
                    "weekday": time.strftime("%a", time.gmtime(i["opened"] / 1000)),
                    **({"messages_lost": lost} if lost else {})}
        def pattern(group):
            group = [i for i in group if i["cause"]["code"] != "STARTING"]
            if len(group) < 3:
                return None
            first, last = min(i["opened"] for i in group), max(i["opened"] for i in group)
            if last - first <= 86400_000:
                return None
            return {"count": len(group), "first_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(first / 1000)),
                    "last_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(last / 1000)),
                    "hours_utc": sorted({int(i["opened"] // 3600_000 % 24) for i in group})}
        by_cause = {}
        for i in real:
            by_cause.setdefault(i["cause"]["code"], []).append(i)
        patterns = {c: pt for c, g in by_cause.items() if (pt := pattern(g))}
        order = {"longest": lambda i: -lasted(i), "oldest": lambda i: i["opened"]}.get(sort, lambda i: -i["opened"])
        return {"days": days, "count": len(real), "tests_left_out": len(tests),
                "minutes_total": round(sum(lasted(i) for i in real), 1),
                "per_cause": {c: len(g) for c, g in sorted(by_cause.items(), key=lambda kv: -len(kv[1]))},
                "pattern": (patterns.get(code) if code else None),
                "patterns": patterns or None,
                "incidents": [one(i) for i in sorted(real, key=order)[:40]],
                "note": "a pattern is set only when the same cause came at least 3 times over more than 24 h; "
                        "otherwise say nothing about patterns"}

    # ---- problems: the rules' findings, joined when they share a part ----
    def problems(self, parts, links, traffic_at, now):
        """[{code, title, evidence, parts, also}] over the whole relay: one per problem."""
        found = []
        for scope in sorted({scope_of(k) for k in parts} - {None}):
            bad = sorted(k for k, p in parts.items() if scope_of(k) == scope and p.get("state") in BAD)
            host = scope.split("/", 1)[1] if scope.startswith("low/") else None
            if host and (links.get(host) or {}).get("state") in ("warn", "stale", "down"):
                bad.append(f"link/{host}")
            if not bad:
                continue
            fs = diagnose(scope, parts, links, traffic_at, now) or [Finding("UNCLEAR").add(
                "parts with a problem: " + ", ".join(label(parts[k]) if k in parts else k for k in bad))]
            for fd in fs:
                if not fd.parts and host and fd.code in ("DIODE_LOSS", "LEFT_SILENT"):
                    fd.parts.append(f"link/{host}")
            covered = {k for fd in fs for k in fd.parts}
            rest = [k for k in bad if k not in covered]
            if rest:                                  # parts with a problem the rules didn't name: with the main finding
                fs[0].parts.extend(rest)
            found.extend(fs)
        groups = []                                   # findings that share a part (or MONRX_DOWN seen from both sides)
        for fd in found:
            keys = set(fd.parts) | ({"~monrx"} if fd.code == "MONRX_DOWN" else set())
            hit = [g for g in groups if g["keys"] & keys or related(g["keys"] - {"~monrx"}, keys - {"~monrx"})]
            g = {"keys": keys, "f": [fd]}
            for h in hit:
                g["keys"] |= h["keys"]
                g["f"] = h["f"] + g["f"]
                groups.remove(h)
            groups.append(g)
        rank = lambda c: PRIORITY.index(c) if c in PRIORITY else len(PRIORITY)
        out = []
        for g in groups:
            fs, merged = sorted(g["f"], key=lambda x: rank(x.code)), {}
            for fd in fs:                             # the same cause found on both sides: one entry
                m = merged.setdefault(fd.code, Finding(fd.code))
                for e in fd.evidence:
                    m.add(e)
            fs = list(merged.values())
            out.append({"code": fs[0].code, "title": self.title(fs[0].code), "evidence": fs[0].evidence,
                        "parts": sorted(g["keys"] - {"~monrx"}),
                        "also": [{"code": x.code, "title": self.title(x.code), "evidence": x.evidence} for x in fs[1:4]]})
        return sorted(out, key=lambda x: rank(x["code"]))

    def update(self, parts, links, traffic_at, test=None):
        now = now_ms()
        bad_now = {k for k, p in parts.items() if p.get("state") in BAD}
        bad_now |= {f"link/{h}" for h, l in links.items() if l.get("state") in ("warn", "stale", "down")}
        self.bad_since = {k: self.bad_since.get(k, now) for k in bad_now}
        hit = {}                                      # open incident id -> the problems it has now
        new = []
        for pr in self.problems(parts, links, traffic_at, now):
            keys = set(pr["parts"])
            # against what the incident has a problem with lately (not every part it ever had: those may be well now)
            home = sorted((i for i in self.open.values() if related(keys, i.get("active", i["parts"]))
                           or (not keys and i["cause"]["code"] == pr["code"])),
                          key=lambda i: i["opened"])
            if home:
                hit.setdefault(home[0]["id"], []).append(pr)
            else:
                new.append(pr)
        for inc in list(self.open.values()):
            prs = hit.get(inc["id"])
            if not prs:
                inc.setdefault("clear_since", now)
                if inc.get("loss_at"):                # a loss incident: closes a minute after its latest loss
                    inc["clear_since"] = max(inc["clear_since"], inc["loss_at"])
                if now - inc["clear_since"] >= CLOSE_AFTER_S * 1000:
                    self._close(inc, now)
                else:
                    self._save(inc)
                continue
            self._carry(inc, prs, now)
        for pr in new:
            self._open(pr, parts, links, now, test)

    def _state(self, key, parts, links):
        if key.startswith("link/"):
            return (links.get(key.split("/", 1)[1]) or {}).get("state")
        return (parts.get(key) or {}).get("state")

    def _open(self, pr, parts, links, now, test, loss=None):
        states = {self._state(k, parts, links) for k in pr["parts"]}
        codes = {pr["code"]} | {a["code"] for a in pr["also"]}
        opened = min([parts[k].get("since", now) for k in pr["parts"] if k in parts and parts[k].get("since")]
                     + [self.bad_since.get(k, now) for k in pr["parts"]] or [now])
        if not loss:
            if codes == {"STARTING"} and now - opened < STARTING_GRACE_S * 1000:
                return None                           # a normal start: only an incident if it doesn't clear
            serious = states & {"down", "unknown", "stale"} or codes & set(LOSS_OPEN)
            first = min([self.bad_since.get(k, now) for k in pr["parts"]] or [now])
            if not serious and now - first < WARN_WAIT_S * 1000:
                return None                           # WARN alone: only if it lasts a minute
        body = {"code": pr["code"], "title": pr["title"], "evidence": pr["evidence"]}
        inc = {"scope": scope_for(pr["parts"]), "opened": opened, "closed": None, "parts": pr["parts"], "active": pr["parts"],
               "cause": body, "also": pr["also"], "history": [{"at": now, "code": pr["code"]}]}
        if loss:
            inc["loss"], inc["loss_at"] = [loss], now
        if test:
            inc["test"] = test
        inc["id"] = self.store.add_incident(inc)
        self.store.put_incident(inc)
        self.open[inc["id"]] = inc
        self.event(f"incident/{inc['id']}", "INCIDENT_OPENED", "error" if loss else "warning",
                   f"Incident #{inc['id']} on {self.where(inc['scope'])}{' (test ' + test + ')' if test else ''}: "
                   f"likely cause: {body['title']}.")
        self.dirty = True
        return inc

    def _carry(self, inc, prs, now):
        """An open incident's problems now: its parts grow, its cause is the most serious one seen."""
        inc.pop("clear_since", None)
        rank = lambda c: PRIORITY.index(c) if c in PRIORITY else len(PRIORITY)
        prs = sorted(prs, key=lambda x: rank(x["code"]))
        inc["active"] = sorted(set().union(*(set(p["parts"]) for p in prs)))
        inc["parts"] = sorted(set(inc["parts"]) | set(inc["active"]))
        inc["scope"] = scope_for(inc["parts"])
        top = prs[0]
        also = (top["also"] + [{k: p[k] for k in ("code", "title", "evidence")} for p in prs[1:]])[:3]
        if top["code"] != inc["cause"]["code"] and rank(top["code"]) > rank(inc["cause"]["code"]):
            # milder than what it had (e.g. "starting up" while recovering): keep the incident's cause, say "now"
            inc["now"] = {"code": top["code"], "title": top["title"]}
            self._save(inc)
            return
        inc.pop("now", None)
        changed = top["code"] != inc["cause"]["code"]
        inc["cause"] = {"code": top["code"], "title": top["title"], "evidence": top["evidence"]}
        inc["also"] = also
        if changed:
            inc["history"] = (inc["history"] + [{"at": now, "code": top["code"]}])[-20:]
            self.event(f"incident/{inc['id']}", "INCIDENT_CAUSE", "info",
                       f"Incident #{inc['id']} on {self.where(inc['scope'])}: likely cause is now: {top['title']}.")
        self._save(inc)

    def loss(self, key, code, n, text, test=None):
        """Messages were lost (a counted loss code went up): into the open incident that has this part, else a
        loss incident of its own, even if all is well again at once."""
        now = now_ms()
        entry = {"at": now, "code": code, "n": n, "part": key, "text": text}
        inc = next((i for i in sorted(self.open.values(), key=lambda i: i["opened"]) if related([key], i.get("active", i["parts"]))), None)
        if inc:
            inc["loss"] = (inc.get("loss", []) + [entry])[-50:]
            inc["loss_at"] = now
            self._save(inc)
            return inc
        pr = {"code": code, "title": self.title(code), "evidence": [text], "parts": [key], "also": []}
        return self._open(pr, {}, {}, now, test, loss=entry)

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
        del self.open[inc["id"]]
        lost = sum(x.get("n") or 0 for x in inc.get("loss") or [])
        self.event(f"incident/{inc['id']}", "INCIDENT_CLOSED", "info",
                   f"Incident #{inc['id']} on {self.where(inc['scope'])} closed after "
                   f"{ago(inc['opened'], inc['closed'])} (likely cause was: {inc['cause']['title']})"
                   + (f"; {lost} message(s) lost: it stays in the bell until someone opens it." if lost else "."))
        self.dirty = True

    def summary(self, limit=10):
        """Open incidents and the latest closed ones, for the page."""
        out = [{k: v for k, v in i.items() if k != "_saved"} for i in self.open.values()]
        closed = [i for i in self.store.incidents(limit=limit + len(out)) if i.get("closed")][:limit]
        ids = {i["id"] for i in closed}
        unseen = [i for i in self.store.incidents(since_ms=now_ms() - 86400_000, limit=100)   # lost messages nobody looked at
                  if i.get("closed") and i.get("loss") and not i.get("seen") and i["id"] not in ids]
        return sorted(out, key=lambda i: -i["opened"]) + sorted(closed + unseen, key=lambda i: -i["opened"])

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
