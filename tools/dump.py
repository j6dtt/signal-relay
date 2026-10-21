# View raw Signal events for one group, pretty-printed, straight from signal-cli's event stream. Viewing only.
# Runs on the host (Python standard library only). Connects to the signal-cli container's TLS gateway with the
# subscriber's certificate; host names of containers don't resolve on the host, so it looks up the container's
# address with `docker inspect`, and still checks the server certificate under its name "signal".
# Each client gets its own copy of the stream, so this doesn't affect the relay's subscriber.
#   python3 tools/dump.py [GROUP_ID]          GROUP_ID: internal id (ends in "="); "" shows all groups
# Split setup (signal-cli on another host): SIGNAL_ADDR=ubt2:443 SIGNAL_NAME=ubt2 python3 tools/dump.py
import http.client, json, os, socket, ssl, subprocess, sys, time
from pathlib import Path

GROUP = sys.argv[1] if len(sys.argv) > 1 else ""      # no argument: every group
CERTS = Path(__file__).resolve().parent.parent / "left" / "certs" / "subscriber"
NAME  = os.environ.get("SIGNAL_NAME", "signal")           # a name in signal-cli's server certificate

def address():
    if os.environ.get("SIGNAL_ADDR"):
        host, _, port = os.environ["SIGNAL_ADDR"].rpartition(":")
        return host, int(port)
    ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}",
                         "signal-cli"], capture_output=True, text=True, check=True).stdout.split()
    if not ip:
        raise OSError("the signal-cli container has no network address (is it running?)")
    return ip[0], 8443

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)             # checks the certificate and its name
ctx.minimum_version = ssl.TLSVersion.TLSv1_3
ctx.load_verify_locations(CERTS / "ca.crt")
ctx.load_cert_chain(CERTS / "client.crt", CERTS / "client.key")

def events():
    """Yield each event ({"envelope", "account"}); reconnects on its own, without repeats."""
    last_id = None
    while True:
        try:
            host, port = address()
            # connect to the address, but check the certificate under NAME (keep-alives come every 15 s)
            conn = http.client.HTTPConnection(host, port)
            conn.sock = ctx.wrap_socket(socket.create_connection((host, port), timeout=60), server_hostname=NAME)
            headers = {"Accept": "text/event-stream", "Host": NAME}
            if last_id:
                headers["Last-Event-ID"] = last_id
            conn.request("GET", "/api/v1/events", headers=headers)
            resp = conn.getresponse()
            if resp.status != 200:
                raise OSError(f"signal-cli answered {resp.status} {resp.reason}")
            print(f"Connected to signal-cli at {host}:{port}. Waiting for messages"
                  f"{' in group ' + GROUP if GROUP else ''} (Ctrl-C to stop)...", file=sys.stderr)
            data, event_id = [], None
            for raw in resp:                                  # one SSE line at a time
                line = raw.decode("utf-8").rstrip("\r\n")
                if line:
                    field, _, value = line.partition(":")
                    value = value[1:] if value.startswith(" ") else value
                    if field == "data":
                        data.append(value)
                    elif field == "id":
                        event_id = value
                    continue
                if data:                                      # a blank line ends one event
                    obj = json.loads("\n".join(data))
                    p = obj.get("params", obj) if isinstance(obj, dict) else {}
                    yield p.get("result", p) if isinstance(p, dict) else {}
                if event_id:
                    last_id = event_id
                data, event_id = [], None
            print("signal-cli closed the event stream. Reconnecting in 3 seconds...", file=sys.stderr)
        except (OSError, http.client.HTTPException, subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"Lost the connection to signal-cli ({e}). Reconnecting in 3 seconds...", file=sys.stderr)
        time.sleep(3)

try:
    for msg in events():
        env = msg.get("envelope") or {}
        # own-device messages arrive as syncMessage.sentMessage
        dm  = env.get("dataMessage") \
              or (env.get("syncMessage") or {}).get("sentMessage") \
              or {}
        gid = (dm.get("groupInfo") or {}).get("groupId")
        if not gid or (GROUP and gid != GROUP):
            continue
        print(json.dumps(msg, indent=2), flush=True)
except KeyboardInterrupt:
    pass
