# Room routing: one receiver per Gotham room

Status: **built and live on dev** (2026-10-18 to 19, steps 0-8). Written as the proposal; where the build differs:

- **Names on the page:** a room is shown by its Signal group(s) ("Signal Relay"), monitoring as "Monitoring";
  never the CEF header or the short name (user). Short names (RELAY) stay in routes.conf, `ROOMS`, the diode routes.
- **Diagram:** rooms are slim one-line markers in the diode band (`Signal Relay  2 → 2 ✓`), not tiles; the
  monitoring link is the same kind of marker on signal-monitor's row. Each receiver has a **destination box**
  ("Gotham Chat", from `my_forward`: delivered = returned without an error) unless its `FORWARD_TO` is "off"; the
  console's box sits above the receivers. No ≡ on boxes: a box's Log opens from its timeline lane.
- **Group windows:** one per Signal group (user chose per group over per room), with per-route figures; the dock,
  room markers/wires and a subscriber's rooms button open them; pulses run subscriber → room → receiver → Gotham.
- **Dev has a diode stand-in:** `high/diode-sim` routes by header on 6510 (prod: comment it out, publish each
  receiver's port). Monitoring goes through the same diode input (`monitor-port`), routed as `CHAT|MONITOR`.
- **The old header is gone:** `ACCEPT_OLD_HEADER` was a step 3-6 bridge and was removed in step 8.
- **`my_forward` contract:** raises or returns False = failed; the caller also catches SystemExit; its counts are
  kept in the receiver's progress file. Open: how the user's `mss_stream` reports a failure (see CLAUDE.md).

## 1. What changes, in one picture
```
LOW SIDE                         DIODE (its own routes file)      HIGH SIDE
signal-cli ─┬─ subscriber   ═╗
            │   OPS, INTEL    ║   CHAT|OPS     ─▶ ip:port ─▶  receiver-ops    ─▶ my_forward (OPS room)
            └─ subscriber-2 ═╬═▶ CHAT|INTEL   ─▶ ip:port ─▶  receiver-intel  ─▶ my_forward (INTEL room)
                LOG           ║   CHAT|LOG     ─▶ ip:port ─▶  receiver-log    ─▶ my_forward (LOG room)
signal-monitor ══════════════╝   CHAT|MONITOR ─▶ ip:port ─▶  monitor-receiver
                                                                   every receiver ─HTTP 8091─▶ the console (all rooms)
```
- A **room** is a diode route. The subscriber puts it in the CEF header; the diode sends each room to its receiver.
- **Prod: one receiver per room.** Several rooms on one receiver only where nothing routes them apart (dev).
- The console gets every room because every receiver also delivers to it, as today.
- Unchanged: one-way only, CEF over UDP only, 1472-byte datagrams, ASCII JSON, `comlibv3.py` untouched,
  the simple record's 8 keys, signal-cli, container-facts, relay-assist, the console's group tabs.

## 2. The wire
```
chat:     CEF:0|CHAT|<ROOM>|{i}|{n}|SIGNAL-RELAY|APP|{"s":..,"q":..,"m":..,"i":..,"n":..,"d":".."}
monitor:  CEF:0|CHAT|MONITOR|1|1|SIGNAL-RELAY-MON|MON|{...}
```
- The diode reads fields 1 and 2 only (`CHAT`, room). The whole header is in capitals.
- **Room names:** `A-Z 0-9 - _`, 1 to 32 characters. `MONITOR` is reserved. This also keeps out what would
  break the wire: `|` (comlibv3 splits on it without escapes), `\` (CEF escape), `{ }` (comlibv3 fills the
  header with `format()`), non-ASCII.
- Was: `CEF:0|J6-DTT|CHAT|…` for both. Nothing uses `J6-DTT` any more.
- `{i}/{n}` stay part number / part count. Packet counting stays in the JSON (`s`, `q`, `m`).

## 3. Low side

### 3.1 `low/routes.conf` (new; the only list of relayed groups)
```
# Which groups go to which room, per subscriber.
# Room: A-Z 0-9 - _ only. MONITOR is reserved.
# Columns: subscriber, room, groups (comma-separated). A line starting with a space continues the groups.

signal-subscriber    OPS    ExampleGroup1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=,
                            ExampleGroup3AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
signal-subscriber    INTEL  ExampleGroup2AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
signal-subscriber-2  LOG    vJ3tw/ZLyfK7iC9tIMzgCE5sWBb1xng0959dZU8BdYA=
```
- Replaces `groups` in the settings block and `GROUP` on each subscriber.
- Mounted read-only in every subscriber (`./routes.conf:/routes.conf:ro`); after an edit, restart the subscriber(s).
- The same file on every low-side host (the `low/` folder is copied as is), so every check sees the whole plan.
- Group names go in comments, as in the settings block today.

### 3.2 Start-up checks (the subscriber refuses to start, with a plain-English error)
| Check | Why |
|---|---|
| File missing or unreadable | never relay by accident |
| No lines for this subscriber (`ROUTES_FOR`) | nothing to do: a typo in the name |
| Bad room name, or `MONITOR` | the diode and the wire can't carry it |
| A group ID that isn't 44 base64 characters | typo |
| **The same group in the same room twice, on any subscriber** (user: refuse) | Gotham would get every message twice |

A group in **two different rooms** is allowed: it is sent once per room.
A refused start exits with an error; Docker restarts it, so the tile shows RESTARTING and the log says why.

### 3.3 Subscriber: a stream per room
- At each start, every room gets its **own session** `s` (random) and its own counters `q` (packets), `m`
  (messages). To the receivers each (subscriber run, room) is just another sender: their gap, duplicate and
  timeout logic works unchanged, and no room sees another room's numbers.
- An event whose group is in N rooms is split and sent N times, once per room stream.
- Events with no routed group (other groups, receipts without a group) are ignored, as today.
- The sent log line gets `"room"`.
- Status to signal-monitor: `flow` becomes per room:
  `"flow": {"rooms": {"OPS": {"sess", "m", "m_age"}, ...}, "read", "lw"}`. signal-monitor passes it on unchanged.
- Log lines name the room: `Sent message #3 to OPS (reply from …) as 2 packets (#5 to #6).`

### 3.4 Compose (low)
```yaml
x-subscriber: &subscriber          # shared by every subscriber
  image: python:3.13-alp-u3
  restart: unless-stopped
  user: *relay-user
  working_dir: /app
  command: ["python", "subscriber_udp.py"]
  mem_limit: 256m
  pids_limit: 64
  volumes:
    - ./subscriber:/app:ro
    - ./shared:/shared:ro
    - ./logs:/logs
    - ./certs/subscriber:/certs:ro
    - ./routes.conf:/routes.conf:ro

x-subscriber-env: &subscriber-env
  SIGNAL_URL: *signal-url
  UDP_DEST: *diode-in
  UDP_PORT: 6510
  ROUTES_FILE: /routes.conf
  MONITOR_ADDR: signal-monitor:6520
  PYTHONPATH: /shared
  PYTHONDONTWRITEBYTECODE: "1"

services:
  subscriber:
    <<: *subscriber
    container_name: signal-subscriber
    environment:
      <<: *subscriber-env
      ROUTES_FOR: signal-subscriber
      STATE_FILE: /logs/signal-last-event-id
      SENT_LOG: /logs/signal-udp-sent.jsonl

  subscriber-2:
    <<: *subscriber
    container_name: signal-subscriber-2
    environment:
      <<: *subscriber-env
      ROUTES_FOR: signal-subscriber-2
      STATE_FILE: /logs/subscriber-2-last-event-id
      SENT_LOG: /logs/subscriber-2-udp-sent.jsonl
```
- signal-monitor: header changes to `CHAT|MONITOR`, still to `diode-in` port 6511. No other change.
- Subscribers on another host: as today (comment out what doesn't run there, `signal-url` to the signal-cli host,
  its own signal-monitor and container-facts, `RELAY_ID`).

## 4. High side

### 4.1 Receiver
- New setting **`ROOMS`**: prod one room (`OPS`), dev a list (`OPS,INTEL,LOG`).
- Reads field 2 of each packet itself (comlibv3's parser returns only the Name).
- A packet for a room **not in `ROOMS`**: dropped (it must never reach the wrong Gotham room), counted per room
  as `ROOM_MISMATCH`. The diode sent it to the wrong place.
- Per room it counts packets, messages, lost; sessions are already separate (one per room per subscriber run).
- `my_forward(record)` unchanged, 8 keys. Each prod receiver serves one room, so your hand-off code takes the
  Gotham room from `ROOMS`. If `ROOMS` has more than one room it logs a warning at start (my_forward can't tell
  them apart); a `room` key is added only when that case is real (user).
- Lost notices name the room: `Message #12 from OPS was lost (3 of 5 packets missing).`
- `/stats` adds `rooms` (what it serves), per-room counters, `sess_room` (session → room), `mismatch` (room → count),
  and `name` (only if `RECEIVER_NAME` is set, see 4.3).

### 4.2 Compose (high)
```yaml
x-receiver: &receiver              # shared by every receiver
  image: python:3.13-alp-u3
  restart: unless-stopped
  depends_on: [console]
  user: *relay-user
  working_dir: /app
  command: ["python", "cef_to_json.py"]
  mem_limit: 256m
  pids_limit: 64
  volumes:
    - ./receiver:/app:ro
    - ./shared:/shared:ro
    - ./logs:/logs

x-receiver-env: &receiver-env
  UDP_LISTEN: 0.0.0.0
  UDP_PORT: 6510                   # inside the container; the host port is in `ports`
  CONSOLE_URL: http://console:8091
  PYTHONPATH: /shared
  PYTHONDONTWRITEBYTECODE: "1"

services:
  receiver-ops:
    <<: *receiver
    container_name: signal-receiver-ops
    environment:
      <<: *receiver-env
      ROOMS: OPS
      RECORD_LOG: /logs/ops-records.jsonl
      PROGRESS_FILE: /logs/ops-progress.json
    ports:
      - {target: 6510, published: "6510", host_ip: *listen-ip, protocol: udp}

  receiver-intel:
    <<: *receiver
    container_name: signal-receiver-intel
    environment:
      <<: *receiver-env
      ROOMS: INTEL
      RECORD_LOG: /logs/intel-records.jsonl
      PROGRESS_FILE: /logs/intel-progress.json
    ports:
      - {target: 6510, published: "6512", host_ip: *listen-ip, protocol: udp}
  # monitor-receiver (6511; accepts CHAT|MONITOR only), relay-assist, the console as today
```
- Adding a room: one diode rule, one line in `routes.conf`, one copied receiver block with a new port.
- Dev: one `receiver` with `ROOMS: OPS,INTEL,LOG` on 6510.

### 4.3 The console
- `/stats` from several receivers: kept **per receiver** (today one `stats` dict, overwritten by each). The
  page's status line sums them.
- Which receiver sent `/stats`: Docker's reverse DNS of the request's source address gives its container name
  (the same no-settings trick signal-monitor uses for subscribers). `RECEIVER_NAME` overrides, for a receiver
  whose name can't be looked up.
- Events: unchanged. A group in two rooms arrives twice and is shown once (existing `chat_key` check).
- Lost notices: "All" tab, now with the room in the text.

### 4.4 monitor-receiver
- Accepts only `CEF:0|CHAT|MONITOR|`; anything else is counted (`BAD_PACKET`) and ignored. No other change.

## 5. Monitoring

### 5.1 Parts
- Receivers: `high/receiver/<container name>` (was the single `high/receiver`). Old history is re-keyed to
  `high/receiver/signal-receiver` once (database backup first).
- No room routing table on the high side: receivers say which rooms they serve, subscribers say which rooms they
  send, and sessions tie the two together.

### 5.2 Checks
| Check | Shown on | Code |
|---|---|---|
| Room sent but no receiver serves it | the subscriber, WARN | `ROOM_UNSERVED` (new) |
| Messages sent to a room not arriving after 60 s | that room's receiver, DOWN | `CHAT_NOT_ARRIVING` (per room) |
| Not arrived after 15 min | that receiver, error event | `CHAT_LOST` (per room) |
| Packets for a room the receiver doesn't serve | that receiver, WARN | `ROOM_MISMATCH` (new) |
| Two hosts report the same subscriber name | both, WARN | `SUBSCRIBER_TWICE` (new) |
| Receiver silent | that receiver | `RECEIVER_DOWN` (per receiver) |

- The receiver part's chat clue (`traffic_at`, used to tell LEFT_SILENT from MONITOR_ONLY_SILENT) becomes
  "any receiver got chat packets".
- New codes get runbook entries and lines in the Analyst's knowledge text.

### 5.3 Diagram
```
 low side                    diode                     high side
 signal-servers
 signal-cli ── subscriber ═╦═▶ [ OPS     ● ] ──────▶ receiver-ops
            └─ subscriber-2╬═▶ [ INTEL   ● ] ──────▶ receiver-intel
                           ╚═▶ [ LOG     ● ] ──────▶ receiver-log
 signal-monitor ═════════════▶ [ MONITOR ● ] ──────▶ monitor-receiver ──▶ the console
```
- **Room tiles in the diode band**, one per room seen: state from sent vs arrived (green / amber mismatch /
  red not arriving / grey no traffic yet). The monitoring link tile becomes the `MONITOR` room tile.
- Subscribers wire into the room tiles they send (a bus when several), room tiles wire straight across to the
  receiver(s) serving them. Receivers stack in the receiver column, one row per room tile, so those wires stay
  straight.
- Receivers on more than one high-side host get host headings, mirroring the low side.
- Everything appears by itself: new host, signal-cli, subscriber, room or receiver shows on its first report.
- Size: scales to fit (down to 55%), then scrolls. About 6 to 8 boxes per column fit on a 1920 screen.

### 5.4 Removing boxes (the fix: today nothing ever removes one)
- **Low side, automatic:** when container-facts reports a container removed (or it is missing from
  signal-monitor's start summary), its boxes go: a subscriber's box; a signal-cli's box and its Signal servers box.
  An event says so: `Removed signal-subscriber-2 on <host> from the diagram: its container was removed.`
- **Anywhere, by hand:** a **Remove this box** button in the Log window when that box is chosen, only for a box
  DOWN or STALE for more than an hour (the console checks this too). In-page confirm. Event:
  `Removed receiver-log from the diagram (by admin).` Removing a host's signal-monitor removes the whole host.
- History stays (Log, timeline, incidents). A removed part that reports again simply comes back.
- `POST /api/monitor/forget` (logged-in users only; tokens stay GET-only).

## 6. Scenarios
1. Message in a group routed to OPS: the subscriber sends it on its OPS stream, the diode sends it to
   receiver-ops, which sees the next number (no gap) and hands it to my_forward and the console.
2. Two subscribers send different groups to OPS: receiver-ops sees two sessions, each gapless.
3. One group in OPS and INTEL: sent on both streams; each room gets it once; the console shows it once.
4. A packet lost on INTEL: only receiver-intel reports a gap, with a lost notice naming INTEL.
5. receiver-intel down: OPS and LOG keep flowing; INTEL's tile and receiver go red after 60 s; those messages
   are lost (one-way UDP), as today.
6. Diode sends OPS to receiver-intel: dropped there and counted; receiver-intel WARN `ROOM_MISMATCH`, OPS tile red.
7. A room nobody serves (diode rule missing, or receiver not deployed): `ROOM_UNSERVED` on the subscriber.
8. A subscriber restarts: new sessions for its rooms ("a sender started" per room); other subscribers unaffected.
9. signal-cli restarts: as today, per subscriber; any loss is reported against that subscriber's rooms.
10. Same group in the same room on two subscribers: the second one refuses to start.
11. The same subscriber started on two hosts: `SUBSCRIBER_TWICE`.
12. A subscriber removed from compose: its box goes when container-facts says so.

## 7. Switching over (prod)
The new headers only route once the diode has the new rules, so it goes in one step:
1. Diode: add `CHAT|<ROOM>` rules for every room and `CHAT|MONITOR`.
2. High side: new receivers, monitor-receiver, the console (`docker compose up -d` in `high/`).
3. Low side: `routes.conf`, subscribers, signal-monitor (`docker compose up -d` in `low/`).
4. Check the Monitor tab: every room tile green, no `ROOM_*` warnings.
5. Remove the old `J6-DTT|CHAT` diode rules.
Messages sent in the gap between 1 and 3 wait in signal-cli (up to 1000), as on any subscriber restart.

## 8. Build and test order (dev; agreed 2026-10-18)
Dev keeps working end to end after every step.
0. **Rename** playback to the Signal Chat Relay Console (section 9).
1. **Dynamic receivers:** stats per receiver, names from Docker DNS, a box each (`high/receiver/<name>`; old
   history re-keyed). No wire change.
2. **Removal fix** (5.4).
3. **Receiver:** `ROOMS`, the new header, room check, per-room counts. Accepts the old `J6-DTT` header for now,
   so dev keeps flowing until step 4.
4. **Subscriber:** `routes.conf`, start-up checks, a stream per room.
5. **Monitoring per room** (5.2) and room tiles (5.3). Then the prod-shape test: a throwaway UDP splitter routes
   by header to two receivers.
6. **`CHAT|MONITOR` header:** monitor-receiver first (accepts both for now), then signal-monitor.
7. **The page** (section 9): Monitor as main page, room windows with figures, dock, pulses, `CHAT_VIEW`.
8. **Clean-up:** drop the old-header acceptance; compose files, `high/docs/monitoring.md`, READMEs, CLAUDE.md, runbook,
   Analyst knowledge, diagrams 01, 06, 09 (and the reference doc's one "playback" mention, with its PDF).
9. **Prod switch-over** (the user's): section 7.

## 9. The console's page (user, 2026-10-18)
- **Name: Signal Chat Relay Console** (step 0). Was "playback": service `console`, container `signal-console`,
  folder `high/console`, files `console_web.py/.html`, `console_login.html`, `console_users.py`, settings
  `CONSOLE_URL` (receivers, monitor-receiver) and `CONSOLE_API` (relay-assist), part key `high/console`.
- **The Monitor diagram is the main page.** The chat tabs go.
- **Chat windows are for troubleshooting** (did messages get through the diode?), not for reading chats.
  - **One floating window per Signal group** (user, 2026-10-18), plus "All"; several open at once (the
    `floating()` helper: drag, resize, remembered place, last clicked on top). Not modals: a modal blocks the
    page and allows one.
  - On top of each window, that group's delivery figures, e.g.
    `via OPS · receiver-ops · 2 messages · nothing lost`, plus sent vs arrived and the last arrival time.
    Removed from the main page.
  - Sent per group: the subscriber's status adds a per-group count (sent, last sent time; group IDs only, never
    names or content). Arrived per group: counted by the console at intake.
  - A lost message's group is unknown (its packets never arrived), so a loss shows in every window of that
    room: `1 message lost in room OPS (group unknown)`.
  - A group sent to two rooms: its window shows both routes, each with its own figures.
  - Chat or terminal view per window. Phone: one window at a time, full screen.
- **Three ways to open a group's window:**
  1. a chat dock along the bottom: one chip per group with an unread badge;
  2. click a room tile in the diode band: a short list of its groups;
  3. live pulses: a dot runs along a room's wires when a message passes (no content); click the wire for its
     groups.
- **`CHAT_VIEW=on|off`** (console, settings block). `off`: the console never stores or shows message text
  (events are counted at intake, then dropped: no content in relay.db); the windows show the figures only.
  Monitoring works the same either way. Proposed default: `on` for dev, the prod value is the user's call.

## 10. Open points
- **High side across hosts:** receivers on a host without the console can't reach its ingest (8091, internal only).
  Proposal: build later, when needed: publish 8091 on the the console host's high-side address with a token.
  On k8s this goes away (service names).
- **Automatic removal on the high side:** none (no container facts there). Remove by hand, or also forget a
  box silent for 7 days? (default proposed: by hand only)
