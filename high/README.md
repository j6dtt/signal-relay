# high/: the high side (receiving side) of the data diode

Runs the receivers (one per chat room), monitor-receiver and the console. The diode sends each packet, by the room in
its CEF header, to that room's receiver, which rebuilds each Signal message and hands it to `my_forward` (prod: the
Palantir Gotham feed) and to the console. monitor-receiver takes the low side's monitoring packets. The console shows
the live diagram of the whole relay in a browser, behind a login, with a window per Signal group, and keeps 30 days
of history. In dev it is the final destination; in prod it runs next to the Gotham feed, for checks and
troubleshooting. Nothing here can reach the low side (`../low`).

```
diode ─┬─ room Signal Relay ──────▶ receiver-relay    ─my_forward▶ Gotham Chat
       ├─ room Signal-API-SelfTest ▶ receiver-selftest (FORWARD_TO off: goes no further)
       └─ monitoring ─────────────▶ monitor-receiver
                       all three ──HTTP :8091 (this project's network only)──▶ console
                                    console ──HTTPS :8090 (login)──▶ browser
                                       └─ data/relay.db (SQLite)
```
Rooms and their groups are set on the low side (`../low/routes.conf`); here each receiver says which room it
serves (`ROOMS`). Dev has no diode: `diode-sim` does its routing on 6510. The design: `../docs/room-routing-design.md`;
monitoring: `../docs/diagrams/09-monitoring-design.pdf`.

## Deploying
Copy this whole folder to the high-side host, then run `docker compose up -d` in it. It needs `python:3.13-alp-u3`.

In a fresh clone `console/certs/` is empty (only a `.gitkeep`). Put the lab.int certificate and key there, then
`sudo chown -R <user>: console/certs && sudo chmod 700 console/certs && sudo chmod 600 console/certs/*.key`.
`<user>` is the account the containers run as (`relay-user` in the settings block of `docker-compose.yml`).
Do the same for `logs/` and `data/` (`sudo chown <user>: logs data && sudo chmod 700 logs data`): the receivers write
message records and their progress to `logs/`, and the console its database to `data/`.

Then create the first user (see [Users](#users)):
```
docker exec -it signal-console python console_users.py add admin --random
```

| Folder | Holds |
|---|---|
| `receiver/` | `cef_to_json.py`: one receiver container per room, all from this script |
| `monitor/` | `monitor_receiver.py` |
| `shared/` | `comlibv3.py`, `chat_render.py` (mounted at `/shared` in the receivers, monitor-receiver and the console) |
| `diode-sim/` | `diode_sim.py`: **dev only**, stands in for the diode's routing by header |
| `assist/` | `relay_assist.py`: the Analyst (AI assistant) |
| `.env` | secrets for compose (gitignored, mode 600): `NVIDIA_API_KEY` (dev model key), `RELAY_ASSIST_TOKEN` |
| `console/` | `console_web.py`, `console_web.html`, `console_login.html`, `store.py` (the database), `auth.py` (login), `monitor.py` (the diagram's states), `incidents.py` and `runbook.json` (likely causes), `console_users.py` (users, tokens) |
| `data/` | `relay.db`: the console's database. Chat history (message content), monitoring, users, sessions. Keep it mode 700. |
| `logs/` | per receiver: `<room>-records.jsonl` (one simple record per message, written by `my_forward()`; holds message text) and `<room>-progress.json` (how far each sender got, my_forward's counts; no content) |
| `console/certs/` | the `*.lab.int` certificate and key for the chat page. Keep it mode 700, owned by the user in `relay-user` (settings block). |

## Settings (in `docker-compose.yml`)
Everything that differs per host is in the **settings block** (`x-settings`) at the top of `docker-compose.yml`:
change the values there only (relay-user, listen-ip, web-bind, web-cert/web-key, ai-url, ai-model, ai-log, chat-view,
retention-days), then `docker compose up -d`. Secrets go in `.env`.

**One block per receiver (per room).** What every receiver shares is the `x-receiver` / `x-receiver-env` template;
each block adds its own:
```yaml
  receiver-relay:
    <<: *receiver
    container_name: signal-receiver-relay
    environment:
      <<: *receiver-env
      ROOMS: RELAY                        # the room's short name, as in routes.conf and the diode's route
      FORWARD_TO: Gotham Chat             # where my_forward delivers it; "off" = this room goes no further
      RECORD_LOG: /logs/relay-records.jsonl
      PROGRESS_FILE: /logs/relay-progress.json
    # ports:                              # prod: the port the diode's route for this room points at
    #   - {target: 6510, published: "6512", host_ip: *listen-ip, protocol: udp}
```
- **To add a room:** a diode route (`CHAT|<ROOM>`), its lines in `../low/routes.conf`, and a copied block here with
  its own name, `ROOMS`, files and port.
- **`ROOMS`:** A-Z 0-9 - _ only; `MONITOR` is reserved. A packet for any other room is dropped and counted
  (ROOM_MISMATCH: a diode route pointing at the wrong receiver). The page never shows short names: a room is called
  by its Signal group(s), e.g. "Signal Relay".
- **`FORWARD_TO`:** set in each block (the template's value is only the default). The page's destination box is
  named after it.
- **`diode-sim` (dev only):** publishes 6510 and sends each packet to its receiver by header, as the diode does.
  Prod: comment it out, and uncomment each receiver's `ports:` and monitor-receiver's (the ports the diode's routes
  point at, on `listen-ip`).
- **console `ports:`** 8090 on `web-bind`: the page, over HTTPS, with a login. Browse to a `*.lab.int` name;
  browsers need the Lab Internal CA installed.
- **Port 8091** is where the receivers and monitor-receiver post to the console. It is never published.
- **`chat-view`** (settings block): `on` shows and keeps message text; `off` never stores or shows it: each group
  window then has its delivery figures only. Monitoring works the same either way.
- **`DB_PATH`** (`/data/relay.db`) and **`RETENTION_DAYS`** (30): the console's database, and how long anything is
  kept. Older chat, monitoring events and link counts are deleted every hour.

## The page
One page: the live diagram of the relay, low side, diode and high side, every part as a box with its state, wires
coloured by state, each room on its own row (subscriber → room → receiver → Gotham Chat). Under it, the last 24
hours per box; a lane opens that box's log. The bell (next to "Live") lists open incidents; each opens a drawer with
its likely cause, the runbook's checks and the Analyst's answer. **Ask Analyst** asks about the relay as a whole.
- **Group windows:** one floating window per Signal group (plus "All"), to check that its messages got through
  the diode, not to read chats. On top, its delivery figures per room: sent by the subscriber this run → arrived,
  its receiver, the last arrival, and its room's lost messages. Several can be open; drag by the title, resize from
  the corner; each browser remembers which were open. Escape closes the one you're in.
- **Opening one:** the dock at the bottom (a chip per group, its delivery dot and unread count); a room marker or
  its wire on the diagram; a subscriber's "rooms" button; or a link `https://<host>:8090/#g=<first 8 characters of
  the group ID>`. A dot runs along a group's wires whenever its message arrives.
- **Chat / Terminal** (top right): the look of the page and the windows.
- History: each window keeps its last 500 lines; everything is also in `data/relay.db` for 30 days (with
  `chat-view: on`), so a restart rebuilds the windows. The same message twice (e.g. two accounts in one group, or a
  group in two rooms) is shown once.

Everything about monitoring, including what to expect when something stops:
**[`../docs/monitoring.md`](../docs/monitoring.md)**.

## Users
Local users, kept in the database (scrypt password hashes). There is no sign-up page.
```
docker exec -it signal-console python console_users.py add NAME            # asks for the password (12+ characters)
docker exec -it signal-console python console_users.py add NAME --random   # makes one, shows it once
docker exec -it signal-console python console_users.py passwd NAME [--random]
docker exec -it signal-console python console_users.py remove NAME
docker exec -it signal-console python console_users.py list
docker exec -it signal-console python console_users.py token add NAME      # for a tool: reads the monitoring API only
```
**Reset a forgotten password** (for example admin's): on the high-side host, run
```
docker exec -it signal-console python console_users.py passwd admin --random
```
It prints the new password once and logs admin out everywhere. Leave out `--random` to type your own (12+
characters). If 5 wrong tries locked the name, wait 5 minutes, or restart the console (`docker restart signal-console`).

- A session lasts 12 hours. A new password or removing a user logs them out everywhere within a minute.
- 5 wrong passwords in a row from one address lock that user name there for 5 minutes. Every login and failure
  is logged by the console.
- Every user sees everything (the diagram and every group window).

The relay's job ends at the simple record handed to `my_forward` in the receiver. What carries it on to Palantir
Gotham is outside this project.

## How the receiver rebuilds messages
Rebuilding is mostly bookkeeping: collect the parts of each message, put them in order, join them, and decode the
JSON once. The code is `Reassembler` in `receiver/cef_to_json.py`.

### 1. What the subscriber sends
Take a Signal event of 3,000 bytes of JSON text. A datagram may be at most 1,472 bytes, so the subscriber's
`split()` cuts the **text** into 3 slices. Each slice goes in its own CEF line, one per UDP datagram:
```
CEF:0|CHAT|RELAY|1|3|SIGNAL-RELAY|APP|
 {"s":"9f3a1c22","q":41,"m":17,
  "i":1,"n":3,"d":"{\"envelope\":{\"sou…"}

CEF:0|CHAT|RELAY|2|3|SIGNAL-RELAY|APP|
 {"s":"9f3a1c22","q":42,"m":17,
  "i":2,"n":3,"d":"…rceName\":\"Tho P…"}

CEF:0|CHAT|RELAY|3|3|SIGNAL-RELAY|APP|
 {"s":"9f3a1c22","q":43,"m":17,
  "i":3,"n":3,"d":"…ham\"}}}"}
```
Field 2 of the header is the room (`RELAY`): the diode routes on `CHAT|RELAY`, and the receiver drops a packet for a
room it doesn't serve. The six fields after it:
- `s` is the session: random, new each time the subscriber starts, and different for each room it sends.
- `q` is the packet number, which goes up by 1 for every packet in the session (so per room: no gaps from other rooms).
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
           → its room (header field 2) + tag + the {s,q,m,i,n,d} JSON
           a room it doesn't serve → dropped, counted (ROOM_MISMATCH)
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
     → notice to console: "a message for <room> was lost"
```
There are two other limits:
- **1,000 incomplete messages at most** (`MAX_PENDING`). When full, the oldest is dropped.
- **2,000 parts per message at most** (`MAX_PARTS`). Anything claiming more is treated as a bad packet.

### 5. After rebuilding
```
event
  ├─▶ Chat.record(event) → my_forward(record)   (FORWARD_TO; prod: hand-off toward Gotham)
  │     returns normally = delivered; raises or returns False = failed (FORWARD_FAILED)
  └─▶ deliver(event) → console                  (with its room and session, for the windows' figures)
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

r = Reassembler(on_event=lambda ev, m, *route: print(ev))   # route: session, room
r.datagram(data)    # for every UDP packet
r.expire()          # about once a second
```
Both examples were tested with a 3-part message arriving out of order (2026-10-16).

## Moving to Kubernetes
Planned: TLS ends at ingress-nginx; the diode reaches each receiver through a UDP LoadBalancer or NodePort Service.
What the code needs is in place; the rest is manifests.
- **the console behind the ingress:** set `TRUST_PROXY=1` and leave out `WEB_TLS_CERT`/`WEB_TLS_KEY` (the console then
  serves plain HTTP to the ingress). It takes the browser's address and http/https from `X-Real-IP` /
  `X-Forwarded-For` / `X-Forwarded-Proto`, so the login lockout counts per real address and the session cookie stays
  Secure. Only when port 8090 is reachable through the ingress alone (a NetworkPolicy): the headers can be faked.
  ingress-nginx keeps the `Host` header by default; keep it so (the console checks `Origin` against it). Websockets work
  through it; the page gets something every 10 s, inside ingress-nginx's 60 s read timeout.
- **One replica each, `strategy: Recreate`:** a receiver rebuilds messages in memory from UDP parts (two pods would
  each get half of a message); the console keeps SQLite, the monitor state and the browsers' live updates in one
  process (Postgres via `store.py` alone would not make it multi-replica).
- **UDP in:** a Service per receiver (the port its room's diode route points at) and one for monitor-receiver
  (`CHAT|MONITOR`'s); diode-sim is not needed. Use `externalTrafficPolicy: Local` and run
  the pods on the node the diode reaches: kube-proxy then doesn't forward between nodes, the source address is
  kept, and packets don't cross the pod overlay network. Check the CNI's MTU: datagrams are up to 1500 bytes
  (1472 + headers); an overlay MTU of 1450 would fragment them, and a lost fragment loses the whole packet.
- **Storage:** persistent volumes for `data/` (relay.db, message content) and the receivers' `logs/` (progress files,
  records), owned by the pod's user (`runAsUser`/`fsGroup`), mode 700.
- **Secrets and config:** `.env` (AI key, relay-assist token), the certificates and `runbook.json` become Secrets /
  ConfigMaps. The scripts need an image per app (they are mounted from files here); the receiver also needs
  `chat_render.py` and `comlibv3.py`.
- **Names:** `CONSOLE_URL`, `ASSIST_URL`, `CONSOLE_API` are settings: name the Services `console` and
  `relay-assist`, or set them. Keep 8091 inside the cluster. The host on the diagram: set `HOST_NAME` (there is
  no `/etc/hostname` mount).
- **Stopping:** every high-side app stops at once on SIGTERM (exit 0); the console closes open pages first, and they
  reconnect by themselves.
- **Receiver names:** each receiver shows by its container name (found by Docker's DNS); on k8s set
  `RECEIVER_NAME` per receiver.
- **Still Docker words:** the runbook's high-side checks and `console_users.py`'s help say `docker compose` /
  `docker exec`; use `kubectl` equivalents until they are changed.

## After editing a script
Each app's folder is mounted read-only (shared code in `shared/`), so after editing a script a restart is
enough: `docker compose restart <service>`. After editing `docker-compose.yml` (its settings block included):
`docker compose up -d`, which recreates only what changed.
