# Relay monitoring

The relay's own monitoring: the health of every part, left and right, on playback's Monitor tab. It runs alongside
app-monitor, which watches the wider environment, and does not replace it.

- It crosses the data diode one way, like the chat, as CEF over UDP, on its **own port (6511)**. Chat stays on 6510.
- It never carries message content and never sends anything to Signal.
- Full design diagram: [`diagrams/09-monitoring-design.pdf`](diagrams/09-monitoring-design.pdf).

```
left host                                         │ diode │  right host
                                                  │       │
signal-cli ◀─ health check ─ signal-monitor ─UDP 6511────────▶ monitor-receiver ─┐
                                  ▲                          │                    ├─▶ playback ─▶ browser
subscriber ─ status every 10 s ───┘                          │  receiver ─────────┘   (Monitor tab, bell)
subscriber ─ chat ───────────────────────── UDP 6510 ──────────▶ (counters = heartbeat)    └─ data/relay.db
```

## Contents
- [The parts](#the-parts)
- [States](#states)
- [What you see on the page](#what-you-see-on-the-page)
- [Incidents and likely causes](#incidents-and-likely-causes)
- [What to expect when something stops](#what-to-expect-when-something-stops)
- [Left side: signal-monitor](#left-side-signal-monitor)
- [Right side: monitor-receiver and playback](#right-side-monitor-receiver-and-playback)
- [Messages on the wire](#messages-on-the-wire)
- [History, API and the later AI assistant](#history-api-and-the-later-ai-assistant)
- [Tokens for tools](#tokens-for-tools)
- [AI assistant (relay-assist)](#ai-assistant-relay-assist)
- [Testing](#testing)
- [Known limits](#known-limits)

## The parts
Every part has a key. The Monitor tab shows one tile per key, grouped by side, left host and signal-cli instance.

| Key | Who reports it | How often |
|---|---|---|
| `right/receiver` | the chat receiver's counters (`/stats`) | every 30 s |
| `right/monitor-receiver` | its own heartbeat (`rx_hb`) | every 30 s |
| `right/playback` | itself | always UP while the page works |
| `left/<host>/signal-monitor` | its heartbeat (`hb`) and every packet it sends | every 30 s |
| `left/<host>/<instance>/signal-cli` | signal-monitor's health check | every 30 s, and at once on a change |
| `left/<host>/<instance>/subscriber/<name>` | the subscriber's status, via signal-monitor | every 30 s, and at once on a change |

Plus one **diode link** tile per left host (see [below](#diode-link)).

Several signal-cli instances (several accounts) are supported. Each has an instance name (`relay-1`, `relay-2`…) and
shows as its own group of tiles.

## States
| State | Meaning |
|---|---|
| **UP** | working |
| **WARN** | working, but needs attention: certificate within 30 days of expiry, disk under 10% free, messages queuing, or starting up |
| **STALE** | nothing heard for 90 s |
| **DOWN** | the part reported itself down, or nothing heard for 3 min |
| **UNKNOWN** | its host's signal-monitor is silent, so nobody can tell (see below) |

**Why UNKNOWN and not DOWN?** Only signal-monitor reports on signal-cli and the subscribers. When it goes quiet, the
right side can't tell these apart:
- the whole left host is down;
- only signal-monitor is down, and signal-cli is still relaying;
- the diode is down, and the left side is fine.

So signal-monitor itself goes STALE and then DOWN, and the parts it reports on go UNKNOWN. Their tiles keep the last
values they reported (for example "stream connected"): the last known state, not a live one.

**The left side reports states; playback decides on changes.** Every status repeats the full state, and playback
turns each change into an event. A lost packet can't lose an alert: the next status (30 s later) carries it again.

**Start-up:**
- signal-monitor: in its first 60 s, "not answering yet" is WARN, not DOWN (signal-cli takes a while to start).
- playback: in its first 90 s, it doesn't mark anything STALE or DOWN for silence, since nothing has had time to
  report yet. A part that *reports* DOWN is shown at once.

## What you see on the page
- **Monitor tab** (always the last tab, far right). Its dot is red when anything is DOWN or UNKNOWN, amber for WARN
  or STALE.
  - **Incidents** (at the top): each open incident with its likely cause, the evidence, what it means and what to
    check; then the last 5 closed ones. See [Incidents and likely causes](#incidents-and-likely-causes).
  - **Tiles:** state, when last heard, why (if not UP), and a few figures (certificate days left, disk free, check
    time, sent, queue, errors…).
  - **Events log:** every change of state (including recoveries) and error lines from the subscribers, newest first,
    with a filter by severity. Kept 30 days.
- **Alert bell** (next to "Live", on every tab): only the problems open **right now** (DOWN, UNKNOWN, STALE, WARN),
  worst first, each with why and since when.
  - A part leaves the list as soon as it is UP again. The history is the Events log, not the bell.
  - The number is red if any is DOWN or UNKNOWN, amber otherwise. It is also in the browser tab's title, e.g.
    `(2) Signal Relay · All groups`. The bell swings once when a new problem appears.
  - Error lines ("5 errors in the last minute") are not in the bell: they have no "recovered". They are in Events.
  - At the top of the list: the likely cause of each open incident.
  - Clicking an alert opens the Monitor tab.
- There are no desktop pop-ups. The right side has no internet, so there is no push service either: alerts show only
  in an open page.

### Diode link
One tile per left host, from signal-monitor's numbered packets:
- **last packet:** how long ago the last monitoring packet arrived.
- **lost (1 h):** packets missing from the numbering, as a percentage.
- **delay (1 h):** average time from sent to received. It compares two hosts' clocks, so both need time sync.
- **bars:** one per hour for the last 24 h; grey = no packets that hour, amber = some lost, red = 5% or more lost.

Its state comes **first from the last packet**: STALE after 90 s with none, DOWN after 3 min ("no monitoring packets
for N min"). Only while packets arrive does the loss rate decide: under 1% UP, under 5% WARN, else DOWN. Loss is
counted from gaps in the numbering, so on its own it can't see a link carrying nothing. The numbering restarts when
signal-monitor restarts, so an outage shows as STALE/DOWN events, not as loss.

The link is not in the bell: signal-monitor's DOWN already reports the same silence.

## Incidents and likely causes
An **incident** is a period of trouble on one side: `right`, or one left host (with its signal-cli instances,
subscribers and diode link).
- It **opens** when a part there stops being UP. Exception: "starting up" alone opens one only if it lasts over
  90 s, so a normal restart leaves none.
- While open it collects every part that has a problem, and keeps its **likely cause** up to date (each change is
  logged).
- It **closes** once all of them have been UP for 60 s. Its duration runs to the recovery, not to the close.
- Opening, a new likely cause and closing each go in the Events log. Kept 30 days.

**The likely cause comes from fixed rules, not from AI** (`right/playback/incidents.py`, `diagnose()`). They use what
the parts report, their fixed codes (below), and one extra clue: whether chat packets are still arriving at the
receiver. Most specific and most serious first; the others are listed as "also".

| Cause | When |
|---|---|
| `MONRX_DOWN` | monitor-receiver is silent: the left side only *looks* silent |
| `LEFT_SILENT` | signal-monitor silent, and no chat since either: left host, its containers, or the diode |
| `MONITOR_ONLY_SILENT` | signal-monitor silent, but chat still arrives: signal-monitor or the 6511 flow |
| `CLI_UNREACHABLE`, `CLI_TIMEOUT`, `CLI_TLS_FAILED`, `CLI_HTTP_ERROR`, `CLI_DOWN` | signal-cli's health check fails, by how |
| `CLI_GATEWAY_CLOSED` | the gateway refuses signal-monitor's certificate (subscribers still connected = signal-cli is fine) |
| `SUB_STOPPED` | a subscriber sends no status |
| `SUB_GATEWAY_CLOSED`, `SUB_TLS_FAILED`, `SUB_UNREACHABLE`, `SUB_NOT_CONNECTED` | signal-cli is up, but the subscriber can't connect, by how |
| `RECEIVER_DOWN` | the chat receiver is silent |
| `DIODE_LOSS` | monitoring packets arrive, but some are missing |
| `STARTING`, `QUEUE_BACKLOG`, `MONITOR_DISK_LOW`, `CERT_EXPIRING` | the WARN states |
| `UNCLEAR` | no known pattern |

A subscriber that lost its stream because signal-cli is down is listed as evidence, not as a second cause.

**What each cause means and what to check** is in `right/playback/runbook.json`: a title, an explanation, and checks
with where to run them (left host, right host, build host, diode). Edit the texts freely; keep the keys. Nothing in it
runs by itself. After editing, recreate playback.

**Fixed codes from the left side:** next to its free text, every state carries a `code`:
- signal-cli: `OK`, `STARTING`, `CERT_EXPIRING`, `UNREACHABLE`, `GATEWAY_CLOSED`, `TLS_FAILED`, `TIMEOUT`, `HTTP_ERROR`.
- subscriber: `OK`, `STARTING`, `NOT_CONNECTED` (why in `info.stream_code`: `UNREACHABLE`, `GATEWAY_CLOSED`,
  `TLS_FAILED`, `HTTP_ERROR`, `TIMEOUT`, `STREAM_CLOSED`, `LOST`), `NO_STATUS`, `CERT_EXPIRING`, `QUEUE_BACKLOG`.
- signal-monitor's heartbeat: `OK`, `DISK_LOW`, `CERT_EXPIRING`.

## What to expect when something stops
Times are after the last packet or status.

**All left containers stopped** (or the left host, or the diode):

| After | signal-monitor | signal-cli, subscribers | diode link |
|---|---|---|---|
| 90 s | STALE | UNKNOWN | STALE |
| 3 min | DOWN | UNKNOWN | DOWN |

The bell shows signal-monitor and each UNKNOWN part. When the left side starts again, signal-monitor and the link go UP
on the first packet; signal-cli and the subscribers go UP within about 30 s (perhaps WARN "starting up" for up to a
minute first).

**Only the subscriber stopped:** DOWN after 35 s, "no status for 35 s (stopped, or can't reach signal-monitor)".

**Only signal-cli stopped:** DOWN at the next check (within 30 s), with the reason (for example "the gateway refused
the connection"). Its subscribers go DOWN "not connected to signal-cli" soon after.

**Only signal-monitor stopped:** the same as "all left stopped" above: the right side can't tell the difference.
Chat keeps flowing; the chat tabs still update.

**monitor-receiver stopped:** it goes STALE then DOWN; signal-monitor and the left parts also go STALE/UNKNOWN,
because nothing passes their packets on. Its own tile tells you where the break is.

**receiver stopped:** STALE then DOWN. Chat stops; monitoring is not affected (separate container and port).

## Left side: signal-monitor
`left/monitor/signal_monitor.py`, container `signal-monitor`, one per left host. Read-only, no capabilities, 128 MB.
- **signal-cli:** calls `/api/v1/check` through each instance's TLS gateway with its own client certificate (CN
  `signal-monitor`, which must be listed in signal-cli's `TLS_CLIENT_NAMES`), and reads the gateway certificate's
  expiry. Make the certificate with `../image/tlscerts client signal-monitor` (see `image/tlscerts.md`).
- **Subscribers:** each sends a status packet every 10 s (UDP 6520, this project's network only): connected or not,
  time since the last event, counts, queue, certificate days, and new error lines (at most 5 a minute are passed on).
- **This host:** free disk under `DISK_PATHS` (default `/certs`, the disk holding `left/`). Never mount `logs/`: it
  holds message content.
- **Every 30 s** it sends its heartbeat and each part's state, and at once when a state changes.

| Setting | On | Meaning |
|---|---|---|
| `MONITOR_HOST` | signal-monitor | this host's name on the Monitor tab |
| `SIGNAL_TARGETS` | signal-monitor | `instance=URL`, comma-separated: each signal-cli to check |
| `UDP_DEST`, `UDP_PORT` | signal-monitor | the diode's input for monitoring (port 6511) |
| `STATUS_PORT` | signal-monitor | where subscribers send their status (6520) |
| `DISK_PATHS`, `DISK_LOW_PCT` | signal-monitor | disks to check, and the WARN level (10%) |
| `CERT_WARN_DAYS` | signal-monitor | certificate expiry WARN (30 days) |
| `MONITOR_ADDR` | subscriber | `signal-monitor:6520`; empty = send no status |
| `RELAY_ID` | subscriber | which signal-cli instance it reads (a name in `SIGNAL_TARGETS`) |
| `MONITOR_NAME` | subscriber | its name on the Monitor tab |

- **Split setup** (signal-cli and subscriber on different hosts): run signal-monitor on the signal-cli host. A
  subscriber on another host sets `MONITOR_ADDR=<signal-cli host>:6520`, and the monitor publishes that port
  (`ports: ["6520:6520/udp"]`). Or run a second signal-monitor on the subscriber host with its own `MONITOR_HOST`.
- **A second signal-cli instance:** add `relay-2=https://<its gateway>` to `SIGNAL_TARGETS`, and give its
  subscribers `RELAY_ID=relay-2`.

## Right side: monitor-receiver and playback
- **monitor-receiver** (`right/monitor/monitor_receiver.py`, container `signal-monitor-receiver`): listens on UDP
  6511 (published `172.16.0.46:6511:6511/udp` on dev), checks each packet (ASCII, at most 1472 bytes, CEF name
  `SIGNAL-RELAY-MON`, one monitoring message), adds the arrival time and posts it to playback's `/monitor` (port 8091,
  this project's network only). Every 30 s it posts its own heartbeat. Separate from the chat receiver, so a fault
  or restart in one never touches the other.
- **playback** (`right/playback/monitor.py`): keeps every part's state, works out STALE/DOWN/UNKNOWN from silence,
  records each change as an event, counts the link's loss and delay, and pushes it all to open pages over the same
  websocket as the chat. Every 3 s it re-checks ages.
- The chat **receiver**'s counters (`/stats`) go every 30 s even when unchanged: they are its heartbeat.

## Messages on the wire
Each message is one CEF line in one UDP datagram (at most 1472 bytes, ASCII JSON), no splitting:
```
CEF:0|J6-DTT|CHAT|1|1|SIGNAL-RELAY-MON|MON|{"t":"st","host":"left-1","boot":"9f3a1c2e","seq":118,"ts":1792265000000,...}
```
| `t` | From | Carries |
|---|---|---|
| `hb` | signal-monitor | its heartbeat: `up_s`, `state`, `code`, `text`, `info` (disk, certificate, version, how many parts) |
| `st` | signal-monitor | one part's state: `inst`, `comp` (`signal-cli`/`subscriber`), `name`, `state` (`up`/`warn`/`down`), `code`, `text`, `info` |
| `ev` | signal-monitor | something that isn't a state (an error line, start-up): `code`, `sev`, `text` |
| `rx_hb` | monitor-receiver (right side only) | its own heartbeat and counters |

`boot` is random per start of signal-monitor and `seq` counts up; together they give the link's loss count. `ts` is
the send time, for the delay.

## History, API and the later AI assistant
- **Storage:** playback's SQLite database `right/data/relay.db` (with the chat history and users). Monitoring events,
  part states and link counts (5-minute buckets) are kept `RETENTION_DAYS` (30) and cleaned up hourly. They survive
  restarts.
- **Moving to k8s:** `store.py` is the only file with SQL; swap it for Postgres to run several playback replicas.
  Until then, one replica.
- **API** (a logged-in session or a [token](#tokens-for-tools); JSON, never message content):
  - `GET /api/monitor/state`: every part, the link summary and recent incidents.
  - `GET /api/monitor/events?since=<ms>&limit=200&part=<key>`: the event history.
  - `GET /api/monitor/incidents?state=open&since=<ms>&limit=50`: incidents, newest first (leave out `state` for all).
  - `GET /api/monitor/incidents/<id>`: one incident with its runbook entry and the events of its parts (from 5 min
    before it opened to its close).
  - `GET /api/monitor/runbook`: every likely cause, what it means and what to check.

  These are the hooks for the planned AI troubleshooting assistant.

## Tokens for tools
A tool (such as the AI assistant) uses a token instead of a login. A token can only **read** the monitoring API
(`GET /api/monitor/...`): not the page, the chat, the websocket or any POST. Tokens are stored hashed.
```
docker exec -it signal-playback python playback_users.py token add relay-assist   # shows the token once
docker exec -it signal-playback python playback_users.py token list               # with when each was last used
docker exec -it signal-playback python playback_users.py token remove relay-assist
```
Adding a token with a name that exists replaces it; the old one stops working at once. The tool sends it as
`Authorization: Bearer <token>`:
```
curl -sk -H "Authorization: Bearer $TOKEN" https://<host>.lab.int:8090/api/monitor/incidents?state=open
```

## AI assistant (relay-assist)
"Explain (AI)" on each incident (Monitor tab) asks an AI model to explain it. The answer appears under the incident
for everyone with the page open, with the model, the time taken, what it looked at, and whether it **agrees with
the rules'** likely cause. Rate it with Yes / No.

```
browser ─POST /api/assist/explain─▶ playback ─▶ relay-assist ─▶ model (OpenAI-compatible, tool calling)
                                        ▲              │ tools: read-only, with its token
                                        └── :8091 ─────┘ get_incident, get_state, get_events, get_runbook
```
- **Read-only:** its only tools read monitoring from playback's internal port 8091, with its own token. It never sees
  chat, can't run anything, and only suggests checks for a person to run.
- **Rules first:** the model is told the rules' cause up front, checks the evidence, and ends with a verdict: agree,
  or disagree with its own cause code.
- **Untrusted text:** tool results (event text from across the diode) are given as data; answers are shown as plain
  text.
- **Container** `signal-relay-assist` (`right/assist/relay_assist.py`): locked down like the others, no published port.
  At most two questions at once; up to 6 rounds of tool calls and 150 s per question. Two clicks on the same incident
  share one answer. The answer is saved even if the browser closes.

**Settings** (`relay-assist` in `right/docker-compose.yml`; secrets in `right/.env`, gitignored, mode 600):

| Setting | Meaning |
|---|---|
| `AI_URL` | the model's endpoint. Dev: `https://integrate.api.nvidia.com/v1`. Prod: your vLLM, e.g. `http://<vllm-host>:8000/v1` |
| `AI_MODEL` | the model ID (dev: `nvidia/nemotron-3-super-120b-a12b`) |
| `AI_KEY` | `${NVIDIA_API_KEY:-}` from `right/.env`. Prod Nemotron: none (leave empty); GPT-5.4: its key |
| `RELAY_TOKEN` | `${RELAY_ASSIST_TOKEN:-}` from `right/.env`: `playback_users.py token add relay-assist` |
| `AI_LOG` | `summary` (default) / `full` / `off`, see below |
| `AI_MAX_TOKENS` | per reply, default 4096 (reasoning models spend part of it thinking; 900 cut answers off) |

Playback's `ASSIST_URL` (`http://relay-assist:8092`) turns the button on; empty turns it off.

**Setting it up on a new host** (the right host, in `right/`, with playback running). Each playback has its own
database, so each host needs its own token; dev's doesn't work on prod.
1. Make the token and write it straight into `.env`. It never shows on screen. Any old line is removed first, so
   this also replaces a lost or leaked token (the old one stops working at once):
   ```
   sed -i '/^RELAY_ASSIST_TOKEN=/d' .env 2>/dev/null; echo "RELAY_ASSIST_TOKEN=$(docker exec signal-playback python playback_users.py token add relay-assist | sed -n 's/.*store it now): //p')" >> .env && chmod 600 .env
   ```
2. If the model needs a key (GPT-5.4; not prod's Nemotron), add it to `.env` as `NVIDIA_API_KEY=<key>`: that is the
   name the compose line `AI_KEY=${NVIDIA_API_KEY:-}` reads, whatever the model. No key: leave it out.
3. Start (or restart) relay-assist, and check it:
   ```
   docker compose up -d --force-recreate relay-assist
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

**Dev results (2026-10-17, Nemotron 3 Super on NVIDIA's free API):** 7 answers on 3 incidents, 14–48 s each. All
agreed with the rules, which were right each time. One was cut off before `AI_MAX_TOKENS` was raised; one suggested
a wrong service name before it was told to copy the runbook's commands exactly.

## Testing
Playback keeps everything in its database, so test against a throwaway playback (own container, spare ports,
scratch `DB_PATH`), not the live one. Post monitoring messages to its `/monitor` the way monitor-receiver does, e.g.:
```
{"t":"st","host":"left-1","boot":"b1","seq":2,"ts":<now ms>,"rx":<now ms>,
 "inst":"relay-1","comp":"signal-cli","name":"signal-cli","state":"down","text":"the gateway refused the connection"}
```
Browser checks: headless Chromium driven over the DevTools protocol (see `CLAUDE.md`, "Working here").

## Known limits
- **One client allowlist for all certificates:** the gateway allows the same calls for every client certificate, so
  signal-monitor's certificate could also open the message stream. Its code only calls `/api/v1/check`. Per-client
  allowlists need an image rebuild (`TlsGateway.java`).
- **Alerts only in an open page:** no e-mail, chat or push (the right side is air-gapped).
- **Clock-based delay:** needs time sync on both sides of the diode.
- **Silence is not loss:** packets that were never sent (left side down) don't count as lost; the outage shows as
  STALE/DOWN events instead.
