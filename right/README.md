# right/: the receiving side of the data diode

Runs the receiver and playback. The receiver listens at the diode's output, rebuilds each Signal message from its
CEF/UDP packets and delivers it. Playback is a temporary stand-in for Palantir: it shows the chat in a browser.
Nothing here can reach the left side (`../left`).

```
diode output ──UDP 6510──▶ receiver ──HTTP :8091 (this project's network only)──▶ playback ──HTTPS :8090──▶ browser
```

## Deploying
Copy this whole folder to the right host, then run `docker compose up -d` in it. It needs `python:3.13-alp-u3`.

In a fresh clone `playback/certs/` is empty (only a `.gitkeep`). Put the lab.int certificate and key there, then
`sudo chown -R <user>: playback/certs && sudo chmod 700 playback/certs && sudo chmod 600 playback/certs/*.key`.
`<user>` is the account the containers run as (`x-relay-user` at the top of `docker-compose.yml`).
Do the same for `logs/` (`sudo chown <user>: logs && sudo chmod 700 logs`): the receiver writes message records there.

| Folder | Holds |
|---|---|
| `receiver/` | `cef_to_json.py`, `comlibv3.py` |
| `playback/` | `playback_web.py`, `playback_web.html`, `chat_render.py` |
| `logs/` | `signal-records.jsonl`: one simple record per message, written by the receiver's `my_forward()`. Holds message text. |
| `playback/certs/` | the `*.lab.int` certificate and key for the chat page. Keep it mode 700, owned by the user in `x-relay-user`. |

## Settings (in `docker-compose.yml`)
- **Receiver `ports:`** `"172.16.0.46:6510:6510/udp"`: where the diode output delivers UDP. On the dev host
  this is the host's LAN address. Anyone on that network could send packets to it.
- **Playback `ports:`** `"0.0.0.0:8090:8090"`: the chat page, over HTTPS. It has no login.
  - Browse to a `*.lab.int` name. Browsers need the Lab Internal CA installed.
- **Port 8091** is where the receiver posts to playback. It is never published.

When Palantir delivery is built (in the receiver's `deliver()`), the `playback` service is removed.

## How the receiver rebuilds messages
Rebuilding is mostly bookkeeping: collect the parts of each message, put them in order, join them, and decode the
JSON once. The code is `Reassembler` in `receiver/cef_to_json.py`.

### 1. What the subscriber sends
Take a Signal event of 3,000 bytes of JSON text. A datagram may be at most 1,472 bytes, so the subscriber's
`split()` cuts the **text** into 3 slices. Each slice goes in its own CEF line, one per UDP datagram:
```
CEF:0|J6-DTT|CHAT|1|3|SIGNAL-RELAY|APP|
 {"s":"9f3a1c22","q":41,"m":17,
  "i":1,"n":3,"d":"{\"envelope\":{\"sou…"}

CEF:0|J6-DTT|CHAT|2|3|SIGNAL-RELAY|APP|
 {"s":"9f3a1c22","q":42,"m":17,
  "i":2,"n":3,"d":"…rceName\":\"Tho P…"}

CEF:0|J6-DTT|CHAT|3|3|SIGNAL-RELAY|APP|
 {"s":"9f3a1c22","q":43,"m":17,
  "i":3,"n":3,"d":"…ham\"}}}"}
```
The six fields:
- `s` is the session: random, and new each time the subscriber starts.
- `q` is the packet number, which goes up by 1 for every packet in the session.
- `m` is the message number. All parts of one message share it.
- `i` is this part's number, from 1 to n.
- `n` is how many parts the message has.
- `d` is a slice of the raw JSON **text**.

A slice isn't valid JSON by itself. It can even end in the middle of a word. Only the joined text is.

### 2. What the receiver does with each packet
`Reassembler.datagram()`:
```
UDP datagram
   │
   ▼
① parse    parse_string_in_cef()
           → header tag + the {s,q,m,i,n,d} JSON
           bad, or tag ≠ SIGNAL-RELAY, or not 1≤i≤n
           → "not a valid relay packet", ignored
   │
   ▼
② dedupe   seen (s,q) before? → duplicate,
           ignored
   │
   ▼
③ gaps     expected q=42 but got q=44?
           → "1 packet(s) never arrived (#43)"
   │
   ▼
④ collect  pending[(s,m)].parts[i] = d
   │
   ▼
⑤ all n parts here?
     no  → wait (up to 30 s)
     yes → complete()
```
The key is **(s, m)**, not only `m`. After a restart the subscriber counts messages from scratch, so `m=17` could
come round again. The new session number keeps the two apart.

### 3. Rebuilding: `complete()`
```
parts = {1:"{\"envel…", 2:"…rceNa…", 3:"…ham\"}}}"}
             │
   join in order 1,2,3 (any arrival order)
             │
             ▼
"{"envelope":{"sourceName":"Tho Pham"…}}"
             │
         json.loads()
             │
             ▼
the original signal-cli event (dict)
```
In the code it's one step:
```python
event = json.loads("".join(
    msg["parts"][k] for k in range(1, n + 1)))
```
Parts can arrive in any order because they're stored by `i`. If one is missing, the join never runs. If the joined
text isn't valid JSON, the message is logged as bad and skipped.

### 4. When parts never arrive
```
every loop (at least once a second):
  expire(): any message waiting > 30 s?
     → drop it
     → log "Gave up on message #17:
            only 2 of 3 packets arrived"
     → notice to playback: "a message was lost"
```
There are two other limits:
- **1,000 incomplete messages at most** (`MAX_PENDING`). When full, the oldest is dropped.
- **2,000 parts per message at most** (`MAX_PARTS`). Anything claiming more is treated as a bad packet.

### 5. After rebuilding
```
event
  ├─▶ Chat.record(event) → my_forward(record)
  └─▶ deliver(event) → playback (later Palantir)
```

### Writing your own
The minimum:
```python
import json
from comlibv3 import parse_string_in_cef

pending = {}   # (s, m) -> {i: d}

def on_datagram(data):
    parts, tag = parse_string_in_cef(
        data.decode("ascii"))
    if tag != "SIGNAL-RELAY":
        return None
    p = parts[0]
    got = pending.setdefault((p["s"], p["m"]), {})
    got[p["i"]] = p["d"]
    if len(got) < p["n"]:
        return None            # still waiting
    del pending[(p["s"], p["m"])]
    text = "".join(got[k] for k in range(1, p["n"] + 1))
    return json.loads(text)    # the Signal event
```
This sketch leaves out the duplicate check, gap reporting, the 30-second timeout and the size limits. To get those,
use the real `Reassembler` and pass it a function. `cef_to_json.py` imports `chat_render.py`, so both must be on
the import path:
```python
from cef_to_json import Reassembler

r = Reassembler(on_event=lambda ev, m: print(ev))
r.datagram(data)    # for every UDP packet
r.expire()          # about once a second
```
Both examples were tested with a 3-part message arriving out of order (2026-10-16).

## After editing a script
Run `docker compose up -d --force-recreate <receiver|playback>`. A plain restart can keep the old file.
Recreating them also clears test messages and counters.
