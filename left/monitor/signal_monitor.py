#!/usr/bin/env python3
"""signal-monitor: watches this host's part of the relay and reports it across the diode (one-way).

One per left host. It never sends anything to Signal and never touches messages.
  - signal-cli: GET /api/v1/check through each instance's TLS gateway, with this monitor's own client certificate
    (CN signal-monitor, which must be in TLS_CLIENT_NAMES); also reads the gateway certificate's expiry date.
  - subscribers: each sends a small status packet here every 10 s (UDP, this host's network only). One that goes
    quiet for SUB_TIMEOUT is reported DOWN.
  - this host: free disk space under DISK_PATHS.
Every HEARTBEAT seconds it sends, as CEF over UDP to the diode (UDP_DEST:UDP_PORT, normally 6511):
  {"t":"hb", ...}   its own heartbeat
  {"t":"st", ...}   the state of each signal-cli and subscriber (also sent at once when it changes), with "code":
                    a fixed word for why (OK, STARTING, CERT_EXPIRING, UNREACHABLE, NOT_CONNECTED, NO_STATUS, ...)
  {"t":"ev", ...}   things that are not states: error lines from a subscriber, start-up
Each message is one datagram (at most MAX_DGRAM bytes):
  CEF:0|J6-DTT|CHAT|1|1|SIGNAL-RELAY-MON|MON|{"t":..,"host":..,"boot":..,"seq":..,"ts":..,...}
"boot" is random per start and "seq" counts up, so the right side can count lost packets.

Settings (environment):
  MONITOR_HOST     a name for this host, as shown on the Monitor tab (default: the container's host name)
  SIGNAL_TARGETS   instance=URL pairs, comma-separated, e.g. "relay-1=https://signal:8443"
  STATUS_PORT      where subscribers send their status (UDP, default 6520)
  UDP_DEST, UDP_PORT   the diode's input for monitoring (default port 6511)
  TLS_CA, TLS_CERT, TLS_KEY   default /certs/ca.crt, /certs/client.crt, /certs/client.key
  DISK_PATHS       comma-separated folders whose disk to check (default /certs: same disk as left/); DISK_LOW_PCT (default 10)
  CERT_WARN_DAYS   a certificate closer than this to expiry makes its part WARN (default 30)
"""
import asyncio, json, logging, os, secrets, signal, shutil, socket, ssl, time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from comlibv3 import string_to_cef, send_events_over_udp

HOST      = os.environ.get("MONITOR_HOST") or socket.gethostname()
TARGETS   = {k.strip(): v.strip() for k, _, v in (t.partition("=") for t in os.environ.get("SIGNAL_TARGETS", "").split(","))
             if k.strip() and v.strip()}
STATUS    = ("0.0.0.0", int(os.environ.get("STATUS_PORT", 6520)))
DEST      = (os.environ.get("UDP_DEST", "127.0.0.1"), int(os.environ.get("UDP_PORT", 6511)))
TLS       = {k: os.environ.get(f"TLS_{k.upper()}", f"/certs/{f}") for k, f in
             (("ca", "ca.crt"), ("cert", "client.crt"), ("key", "client.key"))}
DISKS     = [d.strip() for d in os.environ.get("DISK_PATHS", "/certs").split(",") if d.strip()]
DISK_LOW  = float(os.environ.get("DISK_LOW_PCT", 10))
CERT_WARN = int(os.environ.get("CERT_WARN_DAYS", 30))
HEADER    = "CEF:0|J6-DTT|CHAT|{}|{}|SIGNAL-RELAY-MON|MON|"
MAX_DGRAM = 1472
HEARTBEAT = 30                      # seconds between heartbeats (and status repeats)
SUB_TIMEOUT = 35                    # a subscriber silent this long is DOWN (it reports every 10 s)
CHECK_TIMEOUT = 10
START_GRACE = 60                    # seconds after a start in which "not answering yet" is WARN, not DOWN
ERRORS_PER_MIN = 5                  # error lines forwarded per subscriber per minute; the rest are counted
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
    """GET /api/v1/check through the TLS gateway. (ok, ms, gateway certificate days left, error text, code).
    code is fixed (for the Monitor tab's likely-cause rules): OK, GATEWAY_CLOSED, HTTP_ERROR, TLS_FAILED, TIMEOUT,
    UNREACHABLE."""
    u = urlsplit(url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=CHECK_TIMEOUT) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as s:
                days = days_left(s.getpeercert()["notAfter"])
                s.sendall(f"GET /api/v1/check HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                head = b""
                while b"\r\n" not in head and len(head) < 4096:
                    chunk = s.recv(1024)
                    if not chunk:
                        break
                    head += chunk
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        ms = int((time.monotonic() - t0) * 1000)
        parts = status.split()
        if len(parts) >= 2 and parts[1].startswith("2"):
            return True, ms, days, "", "OK"
        if not status:
            return (False, ms, days, "the gateway closed the connection without answering (is signal-monitor in "
                    "TLS_CLIENT_NAMES?)", "GATEWAY_CLOSED")
        return False, ms, days, f"answered {status}", "HTTP_ERROR"
    except ssl.SSLError as e:
        return False, None, None, f"secure connection failed ({e.reason or e}); check the certificates", "TLS_FAILED"
    except TimeoutError:
        return False, None, None, f"no answer within {CHECK_TIMEOUT} s", "TIMEOUT"
    except OSError as e:
        return False, None, None, f"not reachable ({e.strerror or e})", "UNREACHABLE"


class Monitor:
    def __init__(self):
        self.ctx = tls_context()
        self.cli = {}           # instance -> {"state", "info", "text"}
        self.subs = {}          # (instance, name) -> {"state", "info", "text", "seen", "err_window", "err_count", ...}

    # ---- states ----
    def report(self, inst, comp, name, state, info, text, code):
        """Send a part's state (on every heartbeat, and at once when it changes). code: a fixed word for why."""
        send({"t": "st", "inst": inst, "comp": comp, "name": name, "state": state, "code": code, "info": info, "text": text})

    def set_cli(self, inst, state, info, text, code):
        old = self.cli.get(inst, {}).get("code")
        self.cli[inst] = {"state": state, "info": info, "text": text, "code": code}
        if code != old:
            (log.info if state == "up" else log.warning)("signal-cli %s is %s%s.", inst, state.upper(), f": {text}" if text else "")
            self.report(inst, "signal-cli", "signal-cli", state, info, text, code)

    async def check_loop(self):
        while True:
            for inst, url in TARGETS.items():
                ok, ms, days, err, code = await asyncio.to_thread(check, url, self.ctx)
                info = {"check_ms": ms, "cert_days": days}
                state, text = ("up", "") if ok else ("down", err)
                if not ok and time.time() - STARTED < START_GRACE:      # everything starting together
                    state, text, code = "warn", f"not answering yet, just started ({err})", "STARTING"
                if ok and days is not None and days < CERT_WARN:
                    state, text, code = "warn", f"its certificate expires in {days} days", "CERT_EXPIRING"
                self.set_cli(inst, state, info, text, code)
            await asyncio.sleep(HEARTBEAT / 2)

    # ---- subscriber status packets ----
    def status(self, data, addr):
        try:
            m = json.loads(data.decode("utf-8"))
            inst, name = m["inst"], m["name"]
            if not (name_ok(inst) and name_ok(name)):
                raise ValueError("bad instance or name")
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log.warning("Ignored a status packet from %s that is not valid: %r", addr[0], e)
            return
        key = (inst, name)
        s = self.subs.get(key)
        if s is None:
            s = self.subs[key] = {"state": None, "err_window": time.time(), "err_count": 0, "suppressed": 0}
            log.info("Subscriber %s (%s) reported for the first time, from %s.", name, inst, addr[0])
        s["seen"] = time.time()
        info = {k: m.get(k) for k in ("stream", "last_event_age_s", "sent", "ignored", "queue", "errors", "warnings",
                                       "cert_days", "up_s", "stream_code") if isinstance(m.get(k), (int, float, str)) or m.get(k) is None}
        s["info"] = info
        if m.get("stream") == "starting" and isinstance(m.get("up_s"), int) and m["up_s"] < START_GRACE:
            state, text, code = "warn", "starting up, not connected to signal-cli yet", "STARTING"
        elif m.get("stream") != "connected":
            state, text, code = ("down", f"not connected to signal-cli ({str(m.get('stream_error') or m.get('stream'))[:120]})",
                                 "NOT_CONNECTED")                   # why: info.stream_code
        elif isinstance(m.get("cert_days"), int) and m["cert_days"] < CERT_WARN:
            state, text, code = "warn", f"its certificate expires in {m['cert_days']} days", "CERT_EXPIRING"
        elif isinstance(m.get("queue"), int) and m["queue"] > 100:
            state, text, code = "warn", f"{m['queue']} messages waiting to be sent", "QUEUE_BACKLOG"
        else:
            state, text, code = "up", "", "OK"
        self.set_sub(key, state, text, code)
        for line in (m.get("new_errors") or [])[:10]:
            self.error_line(key, str(line)[:300])

    def set_sub(self, key, state, text, code):
        s = self.subs[key]
        old, s["state"], s["text"], s["code"] = s.get("code"), state, text, code
        if code != old:
            (log.info if state == "up" else log.warning)("Subscriber %s (%s) is %s%s.", key[1], key[0], state.upper(),
                                                       f": {text}" if text else "")
            self.report(key[0], "subscriber", key[1], state, s.get("info", {}), text, code)

    def error_line(self, key, line):
        """Forward a subscriber's error line as an event, at most ERRORS_PER_MIN a minute; count the rest."""
        s, now = self.subs[key], time.time()
        if now - s["err_window"] >= 60:
            if s["suppressed"]:
                send({"t": "ev", "inst": key[0], "comp": "subscriber", "name": key[1], "code": "SUB_ERRORS_SUPPRESSED",
                      "sev": "warning", "text": f"{s['suppressed']} more error line(s) from subscriber {key[1]} in the last minute (not sent)."})
            s["err_window"], s["err_count"], s["suppressed"] = now, 0, 0
        if s["err_count"] >= ERRORS_PER_MIN:
            s["suppressed"] += 1
            return
        s["err_count"] += 1
        send({"t": "ev", "inst": key[0], "comp": "subscriber", "name": key[1], "code": "SUB_ERROR", "sev": "error",
              "text": f"subscriber {key[1]}: {line}"})

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
            text = "; ".join(low + ([f"its own certificate expires in {cert} days"] if expiring else []))
            code = "DISK_LOW" if low else "CERT_EXPIRING" if expiring else "OK"
            send({"t": "hb", "up_s": int(time.time() - STARTED), "state": "warn" if text else "up", "text": text,
                  "code": code, "info": {**disk, "cert_days": cert, "parts": len(self.cli) + len(self.subs), "ver": 2}})
            for inst, c in self.cli.items():
                self.report(inst, "signal-cli", "signal-cli", c["state"], c["info"], c["text"], c["code"])
            for (inst, name), s in self.subs.items():
                self.report(inst, "subscriber", name, s["state"], s.get("info", {}), s.get("text", ""), s.get("code"))
            await asyncio.sleep(HEARTBEAT)


class StatusProtocol(asyncio.DatagramProtocol):
    def __init__(self, mon):
        self.mon = mon

    def datagram_received(self, data, addr):
        self.mon.status(data, addr)


async def main():
    if not TARGETS:
        log.error("No signal-cli to watch. Set SIGNAL_TARGETS, e.g. relay-1=https://signal:8443")
        raise SystemExit(1)
    mon = Monitor()
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: StatusProtocol(mon), local_addr=STATUS)
    log.info("Starting up as host %s (this run: %s). Watching signal-cli %s.", HOST, BOOT,
             ", ".join(f"{k} at {v}" for k, v in TARGETS.items()))
    log.info("Listening for subscriber status on UDP port %d. Reporting to %s:%d over UDP every %d s.",
             STATUS[1], DEST[0], DEST[1], HEARTBEAT)
    send({"t": "ev", "comp": "signal-monitor", "code": "MONITOR_STARTED", "sev": "info",
          "text": f"signal-monitor on {HOST} started."})
    await asyncio.gather(mon.check_loop(), mon.heartbeat_loop())


def stop_now(*_):
    """docker stop sends SIGTERM; stop at once (as Ctrl-C would) instead of being killed 10 s later."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_now)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped.")
