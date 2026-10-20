#!/usr/bin/env python3
"""diode-sim: DEV ONLY. Stands in for the data diode's routing, which dev doesn't have.

The real diode reads fields 1 and 2 of each CEF header (CEF:0|CHAT|<ROOM>|...) and sends the packet to that room's
receiver, from its own routes file. This does the same on one UDP port, so dev can run one receiver per room as
prod does. It changes nothing in the packets and keeps nothing. A packet whose header has no route is dropped and
logged (at most once a minute per header), as the diode would drop it. Not part of prod: comment it out there.

Settings: UDP_LISTEN (0.0.0.0), UDP_PORT (6510), and the routes:
  ROUTES_FILE (e.g. /app/routes.conf): one route per line, "ROOM  host:port" (host = the receiver's container name),
    # comments. Read again when it changes (checked every 2 s), so a new receiver's route needs no restart and the
    other rooms keep flowing, as with the real diode's routes file. A file that doesn't parse is not used: the last
    good routes stay and the problem is logged once.
  else ROUTES: "ROOM=host:port, ..." (e.g. RELAY=signal-receiver-relay:6510), read at start.
"""
import logging, os, signal, socket, sys, time

LISTEN = (os.environ.get("UDP_LISTEN", "0.0.0.0"), int(os.environ.get("UDP_PORT", 6510)))
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stderr,
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("diode-sim")


def routes(text):
    """ "RELAY=signal-receiver-relay:6510, SELFTEST=signal-receiver-selftest:6510" -> {b"RELAY": ("signal-receiver-relay", 6510), ...}"""
    out = {}
    for item in filter(None, (x.strip() for x in text.split(","))):
        room, _, dest = item.partition("=")
        host, _, port = dest.rpartition(":")
        if not (room and host and port.isdigit()):
            sys.exit(f"diode-sim: bad route {item!r} (expected ROOM=host:port)")
        out[room.strip().encode()] = (host.strip(), int(port))
    return out


def routes_file(path):
    """ "RELAY  signal-receiver-relay:6510" lines -> the same table as routes(). Raises ValueError on a bad line."""
    out = {}
    with open(path) as f:
        for no, line in enumerate(f, 1):
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            host, _, port = (parts[1] if len(parts) == 2 else "").rpartition(":")
            if len(parts) != 2 or not (host and port.isdigit()):
                raise ValueError(f"line {no}: {line!r} is not ROOM host:port")
            if parts[0].encode() in out:
                raise ValueError(f"line {no}: room {parts[0]} has two routes")
            out[parts[0].encode()] = (host, int(port))
    if not out:
        raise ValueError("it lists no route")
    return out


class Routes:
    """The routes table; from ROUTES_FILE, read again when it changes, else from ROUTES."""
    def __init__(self):
        self.path, self.stamp, self.checked = os.environ.get("ROUTES_FILE", ""), None, 0
        if self.path:
            try:
                self.table, self.stamp = routes_file(self.path), self._stamp()
            except (OSError, ValueError) as e:
                sys.exit(f"diode-sim: can't use {self.path} ({e})")
        else:
            self.table = routes(os.environ.get("ROUTES", ""))
            if not self.table:
                sys.exit("diode-sim: ROUTES_FILE and ROUTES are both empty (expected ROOM host:port lines)")
        log.info("DEV ONLY: standing in for the diode's routing on UDP %s:%d. Routes%s: %s.", *LISTEN,
                 f" ({self.path})" if self.path else "", self.describe())

    def _stamp(self):
        st = os.stat(self.path)
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def describe(self):
        return ", ".join(f"CHAT|{r.decode()} -> {h}:{p}" for r, (h, p) in self.table.items())

    def check(self):
        """Read the file again if it changed (at most every 2 s)."""
        if not self.path or time.monotonic() - self.checked < 2:
            return
        self.checked = time.monotonic()
        try:
            stamp = self._stamp()
        except OSError:
            return                                # gone for a moment (being saved): try again next time
        if stamp == self.stamp:
            return
        self.stamp = stamp
        try:
            new = routes_file(self.path)
        except (OSError, ValueError) as e:
            log.error("%s changed but can't be used, so the last good routes stay (%s).", self.path, e)
            return
        added = [r.decode() for r in new if r not in self.table]
        gone = [r.decode() for r in self.table if r not in new]
        self.table = new
        log.info("Routes changed: %s.%s%s", self.describe(), f" Added: {', '.join(added)}." if added else "",
                 f" Removed: {', '.join(gone)}." if gone else "")


def stop_now(*_):
    raise KeyboardInterrupt


def main():
    table = Routes()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    sock.bind(LISTEN)
    sock.settimeout(2)                            # so a changed routes file is seen when nothing arrives too
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    passed, dropped, warned = {}, {}, {}
    try:
        while True:
            table.check()
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            f = data.split(b"|", 3)
            room = f[2] if len(f) == 4 and f[0] == b"CEF:0" and f[1] == b"CHAT" else None
            dest = table.table.get(room)
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
