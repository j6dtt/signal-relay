"""Incidents: periods of trouble, each with a likely cause worked out by fixed rules.

One incident per scope at a time: "right" (this side's parts) or "left/<host>" (one left host, its signal-cli
instances, subscribers and diode link). It opens when a part in the scope stops being UP, collects every part that
has a problem while it is open, and closes once all of them have been UP for CLOSE_AFTER_S.

The likely cause comes from rules in diagnose() below, in code on purpose: a model asked "why is signal-cli UNKNOWN?"
looked at signal-cli, not at the silent signal-monitor that explains it. runbook.json says what each cause means
and what to check; this file only decides which one applies. Nothing here runs a check or changes anything.
"""
import json, logging
from store import now_ms

STALE_S, DOWN_S = 90, 180                 # same as monitor.py
CLOSE_AFTER_S = 60                        # all clear this long before an incident closes (no flapping)
STARTING_GRACE_S = 90                     # "starting up" alone opens an incident only if it lasts this long
BAD = ("warn", "stale", "unknown", "down")
# Most specific and most serious first: the first finding is the likely cause, the others are listed as "also".
PRIORITY = ["MONRX_DOWN", "LEFT_SILENT", "MONITOR_ONLY_SILENT", "CLI_UNREACHABLE", "CLI_TIMEOUT", "CLI_TLS_FAILED",
            "CLI_HTTP_ERROR", "CLI_DOWN", "CLI_GATEWAY_CLOSED", "SUB_STOPPED", "SUB_GATEWAY_CLOSED", "SUB_TLS_FAILED",
            "SUB_UNREACHABLE", "SUB_NOT_CONNECTED", "RECEIVER_DOWN", "DIODE_LOSS", "STARTING", "QUEUE_BACKLOG",
            "MONITOR_DISK_LOW", "CERT_EXPIRING", "UNCLEAR"]
CLI_CODES = {"UNREACHABLE": "CLI_UNREACHABLE", "GATEWAY_CLOSED": "CLI_GATEWAY_CLOSED", "TLS_FAILED": "CLI_TLS_FAILED",
             "TIMEOUT": "CLI_TIMEOUT", "HTTP_ERROR": "CLI_HTTP_ERROR"}
SUB_STREAM = {"GATEWAY_CLOSED": "SUB_GATEWAY_CLOSED", "TLS_FAILED": "SUB_TLS_FAILED", "UNREACHABLE": "SUB_UNREACHABLE"}
log = logging.getLogger("playback")


def ago(ms, now):
    s = max(0, (now - ms) / 1000)
    return f"{round(s)} s" if s < 120 else f"{round(s / 60)} min" if s < 7200 else f"{round(s / 3600)} h"


def label(p):
    name = f"subscriber {p['name']}" if p.get("comp") == "subscriber" else p.get("comp", p["key"])
    return name + (f" ({p['inst']})" if p.get("inst") else "")


def scope_of(key):
    if key.startswith("right/"):
        return "right"
    parts = key.split("/")
    return f"left/{parts[1]}" if key.startswith("left/") and len(parts) > 2 else None


def silent(p, now):
    return (now - p.get("last", now)) / 1000 > STALE_S


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
    (with "state"); traffic_at: when chat packets last arrived at the receiver (ms) or None."""
    found = {}

    def f(code):
        return found.setdefault(code, Finding(code))

    def p_(key):
        return parts.get(key)

    monrx = p_("right/monitor-receiver")
    monrx_bad = bool(monrx and monrx["state"] in ("stale", "down"))
    if scope == "right":
        if monrx_bad:
            f("MONRX_DOWN").add(f"monitor-receiver last heard {ago(monrx['last'], now)} ago", monrx["key"])
        rx = p_("right/receiver")
        if rx and rx["state"] in ("stale", "down"):
            f("RECEIVER_DOWN").add(f"the receiver's counters last arrived {ago(rx['last'], now)} ago", rx["key"])
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
                    "nothing from the left could get through", monrx["key"])
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
            if mon and mon["state"] == "warn":
                code = {"DISK_LOW": "MONITOR_DISK_LOW", "CERT_EXPIRING": "CERT_EXPIRING"}.get(mon.get("code"), "UNCLEAR")
                f(code).add(f"signal-monitor: {mon.get('text') or 'WARN'}", mon["key"])
            insts = sorted({p["inst"] for p in mine.values() if p.get("inst")})
            for inst in insts:
                cli = mine.get(f"{scope}/{inst}/signal-cli")
                subs = [p for p in mine.values() if p.get("inst") == inst and p.get("comp") == "subscriber"]
                cli_down = bool(cli and cli["state"] in ("stale", "down"))
                if cli and cli["state"] == "down":
                    fd = f(CLI_CODES.get(cli.get("code"), "CLI_DOWN")).add(f"signal-cli ({inst}): {cli.get('text') or 'DOWN'}", cli["key"])
                    connected = [label(s) for s in subs if s["state"] == "up"]
                    lost = [label(s) for s in subs if s["state"] == "down" and s.get("code") == "NOT_CONNECTED"]
                    if cli.get("code") == "GATEWAY_CLOSED" and connected:
                        fd.add(f"{', '.join(connected)} still connected, so signal-cli itself works: only "
                               "signal-monitor's certificate is refused")
                    if lost:
                        fd.add(f"{', '.join(lost)} lost the stream too (expected while signal-cli is down)",
                               next(s["key"] for s in subs if label(s) == lost[0]))
                elif cli and cli["state"] == "warn":
                    code = {"STARTING": "STARTING", "CERT_EXPIRING": "CERT_EXPIRING"}.get(cli.get("code"), "UNCLEAR")
                    f(code).add(f"signal-cli ({inst}): {cli.get('text') or 'WARN'}", cli["key"])
                for s in subs:
                    if s["state"] not in BAD or s["state"] == "unknown":
                        continue
                    if cli_down and s.get("code") == "NOT_CONNECTED":
                        continue                       # explained by signal-cli above
                    code = s.get("code")
                    if code == "NO_STATUS":
                        c = "SUB_STOPPED"
                    elif code == "NOT_CONNECTED":
                        c = SUB_STREAM.get((s.get("info") or {}).get("stream_code"), "SUB_NOT_CONNECTED")
                    else:
                        c = {"STARTING": "STARTING", "CERT_EXPIRING": "CERT_EXPIRING",
                             "QUEUE_BACKLOG": "QUEUE_BACKLOG"}.get(code, "UNCLEAR")
                    fd = f(c).add(f"{label(s)}: {s.get('text') or s['state'].upper()}", s["key"])
                    if c.startswith("SUB_") and c != "SUB_STOPPED" and cli and cli["state"] == "up":
                        fd.add(f"signal-monitor's own check of signal-cli ({inst}) works")
            if link.get("state") in ("warn", "down") and link.get("loss_1h"):
                f("DIODE_LOSS").add(f"{link['loss_1h']}% of monitoring packets lost in the last hour")
    bad = [k for k, p in parts.items() if scope_of(k) == scope and p["state"] in BAD]
    if bad and not found:
        f("UNCLEAR").add("parts with a problem: " + ", ".join(label(parts[k]) for k in sorted(bad)))
    return sorted(found.values(), key=lambda x: PRIORITY.index(x.code) if x.code in PRIORITY else len(PRIORITY))


class Incidents:
    def __init__(self, store, runbook, event):
        self.store, self.runbook, self.event = store, runbook, event     # event(part, code, sev, text)
        self.open = {i["scope"]: i for i in store.incidents(open_only=True)}
        self.dirty = False

    def title(self, code):
        return (self.runbook.get(code) or {}).get("title") or code.replace("_", " ").lower()

    def where(self, scope):
        return "the right side" if scope == "right" else scope.split("/", 1)[1]

    def update(self, parts, links, traffic_at):
        now = now_ms()
        scopes = {scope_of(k) for k in parts} | set(self.open)
        scopes.discard(None)
        for scope in sorted(scopes):
            bad = sorted(k for k, p in parts.items() if scope_of(k) == scope and p.get("state") in BAD)
            host = scope.split("/", 1)[1] if scope.startswith("left/") else None
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
            findings = diagnose(scope, parts, links, traffic_at, now)
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
        return {**inc, "runbook": self.runbook.get(inc["cause"]["code"]), "events": evs[-300:]}
