#!/usr/bin/env python3
"""monitor-receiver: takes the monitoring packets from the diode's output and passes them on to the console.

Separate from the chat receiver (cef_to_json.py), so a fault or a restart here never touches chat, and the other
way round. Each packet is one CEF line, one message (no reassembly):
    CEF:0|CHAT|MONITOR|1|1|SIGNAL-RELAY-MON|MON|{"t":"hb"|"st"|"ev"|"cf","host",...}
The diode routes on fields 1 and 2 (CHAT|MONITOR is the monitoring stream's own route); any other header is refused.
Valid ones are POSTed to CONSOLE_URL/monitor with "rx" (when it arrived, ms) added. Every 30 s it also posts its
own heartbeat ({"t":"rx_hb"}), so the console can tell a dead monitor-receiver from a silent low side.
While the console can't be reached (restarting, stopped), messages wait here in order, up to HOLD, and are passed on
when it is back: a console restart then loses nothing and doesn't look like loss in the diode. Receiving and passing
on run in separate threads, so a slow or failing console never delays reading a packet (its "rx" time stays true).

Settings: UDP_LISTEN (0.0.0.0), UDP_PORT (6511), CONSOLE_URL (http://console:8091).
"""
import collections, json, logging, threading, os, signal, socket, time, urllib.error, urllib.request
from comlibv3 import parse_string_in_cef

LISTEN    = (os.environ.get("UDP_LISTEN", "0.0.0.0"), int(os.environ.get("UDP_PORT", 6511)))
CONSOLE  = os.environ.get("CONSOLE_URL", "http://console:8091").rstrip("/")
TAG       = "SIGNAL-RELAY-MON"          # CEF Name field expected from signal-monitor
HEARTBEAT = 30
MAX_BYTES = 1472
HOLD      = 1000                        # messages kept while the console can't be reached (~1.5 h of a quiet host)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("monitor-receiver")


def post(body):
    """POST JSON to the console's /monitor. None on success, else a short error text; it starts with "HTTP 4" when
    the console refused this message (no point trying it again)."""
    req = urllib.request.Request(f"{CONSOLE}/monitor", json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()
        return None
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read()[:200].decode('utf-8', 'replace')}"
    except (urllib.error.URLError, OSError) as e:
        return str(getattr(e, "reason", e))


def parse(data):
    """The message dict of one packet, or raise ValueError."""
    if len(data) > MAX_BYTES:
        raise ValueError(f"{len(data)} bytes, over {MAX_BYTES}")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("not ASCII")
    head = text.split("|", 3)[1:3]
    if head != ["CHAT", "MONITOR"]:
        raise ValueError(f"header {'|'.join(head)!r}, not CHAT|MONITOR (the diode's route may be wrong)")
    msgs, tag = parse_string_in_cef(text)
    if tag != TAG:
        raise ValueError(f"CEF name {tag!r}, not {TAG}")
    if len(msgs) != 1 or not isinstance(msgs[0], dict) or msgs[0].get("t") not in ("hb", "st", "ev", "cf"):
        raise ValueError("not one monitoring message")
    return msgs[0]


def pass_on(held, wake, stats):
    """Thread: hands the held messages to the console, oldest first; waits while it can't be reached."""
    failing = False
    while True:
        wake.wait(2.0)
        wake.clear()
        while held:
            m = held.popleft()
            err = post(m)
            if err and not err.startswith("HTTP 4"):
                held.appendleft(m)                           # back to the front: order kept
                if not failing:
                    log.error("Could not pass a monitoring message on to the console: %s. Keeping them (up to %d) "
                              "until it is back.", err, HOLD)
                failing = True
                break
            if err:
                stats["not_delivered"] += 1
                log.warning("The console refused a monitoring message: %s.", err)
            else:
                stats["passed"] += 1
            if failing:
                log.info("Passing monitoring messages on to the console again (%d waited).", len(held) + 1)
                failing = False
        stats["waiting"] = len(held)


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(LISTEN)
    sock.settimeout(1.0)
    stats = {"packets": 0, "passed": 0, "bad": 0, "not_delivered": 0, "waiting": 0}
    held, wake = collections.deque(), threading.Event()    # deque: appends/pops are thread-safe
    threading.Thread(target=pass_on, args=(held, wake, stats), daemon=True).start()
    hosts = set()
    log.info("Listening for monitoring packets on UDP %s:%d. Passing them on to %s/monitor.", *LISTEN, CONSOLE)
    last_hb = 0.0
    try:
        while True:
            try:
                data, addr = sock.recvfrom(65535)
                stats["packets"] += 1
                try:
                    m = parse(data)
                except ValueError as e:
                    stats["bad"] += 1
                    log.warning("Received a packet (%d bytes) from %s that is not a valid monitoring packet, "
                                "so it was ignored: %s", len(data), addr[0], e)
                    continue
                if m.get("host") not in hosts and isinstance(m.get("host"), str):
                    hosts.add(m["host"])
                    log.info("Monitoring packets from low-side host %r are arriving.", m["host"])
                m["rx"] = int(time.time() * 1000)
                if len(held) >= HOLD:
                    held.popleft()
                    stats["not_delivered"] += 1
                held.append(m)
                wake.set()
            except socket.timeout:
                pass
            now = time.monotonic()
            if now - last_hb >= HEARTBEAT:                 # the heartbeat waits in line too: its time is in "ts"
                last_hb = now
                held.append({"t": "rx_hb", "ts": int(time.time() * 1000), "info": dict(stats)})
                wake.set()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Stopping. %d packet(s): %d passed on, %d not valid, %d not delivered.",
                 stats["packets"], stats["passed"], stats["bad"], stats["not_delivered"])


def stop_now(*_):
    """docker stop sends SIGTERM; stop at once (as Ctrl-C would) instead of being killed 10 s later."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_now)
    main()
