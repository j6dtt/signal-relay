#!/usr/bin/env python3
"""diode-sim: DEV ONLY. Stands in for the data diode's routing, which dev doesn't have.

The real diode reads fields 1 and 2 of each CEF header (CEF:0|CHAT|<ROOM>|...) and sends the packet to that room's
receiver, from its own routes file. This does the same on one UDP port, so dev can run one receiver per room as
prod does. It changes nothing in the packets and keeps nothing. A packet whose header has no route is dropped and
logged (at most once a minute per header), as the diode would drop it. Not part of prod: comment it out there.

Settings: UDP_LISTEN (0.0.0.0), UDP_PORT (6510), ROUTES: "ROOM=host:port, ..." (e.g. RELAY=receiver-relay:6510).
"""
import logging, os, signal, socket, sys, time

LISTEN = (os.environ.get("UDP_LISTEN", "0.0.0.0"), int(os.environ.get("UDP_PORT", 6510)))
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr,
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("diode-sim")


def routes(text):
    """ "RELAY=receiver-relay:6510, SELFTEST=receiver-selftest:6510" -> {b"RELAY": ("receiver-relay", 6510), ...}"""
    out = {}
    for item in filter(None, (x.strip() for x in text.split(","))):
        room, _, dest = item.partition("=")
        host, _, port = dest.rpartition(":")
        if not (room and host and port.isdigit()):
            sys.exit(f"diode-sim: bad route {item!r} (expected ROOM=host:port)")
        out[room.strip().encode()] = (host.strip(), int(port))
    return out


def stop_now(*_):
    raise KeyboardInterrupt


def main():
    table = routes(os.environ.get("ROUTES", ""))
    if not table:
        sys.exit("diode-sim: ROUTES is empty (expected ROOM=host:port, ...)")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    sock.bind(LISTEN)
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    log.info("DEV ONLY: standing in for the diode's routing on UDP %s:%d. Routes: %s.", *LISTEN,
             ", ".join(f"CHAT|{r.decode()} -> {h}:{p}" for r, (h, p) in table.items()))
    passed, dropped, warned = {}, {}, {}
    try:
        while True:
            data, _ = sock.recvfrom(65535)
            f = data.split(b"|", 3)
            room = f[2] if len(f) == 4 and f[0] == b"CEF:0" and f[1] == b"CHAT" else None
            dest = table.get(room)
            if dest:
                try:
                    out.sendto(data, dest)        # the name is looked up each time: a restarted receiver gets a new IP
                    passed[room] = passed.get(room, 0) + 1
                except OSError as e:
                    log.warning("Could not pass a packet for %s on to %s:%d (%s).", room.decode(), *dest, e)
                continue
            head = b"|".join(f[:3])[:60].decode("ascii", "replace")
            dropped[head] = dropped.get(head, 0) + 1
            if time.monotonic() - warned.get(head, -60) >= 60:
                warned[head] = time.monotonic()
                log.warning("No route for a packet with header %s|..., so it was dropped (%d so far), as the diode "
                            "would.", head, dropped[head])
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Stopping. Passed on: %s.", ", ".join(f"{r.decode()} {n}" for r, n in passed.items()) or "nothing")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_now)
    main()
