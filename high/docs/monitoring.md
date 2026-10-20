# Relay monitoring

The relay's own monitoring: the health of every part, low side and high side, on the console's page: a live
diagram of the whole relay. It runs alongside app-monitor, which watches the wider environment, and does not replace it.

- It crosses the data diode one way, like the chat, as CEF over UDP, on its **own route**: header
  `CEF:0|CHAT|MONITOR|`, which the diode sends to monitor-receiver (each chat room has its own, `CEF:0|CHAT|<ROOM>|`).
- It never carries message content and never sends anything to Signal.
- Full design diagram: [`diagrams/09-monitoring-design.pdf`](diagrams/09-monitoring-design.pdf); rooms:
  [`room-routing-design.md`](room-routing-design.md).

```
low-side host                                   │ diode │  high-side host
signal-cli ◀─ health check ─ signal-monitor ─ Monitoring ──────▶ monitor-receiver ─┐
                                  ▲                                                ├─▶ console ─▶ browser
subscriber ─ status every 10 s ───┘                                                │   (diagram, bell)
subscriber ─ chat, one route per room ─ Signal Relay ──▶ receiver-relay ───────────┤   └─ data/relay.db
                                     └─ Signal-API-SelfTest ──▶ receiver-selftest ─┘
                                        receiver ─my_forward─▶ Gotham Chat (per FORWARD_TO)
```
On the page a room is called by its Signal group(s) ("Signal Relay"); short names (RELAY) are only in the settings.

## Contents
- [The parts](#the-parts)
- [States](#states)
- [What you see on the page](#what-you-see-on-the-page)
- [Boxes and connections](#boxes-and-connections)
- [Incidents and likely causes](#incidents-and-likely-causes)
- [Application problems](#application-problems)
- [Signal servers (image r2)](#signal-servers-image-r2)
- [What to expect when something stops](#what-to-expect-when-something-stops)
- [Low side: signal-monitor](#left-side-signal-monitor)
- [High side: monitor-receiver and the console](#high-side-monitor-receiver-and-console)
- [Messages on the wire](#messages-on-the-wire)
- [History, API and the later AI assistant](#history-api-and-the-later-ai-assistant)
- [Tokens for tools](#tokens-for-tools)
- [AI assistant (relay-assist)](#ai-assistant-relay-assist)
- [Testing](#testing)
- [Known limits](#known-limits)

## The parts
Every part has a key. The diagram shows one box per key, grouped by side, low-side host and signal-cli instance.

| Key | Who reports it | How often |
|---|---|---|
| `high/receiver/<container>` | each receiver's counters (`/stats`), named by the console from Docker's DNS (`RECEIVER_NAME` overrides) | every 30 s |
| `high/monitor-receiver` | its own heartbeat (`rx_hb`) | every 30 s |
| `high/console` | itself: its database, intake, AI calls, blocked event loop, disk | every 3 s |
| `low/<host>/signal-monitor` | its heartbeat (`hb`) and every packet it sends | every 30 s |
| `low/<host>/<instance>/signal-cli` | signal-monitor's health check | every 30 s, and at once on a change |
| `low/<host>/<instance>/subscriber/<name>` | the subscriber's status, via signal-monitor | every 30 s, and at once on a change |
| `low/<host>/<instance>/signal-servers` | signal-cli's own link to Signal's servers, in its health check (image r2) | every 30 s, and at once on a change |

Plus, drawn from those (no keys of their own):
- one **Monitoring** marker per low-side host in the diode band: the diode link (see [below](#diode-link));
- one **room marker** per chat room in the diode band: its name and sent this run → arrived (no colour since
  2026-10-20: its two wires show the state);
- one **destination box** (e.g. "Gotham Chat") per receiver whose `FORWARD_TO` isn't off: did `my_forward` deliver.

**Names need no settings.** signal-monitor names everything by its real container name:
- each signal-cli in `SIGNAL_TARGETS` (just URLs): the container behind the URL's host (compose service `signal` →
  container `signal-cli`), from container-facts (so also while it is stopped), else Docker's DNS, else the host;
- each subscriber: the container its status packet came from (Docker's DNS);
- which signal-cli a subscriber reads: the one whose URL host matches the subscriber's `SIGNAL_URL`, else the only one.

Several signal-cli (several accounts) each show as their own group of boxes, "signal-cli on sighost",
"signal-cli-2 on sighost". `name=URL` in `SIGNAL_TARGETS` sets a name by hand. Only a subscriber on another host than its
signal-monitor needs `MONITOR_NAME` and `RELAY_ID` (Docker's DNS can't see across hosts). A name is fixed when
signal-monitor starts; if container-facts and DNS both fail then, it uses the URL's host and logs a warning.

**Words on the page (user, 2026-10-18):** **low side** = before the diode (the Signal side: signal-cli, subscribers,
signal-monitor; the `low/` folder); **high side** = after the diode (receiver, monitor-receiver, the console; the `high/`
folder). Hosts show as their real Docker host name, on both sides (signal-monitor and the console read the host's
`/etc/hostname`, mounted at `/host/hostname`; `MONITOR_HOST` / `HOST_NAME` override it). Rooms show by their Signal
group(s) (user, 2026-10-19), never as a CEF header or short name. Internal part keys (database, API) are `<side>/…`: `high/receiver/<container>`, `high/monitor-receiver`, `high/console`,
`low/<host>/signal-monitor`, `low/<host>/<signal-cli>/signal-cli` (and `/signal-servers`), `low/<host>/<signal-cli>/subscriber/<name>`;
incident scopes are `high` and `low/<host>`. The Analyst is told never to show them. Renaming a host or a signal-cli leaves its old parts in the console:
remove them with the console stopped (`DELETE FROM mon_parts WHERE key LIKE 'low/<old host>%'`), or they go STALE.

## States
| State | Meaning |
|---|---|
| **UP** | working |
| **WARN** | working, but needs attention: certificate within 30 days of expiry, disk under 10% free, messages queuing, starting up, a problem of its own in the last minute, or its container near its limits |
| **STALE** | nothing heard for 90 s |
| **DOWN** | the part reported itself down, or nothing heard for 3 min |
| **UNKNOWN** | its host's signal-monitor is silent, so nobody can tell (see below) |

**Why UNKNOWN and not DOWN?** Only signal-monitor reports on signal-cli and the subscribers. When it goes quiet, the
high side can't tell these apart:
- the whole low-side host is down;
- only signal-monitor is down, and signal-cli is still relaying;
- the diode is down, and the low side is fine.

So signal-monitor itself goes STALE and then DOWN, and the parts it reports on go UNKNOWN. Their boxes keep the last
values they reported (for example "stream connected"): the last known state, not a live one.

**The low side reports states; the console decides on changes.** Every status repeats the full state, and the console
turns each change into an event. A lost packet can't lose an alert: the next status (30 s later) carries it again.

**Start-up:**
- signal-monitor: in its first 60 s, "not answering yet" is WARN, not DOWN (signal-cli takes a while to start).
- the console: in its first 90 s, it doesn't mark anything STALE or DOWN for silence, since nothing has had time to
  report yet. A part that *reports* DOWN is shown at once.

## What you see on the page
The page is one live diagram. Each thing has one place: the **bell** says what is wrong now, the **drawer** has the
details of one incident, the **boxes** show every part, the **group windows** show each group's delivery, and the
**Log** window and the **timeline** are the history.
- **Bell** (next to "Live"): one row per **open incident**: where, its likely cause, how long it has been open, and
  the parts involved (or the AI's one-line headline, once it has answered). The number is the count of open
  incidents, red if a part is DOWN or UNKNOWN, amber otherwise; it is also in the browser tab's title. The bell
  swings once when a new incident opens. A row opens the drawer. At the bottom: **Incident history ›**.
- **Incident history** (in the drawer): every incident kept (30 days), newest first; 50 at a time, "Show more" for
  older ones. A row opens that incident; **‹ History** goes back to the list.
- **Incident drawer** (a floating window: drag it by its title bar, resize it from its corner; each browser
  remembers place and size; full width on a phone; the window clicked last is on top): the likely cause and its
  evidence, "now:" if the parts are already recovering, the AI's answer, whether the same cause happened in the 7
  days before, and, folded: what to check (the runbook's commands, with the incident's receiver filled in for
  `<receiver>` / `<receiver-service>`), what it means, and the timeline.
- **The diagram**, left to right as the data flows: Low side (light green) | the data diode (light magenta) | High
  side (light red), on one grid so connected boxes share a row and their wires run straight. Each chat room has its
  own row: subscriber → its **room marker** in the diode band (`Signal Relay  2→2`: sent this run → arrived) →
  its receiver → its destination box (e.g. **Gotham Chat**, the relay's real destination: slate, the boldest box,
  with **no state word**, only a slim bar on its left in the hand-off's state (user, 2026-10-20): we see nothing past the hand-off to my_forward, so the box
  must not look like Gotham Chat's own status; the wire into it shows whether the hand-off worked, the box only counts).
  signal-monitor's row: signal-monitor → the **Monitoring** marker (loss · delay over the last 5 min) → monitor-receiver.
  The console's box sits on top of the receivers; thin wires come up to it from below.
  - **Boxes:** state, when last heard, why (if not UP), how its container ended (e.g. `container exited (exit 137)`),
    "flapping", and a few figures. A part in an open incident has a `#n ›` chip (under its name) to its drawer. A
    subscriber's **rooms** button lists the rooms it sends and their groups; a group opens its window.
  - **Destination box** ("Gotham Chat", per receiver: its title is the receiver's `FORWARD_TO`, typed in its block
    in `high/docker-compose.yml`; none with `FORWARD_TO: "off"`): the latest hand-off to `my_forward`: UP when it
    returned without an error, DOWN when it raised or returned False, WARN when it took over 2 s, IDLE before the
    first, UNKNOWN when its receiver is silent. Green again at the next hand-off that works. Its counts survive a
    receiver restart. No small print (2026-10-20).
  - **Wires** show their link's own state, not the boxes at their ends (see [Boxes and connections](#boxes-and-connections));
    key under the high side's heading: green = flowing, amber = WARN/late, red dotted = DOWN, grey = unknown/idle.
    Hover a wire for its link, state and why. A room's wire and marker are clickable: the room's groups' windows.
- **Top bar KPIs** (2026-10-20): tiles for what is checked (boxes + wires), how many are OK (with a bar), the problems
  (count in the worst one's colour, the worst named, all of them in its tooltip; click: the open incidents) and the
  messages this run (sent → arrived, all rooms; amber with the count when any were lost). A red tile first when the
  monitor itself stopped checking.
- **Compact boxes** (2026-10-20): a healthy box is one line (name, state, its first two figures); a box with a problem
  (anything but UP or idle) opens by itself and stays open a minute after it is back UP. Click a box's header (▸/▾) to
  open or close it by hand: remembered in that browser, until clicked back to what the rule says. "boxes: open all ·
  close all · auto" under the line-style key does every box. A destination box opens with its receiver, so the two stay
  level. The subscriber column spans the free rows down to signal-monitor, so a new subscriber fills empty space first.
- **Tooltips** are the page's own (not the browser's): hover shows one after a moment, moving onto it keeps it, clicking it
  pins it (Escape or a click elsewhere closes it); keyboard focus shows it too; on a phone, a long press shows it pinned.
  - **Pulses:** a dot runs along a group's room wires whenever its message arrives.
  - It is drawn at full size and scaled to the window (down to 55%; on a phone the diagram box scrolls sideways).
  - Boxes appear by themselves on their first report (a new host, signal-cli, subscriber, room or receiver), and go
    when their container is removed (2 min later; tools/sim's `signal-sim-*` test boxes after 20 s, and their made-up
    groups get no window), or with **Remove this box** in their Log (for a box silent over an hour).
- **Group windows** (one per Signal group, plus "All"): for checking that a group's messages got through the diode,
  not for reading chats. On top: per room it goes through, `via Signal Relay → signal-receiver-relay · 3 sent → 3
  arrived ✓ · last arrived 14:02 UTC`, and its room's lost messages (a lost message's group is unknown). Opened from
  the **dock** at the bottom (a chip per group: delivery dot, unread count), a room marker or wire, a subscriber's
  rooms button, or `#g=<8 characters of the group ID>`. Several can be open; each browser remembers which. With
  `chat-view: off` they show the figures only.
- **Log** (the events: every change of state, container starts and stops, error lines, incidents opening and
  closing). It opens in a floating window from a lane of the **timeline** (that part) or the **Monitoring** marker
  (the diode link); its part list also has "all parts". In it: a level filter and a text search; newest first; `#n ›`
  chips open the incident; **Show older** loads earlier ones (the database keeps 30 days); **Remove this box** for a
  part that no longer exists.
- **Timeline** (under the diagram): one lane per box, the last 24 hours in 10-minute columns coloured by the worst
  state in each (blank: nothing recorded), with warnings and errors as dots (hover for the text). A lane opens its
  Log. `GET /api/monitor/timeline?hours=24` (from the minute figures, kept 48 h).
- **How to open an incident's drawer:** a bell row; a box's `#n ›` chip; **bell → Incident history ›**; a Log line's
  `#n ›` chip.
  - **Flapping guard:** a part that changes state more than 10 times in 10 minutes gets one "is flapping" event,
    and its changes are not logged one by one until it has been steady for 10 minutes ("settled at UP after N
    changes"). Its state, the bell and the incident still follow it.
- There are no desktop pop-ups. The high side has no internet, so there is no push service either: alerts show only
  in an open page.

### Diode link
One **Monitoring** marker per low-side host (its 24 h: the timeline's "diode link" lane), from signal-monitor's
numbered packets:
- **last packet:** how long ago the last monitoring packet arrived.
- **on the marker:** lost · delay over the **last 5 min** (since 2026-10-20; was the last hour). The marker has no
  colour: its two wires (signal-monitor → marker → monitor-receiver) show the link's state.
- **in its tooltip:** the last 5 min and the last hour (lost %, delay, packets). Delay compares two hosts' clocks, so
  both need time sync.
- **its 24 h:** the timeline's "diode link" lane, one bar per hour.

Its state comes **first from the last packet**: STALE (amber wire) after 90 s with none, DOWN after 3 min. Only while
packets arrive does the loss rate decide, over the last 5 min: WARN over 2% with at least 2 packets lost (one lost
packet alone is only a Log line), DOWN over 20%. A short drop is green again 5 min later; the hour's figure stays in
the tooltip and for the Analyst. Loss is
counted from gaps in the numbering, so on its own it can't see a link carrying nothing. The numbering restarts when
signal-monitor restarts, so an outage shows as STALE/DOWN events, not as loss.

The link is not in the bell: signal-monitor's DOWN already reports the same silence.

## Boxes and connections
**The header** (step 2) counts boxes and wires alike: `28 checked · 26 OK · 2 problems: Monitoring from ubt2 DOWN`
(the worst first). IDLE counts as OK. **Group chips and group windows** follow their route: the worst of the group's
own arrival check and its wires (subscriber → room, room → each receiver), so a group's dot is never green while its
route is red; a group whose subscriber is down shows that too.

Since 2026-10-20 (design: [monitoring-redesign.md](monitoring-redesign.md), step 1). **A box shows only its own
health; each link between parts has its own state**, worked out by the console (`monitor.connections()`, in
`/api/monitor/state` as `connections`), and the wires show those. Colours are "now": a problem of a part's own colours
it for 1 min after it last happened (was 10), a link's colour is its latest result.

| Link (wire) | DOWN when | WARN when | UNKNOWN when |
|---|---|---|---|
| Signal servers → signal-cli (keep-alive) | unreachable 2 min, unlinked, too old | reconnecting | signal-cli's check fails |
| signal-cli → subscriber (events) | the stream is down 20 s, signal-cli DOWN, the subscriber stopped | reconnecting (under 20 s), starting | the subscriber isn't reporting |
| subscriber → signal-monitor (status) | no status 35 s | | signal-monitor silent |
| signal-monitor → signal-cli (health check) | the check fails | starting, slower than 2 s | signal-monitor silent |
| subscriber → room (send) | its UDP sends fail now; no receiver serves the room (ROOM_UNSERVED) | | the subscriber is DOWN ("nothing can be sent") or not reporting |
| room → receiver (route) | messages sent 30 s ago not arrived (CHAT_NOT_ARRIVING) | | the receiver is silent or DOWN; its subscriber isn't running |
| receiver → destination | the latest hand-off failed (FORWARD_FAILED) | the latest was slow (FORWARD_SLOW) | the receiver is silent (IDLE: nothing yet) |
| receiver → console (HTTP 8091) | its latest delivery failed (DELIVER_FAILED) | | the receiver is silent |
| monitor-receiver → console | | messages waiting (MONRX_WAITING), the console refused some just now (MONRX_REFUSED) | monitor-receiver silent |
| signal-monitor → Monitoring → monitor-receiver | as before (silence, then loss) | | |

**Two witnesses before a box is blamed:**
- signal-monitor's check of signal-cli fails, but a subscriber still reads signal-cli: signal-cli is **UNKNOWN**
  (`MONITOR_CHECK_FAILED`: it runs, the check's path doesn't), the health-check wire DOWN. Both fail: signal-cli DOWN.
- A subscriber that runs but isn't connected to signal-cli: **WARN** (`NOT_CONNECTED`, in plain words: "can't
  connect to signal-cli (refused, ...)"), the events wire DOWN after 20 s. It was DOWN before 2026-10-20.
- No status from a subscriber while container-facts says it runs: **UNKNOWN** (`SUB_STATUS_LOST`), the status wire
  DOWN; its container stopped: DOWN, with how it ended.
- A paused container (docker pause): DOWN, `CONTAINER_PAUSED` ("frozen").

**What no longer colours a box** (counted and logged as before, shown on the link): the receiver's FORWARD_FAILED /
FORWARD_SLOW (destination link), DELIVER_FAILED / NOTICE_FAILED (console link), CHAT_NOT_ARRIVING (route), BAD_PACKET,
ROOM_MISMATCH and MESSAGE_LOST (what reached it); the subscriber's SEND_FAILED (send link), STREAM_STALLED,
EVENTS_MISSED and CLI_RESTART_LOSS (Log lines); signal-cli's GATEWAY_REFUSED and RPC_BLOCKED (the gateway doing its
job); the console's AI_FAILED (the Analyst's button gets a red dot instead).
Incidents take a link with a problem as a part (send, route, destination, console links), so its cause can point at it.
The Analyst starts on a subscriber that isn't connected for 60 s, as on a box DOWN for 60 s.

## Incidents and likely causes
An **incident** is one problem (since 2026-10-20, step 3 of [monitoring-redesign.md](monitoring-redesign.md); it was
one per side before). The rules run per side, then findings that share a part or a connection are one problem,
wherever they are: monitor-receiver down is **one** incident across the diode ("both sides"), while a subscriber
not connected and a failing Gotham hand-off are **two**. A box and its own wires count as one (a receiver and its
link to the console).
- It **opens**: at once when something is DOWN, UNKNOWN or STALE, or messages were lost; after **60 s** when it is
  WARN alone (a WARN that clears within a minute leaves only Log lines). "Starting up" alone: only after 90 s.
- A new problem **joins** the open incident that has one of its parts; otherwise it opens its own.
- It keeps the **most serious** cause it has seen: a milder one later (e.g. "starting up" while the parts come back)
  is shown as "now: …" instead of replacing it.
- It **closes** once none of its problems has been seen for 60 s. Its duration runs to the recovery, not the close.
- **Lost messages** (`MESSAGE_LOST`, `CHAT_LOST`, `EVENTS_MISSED`, `CLI_RESTART_LOSS`, `SEND_FAILED`,
  `FORWARD_FAILED`): a **loss incident** even if everything works again at once, or they join the open incident
  of that part. Its drawer lists each loss (when, how many, where). It closes a minute after the latest loss, and
  **stays in the bell** ("closed, not opened yet") until someone opens it.
- **Tests:** while a tools/sim test's marker container (`signal-sim-on-<test>-<epoch>`) exists, new incidents and
  Log lines carry `test` and show a dashed **test** chip (bell, drawer, history, Log). The Analyst's history leaves
  them out (it says how many) and they never count as "the same cause before".
- Opening, a new likely cause and closing each go in the Log. Kept 30 days.

**The likely cause comes from fixed rules, not from AI** (`high/console/incidents.py`, `diagnose()`). They use what
the parts report, their fixed codes (below), and one extra clue: whether chat packets are still arriving at the
receiver. Most specific and most serious first; the others are listed as "also".

| Cause | When |
|---|---|
| `MONRX_DOWN` | monitor-receiver is silent: the low side only *looks* silent |
| `LEFT_SILENT` | signal-monitor silent, and no chat since either: low-side host, its containers, or the diode |
| `MONITOR_ONLY_SILENT` | signal-monitor silent, but chat still arrives: signal-monitor or the monitoring route |
| `CLI_UNREACHABLE`, `CLI_TIMEOUT`, `CLI_TLS_FAILED`, `CLI_HTTP_ERROR`, `CLI_DOWN` | signal-cli's health check fails, by how |
| `CLI_GATEWAY_CLOSED` | the gateway refuses signal-monitor's certificate (subscribers still connected = signal-cli is fine) |
| `SUB_STOPPED` | a subscriber sends no status |
| `SUB_GATEWAY_CLOSED`, `SUB_TLS_FAILED`, `SUB_UNREACHABLE`, `SUB_NOT_CONNECTED` | signal-cli is up, but the subscriber can't connect, by how |
| `RECEIVER_DOWN` | the chat receiver is silent |
| `DIODE_LOSS` | monitoring packets arrive, but some are missing |
| `CONTAINER_OOM`, `CONTAINER_RESTARTING`, `CONTAINER_CRASHED`, `CONTAINER_STOPPED` | Docker says how the part's container ended: out of memory, restarting again and again, a crash (non-zero exit), or a stop (exit 0/143; 137 without OOM = killed) |
| `STARTING`, `QUEUE_BACKLOG`, `MONITOR_DISK_LOW`, `CERT_EXPIRING` | the WARN states |
| application codes (`SIGNAL_UNREACHABLE`, `EVENTS_MISSED`, `CHAT_NOT_ARRIVING`, `FORWARD_FAILED`, `DB_ERROR`, …) | see [Application problems](#application-problems): each app's own code is the cause |
| `CONTAINER_MEM_HIGH`, `CONTAINER_CPU_HIGH`, `CONTAINER_PIDS_HIGH` | a part's container is near its own limits |
| `OTHER` | an app logged errors that have no code yet (its log says what) |
| `UNCLEAR` | no known pattern |

A subscriber that lost its stream because signal-cli is down is listed as evidence, not as a second cause.

**What each cause means and what to check** is in `high/console/runbook.json`: a title, an explanation, and checks
with where to run them (low-side host, high-side host, build host, diode). Edit the texts freely; keep the keys. Nothing in it
runs by itself. After editing, recreate the console.

**Fixed codes from the low side:** next to its free text, every state carries a `code`:
- signal-cli: `OK`, `STARTING`, `CERT_EXPIRING`, `UNREACHABLE`, `GATEWAY_CLOSED`, `TLS_FAILED`, `TIMEOUT`, `HTTP_ERROR`,
  and its application codes (below).
- subscriber: `OK`, `STARTING`, `NOT_CONNECTED` (why in `info.stream_code`: `UNREACHABLE`, `GATEWAY_CLOSED`,
  `TLS_FAILED`, `HTTP_ERROR`, `TIMEOUT`, `STREAM_CLOSED`, `STALLED`, `LOST`), `NO_STATUS`, `CERT_EXPIRING`,
  `QUEUE_BACKLOG`, and its application codes (below).
- signal-monitor's heartbeat: `OK`, `DISK_LOW`, `CERT_EXPIRING`, `FACTS_UNAVAILABLE`.

## Application problems
Built 2026-10-18 after a test showed that signal-cli could lose Signal's servers while every box stayed UP. Every
app is watched from the inside, in three layers:
1. **Specific checks** for the failures we know.
2. **A catch-all:** every warning and error each app logs is counted by a fixed code; anything without its own code
   is `OTHER`. Only codes and counts leave an app, never log text (it can hold pieces of a message).
3. **Liveness:** each app's workers report when they last did something, so a hung worker shows even when it logs
   nothing.

Counts are cumulative since the app started (`p` on its state), so a lost packet loses nothing. A counted problem of
the part's own keeps it WARN or DOWN for 1 minute after it last happened (colour = now; 10 minutes before
2026-10-20); problems on a link or with what arrived colour the link or only log (see [Boxes and
connections](#boxes-and-connections)). Each further one is one more event ("1 more"). `active` lists everything that applies now, worst first; the box shows the rest as "also:"
and the totals since start as "since start: …".

**Counting every hop.** signal-cli numbers its events `<its start time>-<n>`, n going up by one with no gaps; each
subscriber numbers what it sends; the receiver records, per sender, how far it got. So losses are counted exactly:

| Hop | How | Code |
|---|---|---|
| signal-cli → subscriber | a jump in signal-cli's event numbers | `EVENTS_MISSED` (how many) |
| signal-cli restarted while a subscriber was away | signal-cli's start time (`boot` in its check, read at every reconnect; else the new start time in the event IDs), against when the subscriber lost touch (over 20 s) | `CLI_RESTART_LOSS` (with the time window: what signal-cli held in memory then is lost) |
| subscriber → diode | UDP send errors | `SEND_FAILED` |
| across the diode | the subscriber reports "sent up to #57 at 02:10"; the receiver's last from that run is lower 60 s later. Monitoring still arrives, so it's that room's route through the diode or its receiver (on that receiver, per room) | `CHAT_NOT_ARRIVING`, after 15 min `CHAT_LOST` |
| within the diode | incomplete messages after 30 s; packets the kernel dropped (buffer full) | `MESSAGE_LOST`, `RX_OVERFLOW` |
| receiver → destination | `my_forward` raising (prod: the Gotham hand-off), delivery to the console failing | `FORWARD_FAILED`, `DELIVER_FAILED` |

**Per app** (the runbook has what each means and what to check):

| App | DOWN | WARN |
|---|---|---|
| signal-cli (r2) | `RECEIVE_STALLED` (its receive loop hasn't run for 3 min), `STORAGE_ERROR` | `JVM_MEMORY` (over 90% after GC), `EVENT_DROPPED`, `DECRYPT_FAILED`, `UNTRUSTED_IDENTITY`, `HANDLE_ERROR`, `GATEWAY_FULL`, `GATEWAY_REFUSED`, `RPC_BLOCKED`, `OTHER` |
| Signal servers (r2) | `SIGNAL_UNREACHABLE` (no answered keep-alive for 2 min), `DEVICE_UNLINKED`, `CLIENT_EXPIRED` | `SIGNAL_RECONNECTING`, `SIGNAL_FLAPPING` (over 5 drops an hour), `CLOCK_SKEW`, `SIGNAL_UNKNOWN` |
| subscriber | `SENDER_DEAD`, `SENDER_STALLED` (messages wait, nothing sent for 60 s), `SEND_FAILED` | `EVENTS_MISSED`, `CLI_RESTART_LOSS`, `MESSAGE_SKIPPED`, `SENDER_ERROR`, `WRITE_FAILED` (disk full or permissions), `STREAM_STALLED` (nothing for 45 s, not even the 15 s keep-alives; it reconnects), `QUEUE_SLOW` (oldest waiting over 5 min), `PACKET_TOO_BIG`, `OTHER` |
| receiver | `RX_STALLED` (its loop hasn't come round for 20 s, and where: a watchdog thread reports it), `CHAT_NOT_ARRIVING`, `FORWARD_FAILED`, `DELIVER_FAILED` | `MESSAGE_LOST`, `MESSAGE_BROKEN`, `RX_OVERFLOW`, `FORWARD_SLOW` (`my_forward` over 2 s), `BAD_PACKET`, `NOTICE_FAILED`, `DISK_LOW`, `OTHER` |
| the console | `DB_ERROR`, `CHECKS_FAILING` (a monitoring check keeps failing) | `INGEST_FAILED`, `AI_FAILED`, `LOOP_LAG` (blocked over 2 s), `DISK_LOW` |

**Who watches the console?** The console is the monitor, so if it breaks nothing else can say so; the browser can. Every
round of checks sends the page a heartbeat; without one for 30 s the status line turns red: "⚠ monitor last checked
N s ago, so this may be out of date". A full disk doesn't freeze it: monitoring writes are best-effort (counted as
`DB_ERROR`) and it carries on in memory, while chat writes still fail visibly (the receiver's `DELIVER_FAILED`).

## Signal servers (image r2)
signal-cli's own link to Signal's servers, the "Signal servers" box under each instance. From
`signal-cli:0.14.9-r2`, the launcher (`image/launcher/SignalHealth.java`) listens to signal-cli's log events inside
its own Java process and the gateway's `/api/v1/check` answers with them (a JSON body; signal-cli's own check is still
asked first). Shown: connected since, last keep-alive answered (Signal's servers are asked every 30 s), round trip,
reconnects in the last hour, last failure (usually `CONNECT_FAILED`: Signal's library tries several routes and then
reports only "all connect attempts failed", without the lower reason; `DEREGISTERED` and `EXPIRED` come through as
such). The reason as signal-cli saw it is in `docker logs signal-cli` ("Signal servers: connection failed …"). signal-cli also gets: its
receive loop's last run, envelopes received, Java memory and GC, gateway connections, and its problems by code.

- signal-cli only connects Signal's network library to its logging with `--verbose`; the launcher connects it itself
  and keeps those lines off the console, so the Docker log looks as before plus a few plain-English
  "Signal servers: connected / lost the connection, reconnecting..." lines.
- **What to expect** (measured on dev 2026-10-18, outgoing traffic of signal-cli's container blocked): drop noticed
  in 10 s (WARN "reconnecting"), DOWN `SIGNAL_UNREACHABLE` 2 min later, the subscribers still UP (signal-cli runs, it
  just gets nothing new). New messages wait on Signal's servers: nothing is lost by waiting. Reconnected 4 s after the
  block was lifted (and in 39 s after a longer block: signal-cli's retries back off to about 51 s).
- **A broken DNS doesn't cut signal-cli off:** with every Signal name in its container's hosts file pointing to a
  dead address, it still connected within a second (tested 2026-10-18). Signal's library does its own lookups and has
  fallback addresses. Only a network-level block (firewall, proxy, no route) stops it.
- The health watcher matches signal-cli's log events: after a signal-cli update, check the box says connected (or
  `docker logs signal-cli | grep "Signal servers"`). If it matches nothing, the box says `SIGNAL_UNKNOWN`, never a
  false DOWN. Before r2 there is no Signal servers box.

## What to expect when something stops
Times are after the last packet or status.

**All left containers stopped** (or the low-side host, or the diode):

| After | signal-monitor | signal-cli, subscribers | diode link |
|---|---|---|---|
| 90 s | STALE | UNKNOWN | STALE |
| 3 min | DOWN | UNKNOWN | DOWN |

The bell shows signal-monitor and each UNKNOWN part. When the low side starts again, signal-monitor and the link go UP
on the first packet; signal-cli and the subscribers go UP within about 30 s (perhaps WARN "starting up" for up to a
minute first).

**Only the subscriber stopped:** DOWN after 35 s, "no status for 35 s (stopped, or can't reach signal-monitor)".

**Only signal-cli stopped:** DOWN at the next check (within 30 s), with the reason (for example "the gateway refused
the connection"). Its subscribers go DOWN "not connected to signal-cli" soon after.

**Only signal-monitor stopped:** the same as "all of the low side stopped" above: the high side can't tell the difference.
Chat keeps flowing; the group windows still update.

**monitor-receiver stopped:** it goes STALE then DOWN; signal-monitor and the left parts also go STALE/UNKNOWN,
because nothing passes their packets on. Its own box tells you where the break is.

**receiver stopped:** STALE then DOWN. Chat stops; monitoring is not affected (separate container and port).

## Low side: signal-monitor
`low/monitor/signal_monitor.py`, container `signal-monitor`, one per low-side host. Read-only, no capabilities, 128 MB.
- **signal-cli:** calls `/api/v1/check` through each instance's TLS gateway with its own client certificate (CN
  `signal-monitor`, which must be listed in `low/gateway/clients.conf`), and reads the gateway certificate's
  expiry. Make the certificate with `../image/tlscerts client signal-monitor` (see `high/docs/tlscerts.md`).
- **Subscribers:** each sends a status packet every 10 s (UDP 6520, this project's network only): connected or not,
  counts, queue, certificate days, its workers' ages, problems by code, events missed and its numbering (see
  [Application problems](#application-problems)). signal-monitor turns them into a state; no log text is passed on.
- **This host:** free disk under `DISK_PATHS` (default `/certs`, the disk holding `low/`). Never mount `logs/`: it
  holds message content.
- **Every 30 s** it sends its heartbeat and each part's state, and at once when a state changes.

| Setting | On | Meaning |
|---|---|---|
| `MONITOR_HOST` | signal-monitor | this host's name on the diagram (default: the Docker host's own name) |
| `SIGNAL_TARGETS` | signal-monitor | each signal-cli's URL, comma-separated (named by container name; `name=URL` by hand) |
| `UDP_DEST`, `UDP_PORT` | signal-monitor | the diode's input for monitoring (settings block `diode-in`, `monitor-port`; the header `CEF:0|CHAT|MONITOR|` routes it) |
| `STATUS_PORT` | signal-monitor | where subscribers send their status (6520) |
| `DISK_PATHS`, `DISK_LOW_PCT` | signal-monitor | disks to check, and the WARN level (10%) |
| `CERT_WARN_DAYS` | signal-monitor | certificate expiry WARN (30 days) |
| `MONITOR_ADDR` | subscriber | `signal-monitor:6520`; empty = send no status |
| `RELAY_ID` | subscriber | only on another host: its signal-cli's name, as on the diagram |
| `MONITOR_NAME` | subscriber | only on another host: its name on the diagram (else its container name) |

- **Split setup** (signal-cli and subscriber on different hosts): run signal-monitor on the signal-cli host. A
  subscriber on another host sets `MONITOR_ADDR=<signal-cli host>:6520`, and the monitor publishes that port
  (`ports: ["6520:6520/udp"]`). Or run a second signal-monitor on the subscriber host with its own `MONITOR_HOST`.
- **Container facts** (`container-facts`, `low/facts/container_facts.py`): signal-monitor reads, every 15 s, each
  container of the `signal-low` project: state, exit code, out-of-memory flag, restarts, start and stop times. It
  sends **only news** across the diode, as `cf` messages (see [Messages on the wire](#messages-on-the-wire)): nothing
  while nothing changes. container-facts itself never sends anything; it only answers signal-monitor. It holds the
  Docker socket (read-only mount; the socket is still powerful) and answers only `GET /facts` with those fields:
  never another Docker call, never environment variables (which can hold keys), never other apps' containers.
  `group_add` must be the socket's group: `stat -c %g /var/run/docker.sock` (dev: 988; prod may differ). Off:
  comment out the service and empty `FACTS_URL`. Matching to parts: a subscriber reports its container ID (its host
  name) and is matched by that; otherwise by name. Each container's compose `service` is in the facts too: that is how
  signal-monitor names a signal-cli by its container, even while it is stopped.
  It also reads each running container's memory, CPU and processes as a share of **its own limits** (one-shot
  stats). News is only a change: memory or processes over 85%, CPU over 90% for 5 minutes, back to normal, or a
  different image. Its part goes WARN (`CONTAINER_MEM_HIGH`, …) while it lasts. The figures cross the diode only in
  that news, so on a quiet day the boxes don't show them. Every relay container has a memory and process limit
  (subscriber and receiver 256 MB, the console 512 MB); without a limit there is nothing to compare with.
- **A second signal-cli:** add its URL to `SIGNAL_TARGETS` (`https://signal:8443,https://signal-2:8443`) and point
  its subscribers' `SIGNAL_URL` at it. It shows by its container name; nothing else to set.

## High side: monitor-receiver and the console
- **monitor-receiver** (`high/monitor/monitor_receiver.py`, container `signal-monitor-receiver`): listens on UDP
  6511 (dev: reached by diode-sim, which routes `CHAT|MONITOR` to it; prod: publish the port the diode's route
  points at, on `listen-ip`), checks each packet (ASCII, at most 1472 bytes, header `CEF:0|CHAT|MONITOR|`, CEF name
  `SIGNAL-RELAY-MON`, one monitoring message), adds the arrival time and posts it to the console's `/monitor` (port 8091,
  this project's network only). Every 30 s it posts its own heartbeat. Separate from the chat receiver, so a fault
  or restart in one never touches the other.
- **the console** (`high/console/monitor.py`): keeps every part's state, works out STALE/DOWN/UNKNOWN from silence,
  records each change as an event, counts the link's loss and delay, and pushes it all to open pages over the same
  websocket as the chat. Every 3 s it re-checks ages.
- The chat **receiver**'s counters (`/stats`) go every 30 s even when unchanged: they are its heartbeat.

monitor-receiver holds messages it can't pass on (the console restarting or stopped) in order, up to 1000, and passes
them on when the console is back, so a console restart loses nothing and doesn't show as loss or delay in the diode.
Receiving and passing on are separate threads, so each packet's arrival time (`rx`) is stamped the moment it lands.
Its box counts `waiting` and `not delivered` (dropped past 1000, or refused by the console).

## Messages on the wire
Each message is one CEF line in one UDP datagram (at most 1472 bytes, ASCII JSON), no splitting:
```
CEF:0|CHAT|MONITOR|1|1|SIGNAL-RELAY-MON|MON|{"t":"st","host":"sighost","boot":"9f3a1c2e","seq":118,"ts":1792265000000,...}
```
| `t` | From | Carries |
|---|---|---|
| `hb` | signal-monitor | its heartbeat: `up_s`, `state`, `code`, `text`, `info` (disk, certificate, version, how many parts) |
| `st` | signal-monitor | one part's state: `inst`, `comp` (`signal-cli`/`signal-servers`/`subscriber`), `name`, `state` (`up`/`warn`/`down`), `code`, `text`, `info`, `active` (every code that applies now), `p` (problem counts), `flow` (a subscriber's run, last number sent and when, events read, a loss window) |
| `ev` | signal-monitor | something that isn't a state (start-up): `code`, `sev`, `text` |
| `cf` | signal-monitor | **container facts, only news:** one container that stopped, crashed, was restarted by Docker, came back, appeared, was removed (`"s":"removed"`), crossed or left a resource limit, or changed image: `c: [{n, s, x, o, r, st, fi, h, img, m, c, pi, b}]` (`m`/`c`/`pi` = memory/CPU/processes %, `b` = what is high). Each sent 3 times (at once, +10 s, +30 s); newer news about a container drops the unsent copies of older news, and the console ignores a copy whose start/end times are older than what it has, so a late copy can't undo a change. Once at signal-monitor's start: `start: true`, `run: {name: restarts}` for the running ones and `c` for the others |
| `rx_hb` | monitor-receiver (high side only) | its own heartbeat and counters |

The heartbeat also carries `cfh`: an 8-character fingerprint of every container's name, state and restart count, as
signal-monitor knows them. The console computes the same; if they differ, it missed a change (all 3 copies), and marks
that host's container facts "may be out of date" on the boxes and for the AI until the next change or restart.

**Volume** (one low-side host): a heartbeat and one state per part (signal-cli, Signal servers, each subscriber) every
30 s, each under 1 KB: about 8 datagrams a minute, about 3–4 MB a day. A container change adds 3 × about 250 bytes.
No error lines, no logs, no chat: problems travel as codes and counts in the states.

`boot` is random per start of signal-monitor and `seq` counts up; together they give the link's loss count. `ts` is
the send time, for the delay.

## History, API and the later AI assistant
- **Storage:** the console's SQLite database `high/data/relay.db` (with the chat history and users). Monitoring events,
  part states and link counts (5-minute buckets) are kept `RETENTION_DAYS` (30) and cleaned up hourly. They survive
  restarts.
- **Moving to k8s:** `store.py` is the only file with SQL; swap it for Postgres to run several the console replicas.
  Until then, one replica.
- **API** (a logged-in session or a [token](#tokens-for-tools); JSON, never message content):
  - `GET /api/monitor/state`: every part, the link summary and recent incidents.
  - `GET /api/monitor/events?since=<ms>&limit=200&part=<key>`: the event history.
  - `GET /api/monitor/incidents?state=open&since=<ms>&limit=50`: incidents, newest first (leave out `state` for all).
  - `GET /api/monitor/incidents/<id>`: one incident with its runbook entry and the events of its parts (from 5 min
    before it opened to its close).
  - `GET /api/monitor/runbook`: every likely cause, what it means and what to check.
  - `GET /api/monitor/history?code=<cause>&part=<key>&days=30`: past incidents (how often, hour, weekday, minutes).
  - `GET /api/monitor/metrics?part=<key>&minutes=60`: a part's state and figures once a minute (kept 48 h);
    `link/<host>` for the diode link.
  - `GET /api/monitor/chat-activity?hours=24`: messages per group per hour and the 7-day average (counts and group
    names only).

  These are the hooks for the planned AI troubleshooting assistant.

## Tokens for tools
A tool (such as the AI assistant) uses a token instead of a login. A token can only **read** the monitoring API
(`GET /api/monitor/...`): not the page, the chat, the websocket or any POST. Tokens are stored hashed.
```
docker exec -it signal-console python console_users.py token add relay-assist   # shows the token once
docker exec -it signal-console python console_users.py token list               # with when each was last used
docker exec -it signal-console python console_users.py token remove relay-assist
```
Adding a token with a name that exists replaces it; the old one stops working at once. The tool sends it as
`Authorization: Bearer <token>`:
```
curl -sk -H "Authorization: Bearer $TOKEN" https://<host>.lab.int:8090/api/monitor/incidents?state=open
```

## AI assistant (relay-assist)

On the page it has one name, **the Analyst** ("Ask Analyst", "Analyst: …", "the Analyst fails"); the service
is relay-assist.
The rules already name the likely cause and the runbook the standard checks, so the AI does what they can't: it
tells the story, judges the **impact on chat** and the **risk**, spots what is **unusual**, picks **one first
step**, asks the operator **the question** only a person can answer, and writes a **handover line** for the shift
log. Its answer is in the incident's drawer (headline, verdict, confidence, impact, risk, unusual, first step,
question; story, handover with Copy, and what it looked at folded).

```
console ─▶ relay-assist ─▶ model (OpenAI-compatible, tool calling)
   ▲             │ tools: read-only, with its token, on console's :8091
   └─────────────┘ get_incident, get_state, get_outages, get_delivery,
                   get_setup, get_events, get_history, get_metrics,
                   get_analysis, get_chat_activity, get_runbook
```
- **Tools return worked-out answers** (step 4 of [monitoring-redesign.md](monitoring-redesign.md), 2026-10-20), so
  the model copies figures instead of adding up rows. Tests (tools/sim) are left out everywhere and counted.
  - `get_state`: boxes, and every connection with a problem in full (the working ones by name), rooms, the
    Monitoring route (last 5 min and last hour), open incidents.
  - `get_outages(what, days)`: periods a box was DOWN/UNKNOWN/STALE or a connection DOWN, from the Log's state
    changes: per item how many, minutes, availability %; the longest and the latest. A removed box's outage ends
    at its removal.
  - `get_delivery(days)`: per day per room, group and receiver: sent, arrived, lost, never arrived, handed on,
    failed (the console's daily count table, kv `mon_daily`, 31 days, counts only; counting started 2026-10-20).
  - `get_setup()`: hosts, containers (image, started, restarts), rooms, receivers and destinations, certificate
    days, settings. Nothing secret.
  - `get_events`: also a UTC range (`from_utc`, `to_utc`) and a code or prefix (`LINK_DOWN`, `SUBSCRIBER_`).
  - `get_history`: every incident when no filter is given, `sort` newest/longest/oldest, per cause, and
    `pattern`/`patterns` worked out in code (the same cause at least 3 times, over more than 24 h). The model may
    mention a pattern only when it is set.
  - `get_metrics`: also the container's memory/CPU/processes % of its limits, and a `summary` (min, max, avg, last,
    minutes per state).
  - `get_analysis(kind)` (user, 2026-10-20: "tools for complex analytics"): `trend` (incidents, incident minutes,
    sent, arrived, lost, never arrived, forwarded, forward failed, per day or hour, with the peak and rising/falling),
    `compare` (these days vs the same number before), `around` (everything within N minutes of a time, and which
    part had a problem first: correlation), `reliability` (per cause or part: count, mean time to recover, mean
    time between, longest), `busiest` (incidents per hour of day and weekday).
  - Connections' state changes are Log lines too since 2026-10-20 (`LINK_DOWN`, `LINK_UP`, …; not into or out of
    UNKNOWN), so a cut link is in the history.
- **What it gets up front:** the first message already carries get_state (and get_incident for an incident), so it
  doesn't spend its first rounds fetching them (recorded in the entry's tools as `prefetched`).
- **Answers are cleaned in code** before they are saved: part keys become names ("signal-cli on ubt2"), cause codes
  the runbook's titles, tool names are dropped.
- **How-to tab (2026-10-20):** Ask Analyst has two tabs: *The relay now* (the questions above) and *How-to*, answered
  only from the relay's documents in `high/docs/` (mounted read-only at `/docs` in relay-assist, `DOCS_DIR`): how it's
  built and why, its rules, the risks assessed, how to do things. Its one tool, `get_docs`, searches the documents'
  sections (a word search ranked like a search engine: no internet or embeddings needed) or reads one section; the
  question's own search results are fetched first, so it usually answers in one or two rounds (dev: 7-14 s). Each
  answer ends with "From:" and the sections it used; if the documents don't cover something it says so. Its
  conversation and its Clear are its own (log kind `howto`). The relay tab's tools include `get_docs` too (12 tools),
  so explanations and wrap-ups can use the architecture and risks. **New documents:** put a `.md` file in `high/docs/`:
  searchable at the next question; its `##` headings are the sections it can point to.
- **Ask answers have a shape:** a bold headline (the answer itself), 3-10 points with the numbers and UTC times, "Why:"
  (how the events connect and the likely cause), "Compared with normal:" (from get_analysis/get_history), a
  small table for lists, "Not sure:" and "Next:" only when there, and "Based on (n)" folded; each answer shows its
  time. Older answers ({answer, facts}) still show as before.
- **Settings** (high settings block): `ai-timeout` / `ai-total` (seconds per model call / per question; dev 90/300),
  `ai-reasoning` (off | low | high: Nemotron thinking off/on, GPT-5.x reasoning effort; "" = the model's default),
  `ai-auto-per-hour` / `ai-auto-per-incident` (dev 20 / 3; 0 = no limit, for prod), `ai-ask-url` / `ai-ask-model`
  (+ `AI_ASK_KEY` in `.env`): a second model for Ask Analyst only.
- **Comparing models** (6.3 of the design): `docker exec signal-relay-assist python relay_assist.py --compare >
  compare.txt` asks 15 typical questions to the main model (and the Ask model, if set), or
  `--models 'URL|MODEL|KEYVAR,URL|MODEL|KEYVAR'`, through the real loop and tools; per question and model: seconds,
  rounds, tools used, errors, the headline and points, and whether the figure the data gives appears in the answer.
  Then 13 **How-to** questions (`--howto`: only those), each checked in code against the documents: the right
  section among its sources, the key facts in the answer (e.g. `--no-deps`, `1472`, `CEF:0|CHAT|`), no dev values in a
  prod answer (`172.16.0.46`, `ubt2`), a follow-up understood (multi-turn), and two questions the documents don't cover
  answered as such. Dev, Nemotron Ultra, 2026-10-20: 16 s per question, sources right 11/11, facts 10/11, gaps 2/2.
  How-to answers are for **prod** unless the question says dev; dev-only parts come last, marked "On dev:".
- **When:** the console asks **by itself**, once per likely cause and at most `ai-auto-per-incident` times per
  incident (`ai-auto-per-hour` an hour in all; dev 3 and 20, 0 = no limit), when it can add something: an unclear cause, several problems at once, a container crash, out of memory or
  restarts, diode loss, a repeat of an earlier incident, flapping, or a part DOWN/UNKNOWN for a minute. Not for
  WARN-only known causes (starting up, certificate, disk, queue): those have "Ask anyway". "Ask again" in the drawer
  asks once more; an answer about an earlier likely cause is marked as such.
- **Questions (the Ask box):** in every incident's drawer, "Ask about this incident" (e.g. "Were any messages lost?
  Has this happened before?"); and for the relay as a whole, **Ask Analyst** (the button right of the bell, shown
  next to the bell). The answer comes with what it was based on (folded), what the data can't tell
  ("Can't tell"), and one check to settle it ("Next"), and can be rated. Kept in the same log (kind `ask`).
  - **Multi-turn:** each question carries the last 4 questions and answers shown in the same box, so a follow-up
    ("why?", "is it still?") works. The page sends only their ids; the console reads them back from the log
    (`store.ai_asks`, same kind and incident), so a browser can't put words in the AI's mouth. The AI is told to check
    the data again rather than trust its earlier answer. Such answers are marked "follow-up" and log `follows: [ids]`.
  - The AI has 6 rounds of tool calls (`MAX_ROUNDS`, all kinds); the last round allows no tools, so it answers from
    what it found (and says what it couldn't check) instead of failing with "no answer after 6 rounds".
  - **The Ask Analyst window** floats like the drawer: drag it by its title bar, resize it from its bottom-right corner, close it
    with ✕, Esc or the button again. It stays open while you open incidents and group windows. Each browser remembers its place and size (kept inside the screen; on a phone it fills the width).
  - **While the Analyst works** (a question, an explain card, a wrap-up), the box shows pulsing dots, what it is
    doing, the seconds so far, and two shimmering placeholder lines (still when the browser asks for reduced motion).
    The console announces the start and end to every open page (ops `ai_wait` / `ai_done`), so it also shows for the
    automatic explain and wrap-up and for someone else's question.
  - **Clear** deletes a box's questions and answers from the log, for everyone, and starts a new conversation: in
    Ask Analyst's title bar (every question about the relay as a whole), and at the top of each incident's Ask box
    (that incident's questions only; its explain cards and wrap-ups stay). It asks first in an in-page box (Cancel
    has the focus; Esc cancels); can't be undone. `POST /api/assist/clear {"incident": id|null}` (login only, not
    tokens); every open page empties that box (op `ai_clear`). All popups on the page are these in-page boxes, never
    the browser's own.
- **Wrap-up when an incident closes:** written by itself when the incident mattered (the AI was asked while it was
  open, it lasted 2 min or more, or messages may have been lost), within the same 20-an-hour limit: what happened,
  when, impact, lost or not and why, the cause, one follow-up, and a handover line (Copy). A closed incident's drawer
  shows it first, with the answer from while it was open folded below; "Write the wrap-up" / "Write again" by hand.
- **What it knows:** how the relay works, written into its instructions: signal-cli keeps the last 1000 messages in
  memory, so a stopped subscriber's messages wait there and are lost if signal-cli restarts first; what exit codes
  mean; why the low side shows UNKNOWN; that nothing can go back across the diode.
- **What it reads:** the incident; every part's state and container facts; past incidents with the same cause or
  part (30 days: how often, hour, weekday, how long); each part's figures once a minute (`GET
  /api/monitor/metrics`, kept 48 h); messages per group per hour (`GET /api/monitor/chat-activity`: counts and
  group names only, never content), to estimate how many messages are waiting or at risk.
- **Read-only:** its tools only read monitoring, from the console's internal port 8091, with its own token. It never sees
  chat content, can't run anything, and only suggests steps for a person.
- **Rules first:** the model is told the rules' cause up front, and gives a verdict: agree, or disagree with its own
  cause code.
- **Untrusted text:** tool results (text from across the diode) are given as data; answers are shown as plain text.
- **Container** `signal-relay-assist` (`high/assist/relay_assist.py`): locked down like the others, no published port.
  At most two questions at once; up to 6 rounds of tool calls and 300 s per question (`AI_TOTAL_S`; each model call 90 s, `AI_TIMEOUT`, tried twice if it stalls). Two requests for the same
  incident share one answer, which is saved even if the browser closes.

**Settings** (`relay-assist` in `high/docker-compose.yml`; secrets in `high/.env`, gitignored, mode 600):

| Setting | Meaning |
|---|---|
| `AI_URL` | the model's endpoint. Dev: `https://integrate.api.nvidia.com/v1`. Prod: your vLLM, e.g. `http://<vllm-host>:8000/v1` |
| `AI_MODEL` | the model ID (dev: `nvidia/nemotron-3-super-120b-a12b`) |
| `AI_KEY` | `AI_KEY` from `high/.env` (the old name `NVIDIA_API_KEY` is still read). Prod Nemotron: none (leave empty); GPT-5.4: its key |
| `RELAY_TOKEN` | `${RELAY_ASSIST_TOKEN:-}` from `high/.env`: `console_users.py token add relay-assist` |
| `AI_LOG` | `summary` (default) / `full` / `off`, see below |
| `AI_MAX_TOKENS` | per reply, default 4096 (reasoning models spend part of it thinking; 900 cut answers off) |

The console's `ASSIST_URL` (`http://relay-assist:8092`) turns the button on; empty turns it off.

**Setting it up on a new host** (the high-side host, in `high/`, with the console running). Each the console has its own
database, so each host needs its own token; dev's doesn't work on prod.
1. Make the token and write it straight into `.env`. It never shows on screen. Any old line is removed first, so
   this also replaces a lost or leaked token (the old one stops working at once):
   ```
   sed -i '/^RELAY_ASSIST_TOKEN=/d' .env 2>/dev/null; echo "RELAY_ASSIST_TOKEN=$(docker exec signal-console python console_users.py token add relay-assist | sed -n 's/.*store it now): //p')" >> .env && chmod 600 .env
   ```
2. If the model needs a key (GPT-5.4; not prod's Nemotron), add it to `.env` as `AI_KEY=<key>`, whatever the model
   (an older `.env` with `NVIDIA_API_KEY=` still works). No key: leave it out.
3. Start (or restart) relay-assist, and check it:
   ```
   docker compose up -d relay-assist
   docker logs signal-relay-assist 2>&1 | tail -1
   ```
   It should say "Answering on port 8092 …". Without a token it says AI_URL, AI_MODEL and RELAY_TOKEN must be set.

**The question log** (in `relay.db`, 30 days): who asked, when, which incident, the model, the time, each tool call
with a one-line result, the answer, the verdict, the rating, and any error. Never keys or chat.
- `AI_LOG=summary` (prod): the exact prompt (what the model received, about 7–10 KB) is held for 24 h, then dropped,
  **unless** the answer was rated not helpful, disagreed with the rules, or failed (error, timeout, invalid tool
  call, cut off): those keep it for the 30 days.
- `AI_LOG=full` (dev): always keeps the prompt. `AI_LOG=off`: keeps nothing.
- `GET /api/assist/log?incident=<id>` (logged in): the answers, newest first, with `detail` (`full` or `summary`).

**Dev results (2026-10-17, Nemotron 3 Super on NVIDIA's free API):** with the first, "explain" instructions, 7
answers on 3 incidents only restated the rules. With the current ones, a real `docker stop` of the subscriber got,
by itself, 43 s after the incident opened: the container was killed after the 10 s stop grace (exit 137, not out
of memory; the subscriber now stops cleanly with exit 0); only one group affected, at about 0.01 messages an hour, so likely none waiting; the risk if signal-cli
restarts first; something unusual the rules missed (signal-monitor had restarted a minute earlier); and the
question "was the stop intentional?".

## Testing
The console keeps everything in its database, so test against a throwaway console (own container, spare ports,
scratch `DB_PATH`), not the live one.

**Cutting signal-cli off from Signal's servers** (dev only): `tools/signal-block start [drop|reject]`, then
`tools/signal-block stop` (`status` in between). A helper container joins signal-cli's own network namespace (never
the host's) and blocks everything to the internet there; local addresses keep working, so the subscriber and
signal-monitor stay connected. `drop`: packets vanish (a dead network); `reject`: refused at once (a connection
error). Messages sent meanwhile wait on Signal's servers. Tested 2026-10-19 (reject): blocked 02:21:04, Signal
servers WARN (reconnecting) at 02:21:32, DOWN (SIGNAL_UNREACHABLE) at 02:23:38 with an incident, unblocked 02:24:00,
connected again at 02:24:07; signal-cli and the subscriber stayed UP throughout; no rules left behind.

**The test rig used for the application checks (2026-10-18):** a throwaway compose project on its own network: a fake
signal-cli (the same event stream, event IDs, RPC and check, with a control endpoint for faults: gaps, restarts,
down, hang, health bodies), the real subscriber, signal-monitor, monitor-receiver, receiver, the console and
container-facts. Faults caused there: gaps, restarts with and without loss, a run that dropped events, a hung stream
(45 s), a full disk (tmpfs), a bad UDP destination, a chat port nobody listens on, incomplete and garbage packets,
`my_forward` failing and stuck (a named pipe as the records log), the console down, the console's database on a full disk,
a memory squeeze, container-facts stopped, every r2 health case, and the page with the console paused. In-process checks
cover a dead or failing sender, the catch-all, a check that keeps failing, a blocked loop and the 15-minute
`CHAT_LOST`. The real Signal-servers test ran on dev against signal-cli itself (its container's outgoing traffic
blocked in its own network namespace, not on the host).

Post monitoring messages to a throwaway console's `/monitor` the way monitor-receiver does, e.g.:
```
{"t":"st","host":"sighost","boot":"b1","seq":2,"ts":<now ms>,"rx":<now ms>,
 "inst":"signal-cli","comp":"signal-cli","name":"signal-cli","state":"down","text":"the gateway refused the connection"}
```
Browser checks: headless Chromium driven over the DevTools protocol (see `CLAUDE.md`, "Working here").

## Known limits
- **Alerts only in an open page:** no e-mail, chat or push (the high side is air-gapped).
- **Clock-based delay:** needs time sync on both sides of the diode.
- **Silence is not loss:** packets that were never sent (low side down) don't count as lost; the outage shows as
  STALE/DOWN events instead.
- **The chat-path check and receiver restarts:** each receiver keeps, per sender, how far it got in its progress
  file (`high/logs/<room>-progress.json`, no content), so after a restart messages sent while it was down are still
  checked, and show as not arriving, then lost (UDP sent to a stopped receiver is lost). Without that file (e.g. a new
  host), messages sent before its start are not checked.
- **What signal-cli never hears of can't be seen:** if Signal's servers quietly stop delivering to this account (no
  error, no drop), every check stays green. Only a test message sent from another account would show it.
- **The Signal servers box depends on signal-cli's log events** (see [above](#signal-servers-image-r2)).
- **"Delivered" to Gotham means my_forward returned without an error.** The relay can't see past it: if the sending
  call reports a failure by its return value without raising (or returning False), the destination box stays green.
- **"This run":** a room marker's and a group window's sent counts are the subscriber's since it last started;
  arrivals are matched to that run's sessions, so they always compare like with like.
- **A room's name** comes from the groups a running subscriber reports sending to it; with none, the page falls
  back to its short name.
