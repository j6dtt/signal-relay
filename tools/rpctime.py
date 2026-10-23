"""Times signal-cli's answers, from inside a subscriber container (its certificate, read-only calls only):
  docker exec -i signal-sub-relaypoc python - < rpctime.py"""
import json, os, ssl, time, urllib.request

url = os.environ["SIGNAL_URL"]
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.load_verify_locations(os.environ.get("TLS_CA", "/certs/ca.crt"))
ctx.load_cert_chain(os.environ.get("TLS_CERT", "/certs/client.crt"), os.environ.get("TLS_KEY", "/certs/client.key"))

def timed(what, req):
    t = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
            body = r.read()
        print(f"{what:22s} {time.monotonic() - t:6.2f} s  ({len(body)} bytes)")
    except Exception as e:
        print(f"{what:22s} {time.monotonic() - t:6.2f} s  failed: {e!r}")

rpc = lambda: urllib.request.Request(f"{url}/api/v1/rpc", headers={"Content-Type": "application/json"},
    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "listContacts", "params": {"allRecipients": True}}).encode())
timed("health check", f"{url}/api/v1/check")
for i in range(3):
    timed(f"contact list, try {i + 1}", rpc())
