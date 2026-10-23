"""When each of the last relayed messages was sent (sender's clock), reached Signal's server, and was handed to
signal-cli: times only, never content. Compare with the subscriber's "Sent message #N" log times. Run inside a subscriber container (its sent log):
  docker exec -i signal-sub-relaypoc python - < msgtimes.py"""
import glob, json, os, sys, time
log = os.environ.get("SENT_LOG") or sorted(glob.glob("/logs/*udp-sent.jsonl"))[-1]
utc = lambda ms: time.strftime("%H:%M:%S", time.gmtime(ms / 1000)) if isinstance(ms, (int, float)) else "–"
lines = open(log, encoding="utf-8").readlines()[-int(sys.argv[1] if len(sys.argv) > 1 else 10):]
print("msg   sent      at server  to signal-cli   (UTC)")
for ln in lines:
    w = json.loads(ln)
    env = (w.get("event") or {}).get("envelope") or {}
    sent, srv, dlv = env.get("timestamp"), env.get("serverReceivedTimestamp"), env.get("serverDeliveredTimestamp")
    print(f"#{w.get('m'):<4} {utc(sent)}  {utc(srv)}   {utc(dlv)}"
          + (f"   held at server {(dlv - srv) / 1000:.0f} s" if isinstance(dlv, int) and isinstance(srv, int) and dlv - srv > 5000 else ""))
