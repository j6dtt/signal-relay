#!/usr/bin/env python3
"""monitor-receiver: takes the monitoring packets from the diode's output and passes them on to playback.

Separate from the chat receiver (cef_to_json.py), so a fault or a restart here never touches chat, and the other
way round. Each packet is one CEF line, one message (no reassembly):
    CEF:0|J6-DTT|CHAT|1|1|SIGNAL-RELAY-MON|MON|{"t":"hb"|"st"|"ev","host",...}
Valid ones are POSTed to PLAYBACK_URL/monitor with "rx" (when it arrived, ms) added. Every 30 s it also posts its
own heartbeat ({"t":"rx_hb"}), so playback can tell a dead monitor-receiver from a silent left side.

Settings: UDP_LISTEN (0.0.0.0), UDP_PORT (6511), PLAYBACK_URL (http://playback:8091).
"""
import json, logging, os, signal, socket, time, urllib.error, urllib.request
from comlibv3 import parse_string_in_cef

LISTEN    = (os.environ.get("UDP_LISTEN", "0.0.0.0"), int(os.environ.get("UDP_PORT", 6511)))
PLAYBACK  = os.environ.get("PLAYBACK_URL", "http://playback:8091").rstrip("/")
TAG       = "SIGNAL-RELAY-MON"          # CEF Name field expected from signal-monitor
HEARTBEAT = 30
MAX_BYTES = 1472

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("monitor-receiver")


def post(body):
    """POST JSON to playback's /monitor. None on success, else a short error text."""
    req = urllib.request.Request(f"{PLAYBACK}/monitor", json.dumps(body).encode(), {"Content-Type": "application/json"})
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
        msgs, tag = parse_string_in_cef(data.decode("ascii"))
    except UnicodeDecodeError:
        raise ValueError("not ASCII")
    if tag != TAG:
        raise ValueError(f"CEF name {tag!r}, not {TAG}")
    if len(msgs) != 1 or not isinstance(msgs[0], dict) or msgs[0].get("t") not in ("hb", "st", "ev"):
        raise ValueError("not one monitoring message")
    return msgs[0]


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(LISTEN)
    sock.settimeout(1.0)
    stats = {"packets": 0, "passed": 0, "bad": 0, "not_delivered": 0}
    hosts = set()
    log.info("Listening for monitoring packets on UDP %s:%d. Passing them on to %s/monitor.", *LISTEN, PLAYBACK)
    last_hb, failing = 0.0, False
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
                    log.info("Monitoring packets from left host %r are arriving.", m["host"])
                m["rx"] = int(time.time() * 1000)
                err = post(m)
                if err:
                    stats["not_delivered"] += 1
                    if not failing:
                        log.error("Could not pass a monitoring message on to playback: %s. Retrying with the next ones.", err)
                    failing = True
                else:
                    stats["passed"] += 1
                    if failing:
                        log.info("Passing monitoring messages on to playback again.")
                    failing = False
            except socket.timeout:
                pass
            now = time.monotonic()
            if now - last_hb >= HEARTBEAT:
                last_hb = now
                err = post({"t": "rx_hb", "ts": int(time.time() * 1000), "info": dict(stats)})
                if err and not failing:
                    log.error("Could not send this receiver's heartbeat to playback: %s.", err)
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
