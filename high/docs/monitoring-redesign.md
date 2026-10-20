# Monitoring redesign: boxes, connections, incidents, the Analyst, the tests

Status: **approved** (user, 2026-10-20; decisions in section 10). Nothing built yet.
Why: a review after the first sim test runs (2026-10-19) found that the monitor has no idea of a *connection*.
Every finding about a link is put on a box, and the wires and markers copy the boxes. So the wrong part gets the
blame (test 4: the subscriber shown DOWN while only its link to signal-cli was cut), and the Analyst repeats it.

## 1. What went wrong (the findings)

**A. The wrong part gets the blame**
1. Subscriber DOWN whenever its stream to signal-cli isn't connected, even for a one-second reconnect.
2. Subscriber DOWN "stopped" when its status packets don't arrive, even while container-facts says it runs.
3. Receiver DOWN when messages are lost on the way to it (CHAT_NOT_ARRIVING); its text admits "the chat port or
   the receiver".
4. Receiver WARN for 10 min after junk or wrong-room packets: the receiver works, what reached it is wrong.
5. Receiver DOWN when my_forward fails (the Gotham hand-off), as well as the Gotham box.
6. Receiver DOWN when its posts to the console fail (the receiver → console connection).
7. Console WARN when the Analyst fails (relay-assist, its own container, has no box).
8. 8 of the 10 wires copy a box's state; only Signal servers → signal-cli and the Monitoring route show the link.

**B. Wrong when nothing is wrong**
9. Rooms and group chips go IDLE "no messages yet this run" when their subscriber is down (it is left out).
10. The Monitoring marker's state comes from loss over the last hour: a 3-minute gap keeps it WARN/DOWN, and a
    DIODE_LOSS incident open, for up to an hour.
11. The header says "all well" while a room marker or the Monitoring marker is DOWN: it counts boxes only.

**C. Incidents and the Analyst**
12. One incident per side: unrelated problems on the high side share one incident (#40: a junk packet, then a
    wrong-room packet, "likely cause is now ...", open 19 min); a fault across the diode splits into two.
13. An incident opens at the first WARN: blips of 6-12 s opened and closed incidents (#1, #12, #28, #30).
14. Test incidents count as real history: "fifth SUB_UNREACHABLE today, recurring", "a pattern at night", "the
    longest outage: 74 min LEFT_SILENT" all came from incidents opened during sim test runs.
15. Rules that are only in the Analyst's instructions don't hold ("within one day is not a pattern": it said one).
16. The Analyst copies box states ("the subscriber reported DOWN") even when the rules had the cause right.
17. Ask answers: one dry paragraph; raw codes and keys (LEFT_SILENT, low/ubt2, STREAM_DROP); tool names given to
    the person as the next step ("get_metrics"); a "Can't tell" line on every answer, some of it wrong ("the data
    does not show whether any messages were lost": it does).

**D. Smaller**
18. Raw error text on the page (`ssl:<ssl.SSLContext object at 0x...>`).
19. monitor-receiver never shows a problem (it counts posts the console didn't take, but stays UP).
20. "Gotham Chat UP" only means my_forward returned without an error.

## 2. Principles
- **A box shows only its own health:** is it running, stuck, crashing, short of memory, failing at its own job.
- **A connection has its own state,** worked out from what both ends say. Wires and markers show connections.
- **Two witnesses before blaming a box:** if signal-monitor's check of signal-cli fails but the subscriber's stream
  to it works, signal-cli is fine and the check's path is not. If both fail, it is signal-cli.
- **Recent, not hourly:** states use the last few minutes; longer figures are numbers on the marker, not its colour.
- **One place decides, everyone reads it:** the console works out boxes, connections and incidents; the page, the
  header, the bell and the Analyst all read the same result. Nothing important lives only in a prompt.
- **Tests are labelled,** never mistaken for real history.

## 3. Boxes: what each one's state means

| Box | UP | WARN | DOWN | UNKNOWN |
|---|---|---|---|---|
| signal-cli | running, answers | its own counted problems, Java memory, certificate | container stopped, crashed, frozen (paused), or no answer to anyone (check AND stream fail) | signal-monitor silent; or only the check fails while a subscriber still reads it (then the health-check connection is DOWN, not the box) |
| Signal servers | connected | reconnecting, flapping, clock | unreachable for 2 min, unlinked, too old | signal-cli not answering |
| subscriber | running, connected, sending | **running but not connected to signal-cli** (was DOWN), queue, its own counted problems | container stopped or crashed, sender dead or stuck | its status isn't arriving but container-facts says it runs (the status connection is DOWN) |
| signal-monitor | as now | as now | silent | — |
| receiver | running, receiving | its own counted problems only (RX_OVERFLOW, MESSAGE_BROKEN, OTHER, disk) | stuck loop (RX_STALLED), silent | — |
| Gotham box | delivered | slow | my_forward failing | receiver silent |
| monitor-receiver | as now | **posts the console didn't take, or messages waiting** (new) | silent | — |
| console | as now, without AI_FAILED | INGEST_FAILED, LOOP_LAG, disk | DB_ERROR, CHECKS_FAILING | — |

Moved off boxes, onto connections: NOT_CONNECTED, NO_STATUS (while running), CHAT_NOT_ARRIVING, BAD_PACKET,
ROOM_MISMATCH, MESSAGE_LOST, PACKETS_MISSING, FORWARD_FAILED/SLOW (to the Gotham connection), DELIVER_FAILED.
AI_FAILED goes to the Analyst button (a red dot, "the Analyst is not answering"), not the console box.
The Gotham box: its title is each receiver's `FORWARD_TO`, keyed in by hand in `high/docker-compose.yml` (already so);
its small-print description goes (user, 2026-10-20); the rest stays as it is.

## 4. Connections (new)
The console keeps one entry per connection, in `state()["connections"]`, each
`{id, from, to, state, code, text, since, figures}`. They are worked out from data that already crosses the diode,
except one small addition (see 4.2).

| Connection (wire) | State from | DOWN when | WARN when |
|---|---|---|---|
| Signal servers → signal-cli | the Signal servers part (as now) | unreachable | reconnecting |
| signal-cli → subscriber (events) | the subscriber's stream status and code | not connected for 20 s (or at once if signal-cli is DOWN) | a reconnect in the last 2 min, a stalled stream |
| subscriber → signal-monitor (status) | age of the last status + container-facts | no status 35 s while the container runs | — |
| signal-monitor → signal-cli (health check) | the check result only | check fails while the subscriber still reads signal-cli | check slow (> 2 s) |
| subscriber → room → receiver (chat route, both wire segments and the room marker) | sent vs arrived per room, the receivers' loss and junk counts | messages sent > 60 s ago not arrived (CHAT_NOT_ARRIVING); no receiver serves it; its subscriber is down ("nothing can be sent") | packets missing, messages lost, junk or wrong-room packets in the last 10 min |
| receiver → Gotham | my_forward results | failed in the last 10 min | slow |
| receiver → console, monitor-receiver → console (HTTP 8091) | posts failed / waiting | failing now | some failed in the last 10 min |
| signal-monitor → Monitoring → monitor-receiver | last packet age + loss **in the last 10 min** | silent 3 min, or > 20% lost in 10 min | silent 90 s, or > 2% lost in 10 min |

The Monitoring marker still shows the hour's figure ("2.7% lost (1 h)") as a number.

### 4.1 Room markers and group chips
**Markers keep their figures, the wires carry the state (user, 2026-10-20).** A room marker shows the room's name
and its counts this run, sent → arrived (`Signal Relay   5→5`): the only per-room count on the diagram. The
Monitoring marker keeps loss · delay. Neither has a colour or a ✓/!/✕ any more: the wire into a room marker
(sending fails, no receiver serves the room) and the wire out of it (messages not arriving, per receiver) already say
it, and say which receiver when a room has two. The route's text ("nothing can be sent: signal-subscriber is DOWN",
"messages not arriving") goes in the tooltip of its wires and in the Log. Group chips and group windows take their
route's state. (Correction: "no status" at first was taken to mean no counts either; the user meant the Gotham box.)

### 4.2 What changes on the low side
- signal-monitor reports the subscriber's own health as its state (not connected = WARN, not DOWN) and adds the
  stream's state as its own small field, `link: {"events": ["down", "UNREACHABLE", <since>]}`, in the same `st`
  message (a few bytes; stays one datagram).
- The subscriber's error text becomes plain words per code ("connection refused", "certificate refused", "timed
  out"); the raw exception stays in its Docker log only.

### 4.3 The header
Counts boxes, connections and markers: "14 checked · 13 OK · 1 problem: chat route Signal Relay not arriving".

## 4a. Timing: colour = now (user, 2026-10-20)
The colour shows the current truth within one report (10-30 s). What went wrong earlier is in the Log, the bell,
the incident and the timeline, never in the colour. ("For technical people: no red that is really green, and the
other way round.")

| What | Before | Now |
|---|---|---|
| A one-off problem (junk packet, wrong-room packet, a lost message, a failed or slow send that worked next time) | WARN/DOWN for 10 min after it | **no colour change**: a Log line (and see 5.1) |
| Gotham link | red 10 min after the last failure | red while the latest send fails, green on the next one that works |
| Slow sends | amber 10 min | amber while sends are slow, green on the next quick one |
| Monitoring marker / route (loss) | loss over the last hour | silence as now; loss over the last 5 min |
| Messages not arriving | after 60 s | after 30 s |
| A part gone silent | STALE 90 s, DOWN 3 min | unchanged (3 missed reports) |
| Signal servers | WARN at once (reconnecting), DOWN after 2 min | unchanged (WARN is the truth while it retries) |

A problem that keeps happening (sends failing again and again, loss every minute) stays red or amber while it goes
on. The 10-min `Recent` windows (signal-monitor and the console) stay only as counts for the Log, the tiles'
"since start" line and the Analyst, not for states.
Recovery itself can take time (signal-cli waited about 90 s between retries in the 2026-10-19 tests): the colour is
right meanwhile.

## 5. Incidents
- **One incident per problem, not per side.** Findings are worked out over the whole relay at once (so a fault
  across the diode is one incident). A new problem joins an open incident only if the rules tie it to that
  incident's parts or connections; otherwise it opens its own. A junk packet and a wrong-room packet: two incidents.
- **A short wait for WARN:** WARN alone opens an incident after 60 s; DOWN, UNKNOWN and anything with message loss
  open one at once.
- **Connections take part:** a connection is an incident's part like a box (`conn/<id>`), so causes such as
  "the subscriber can't reach signal-cli" point at the link.

### 5.1 What opens an incident
- **A state that isn't UP now** (a box or a connection): DOWN or UNKNOWN at once, WARN after 60 s.
- **Messages lost** (MESSAGE_LOST, CHAT_LOST, EVENTS_MISSED, CLI_RESTART_LOSS, SEND_FAILED, FORWARD_FAILED that
  didn't go out): a **loss incident** even if everything works again at once: how many, which room or group, when,
  where on the way. It closes by itself once nothing is wrong (so it is in the history and the Analyst's
  get_delivery), and the bell keeps it until someone has opened it.
- **Not an incident:** one lost monitoring packet (the route only turns amber when loss goes on: over 2% in 5 min),
  one junk or wrong-room packet, one slow send. A Log line only.

### 5.2 Tests are labelled
- Every sim test (one host too) runs its marker container `signal-sim-on-<test>-<epoch>`; container-facts carries it.
- While a marker exists, new incidents and events get `"test": "<test name>"`. The bell, the drawer and the history
  show a "test" chip. The timeline shades test periods.
- The Analyst's history tool leaves test incidents out (it says how many it left out).
- Sim artefacts stay on the diagram (user, 2026-10-20): room `SIMTEST` (reserved, refused in routes.conf) and the
  boxes of `signal-sim-*` containers show as now, with the "test" label on their incidents.

## 6. The Analyst
**Input:** reads connections as well as boxes (get_state), so it stops repeating box blame. Its knowledge text
explains boxes vs connections and the two-witness rule.

**History and patterns are worked out in code:** get_history returns real incidents only, with
`pattern: null | {count, first, last, hours}` computed by the rule (same cause, at least 3, over more than 24 h).
The instructions say: mention a pattern only if `pattern` is set.

**Words are cleaned in code, not only asked for:** before an answer is saved, relay-assist replaces part keys with
their names ("signal-cli on ubt2"), cause codes with the runbook's title ("the subscriber can't reach signal-cli"),
and drops tool names. The "next" field must be a runbook command for a person, or empty.

### 6.1 Tools for a wider range of questions
Today: get_state, get_incident, get_events (last N minutes), get_history (incidents, 30 days), get_metrics (one
part, per minute, 48 h), get_chat_activity (per group per hour), get_runbook. Questions they answer badly:
"longest outage", "uptime this week", "were any messages lost", "did everything reach Gotham", "assess container
X", "what is set up". The model then adds up rows itself, and gets it wrong or answers vaguely.
Rule for every tool: **it returns the worked-out answer (totals, durations, longest, per day), not raw rows to add
up**, and marks or leaves out tests.

New (3):
- **get_outages(what, days)**: the periods a box, connection or route was DOWN/UNKNOWN (start, end, length,
  test or not), per item and in total: availability %, the longest, how many. From the state-change events.
  For: longest outage, uptime, how often.
- **get_delivery(days)**: per route and per group, per day: sent, arrived, lost (and where), handed to Gotham,
  failed. For: were messages lost, the rate, did everything go out. Needs a small daily count table in relay.db
  (counts only, kept 30 days): today only counters since each app's start and loss events exist.
- **get_setup()**: what the relay is: hosts, containers (image, started, restarts), rooms → groups → receivers →
  destination, certificate days left, the settings that change behaviour (chat-view, chat-text, forward-to,
  retention). Nothing secret. For: how is it set up, what changed, which version.

Extended (3):
- **get_events**: a time range (from, to, UTC) and a code filter, not only "the last N minutes".
- **get_metrics**: the container's memory/CPU/processes % as well, and a summary (min, max, average, last) so the
  model doesn't read 2,880 rows.
- **get_history**: every incident when no filter is given, sortable by length, tests marked, `pattern` worked out
  (section 6).

Kept at 10 tools: more makes a mid-size model pick worse (Nemotron Super scored 10/10 on tool calls with 7).
Metrics are kept 48 h; the daily counts (get_delivery) give the longer view.

**Ask answers get a shape** (all fields optional except `headline`):
```
{"headline": "one line: the answer itself",
 "points":   ["2-6 short points with the numbers and UTC times"],
 "table":    {"columns": [...], "rows": [[...], ...]},     // for lists: incidents, rates per group, per host
 "unsure":   "only when it changes the answer",
 "next":     "<host>: one command from the runbook"}
```
The page shows the headline in bold, the points as a list, the table as a small table, "Not sure:" and "Next:"
only when present, and "Based on (n)" folded as now. All built with textContent (the model's text is untrusted).
Example, "What was the longest outage?":
```
Longest outage (last 30 days, tests left out): 12 min, signal-cli lost Signal's servers
  · 2026-10-14 03:10-03:22 UTC, on ubt2
  · new messages waited on Signal's servers: none lost
  · 3 tests left out (the longest 74 min, 2026-10-19)
```
(Numbers in the example are made up.)

### 6.2 Speed and limits (user, 2026-10-20)
- **When it starts:** as now (a box DOWN/UNKNOWN for 60 s, or a cause worth asking at once; well-known WARNs only
  by "Ask anyway"). A subscriber that runs but is not connected for 60 s counts like DOWN here.
- **No limit in prod:** the automatic limits become settings in the high settings block: `ai-auto-per-hour` (dev 20:
  the free tier; prod `0` = no limit) and `ai-auto-per-incident` (dev 3; prod `0` = no limit). Kept everywhere:
  the same cause is not asked twice in one incident (the same question gives the same answer).
- **Faster answers** (what this side controls; the model's own speed is the model's):
  - The first message already carries the incident and the current state, so the model doesn't spend a round
    fetching them (today: 1-2 of its rounds).
  - The tools of 6.1 return worked-out answers: fewer rounds.
  - Timeouts become settings: `ai-timeout` per model call and `ai-total` per question (dev 90 s / 300 s for the
    slow free model; prod e.g. 30 s / 90 s).
  - The model's reasoning budget becomes a setting (`ai-reasoning`: off | low | high) where the model supports it
    (Nemotron: thinking on/off; GPT-5.4: reasoning effort). Prod default: low.
  - The page shows each answer's time; the Q&A log already records it, so prod's speed can be checked.

### 6.3 Which model: measured, not guessed (user, 2026-10-20)
Prod has two models (Nemotron 3 Super on its own vLLM, no key; GPT-5.4, with a key). Only Nemotron was ever tried
(dev, NVIDIA's free API: tool calls 10/10, median 4.3 s per call). Starting point: **Nemotron** (on the high side, no
cost per call, and after this redesign most of the hard work is in code); then measure both:
- **`relay_assist.py --compare`**, run inside the relay-assist container on prod (it has the token and the tools):
  the same ~15 questions (6.1's set: outages, losses, rates, one container, the setup, "is it OK now?", one open
  incident) to each model, through the **real question loop and the real tools** (tool calling exactly as in prod).
- Per question and model: seconds, rounds, tool calls (right tool, valid arguments, errors), the answer, and a
  check against the data where code can tell (e.g. the longest outage from get_outages, the loss counts from
  get_delivery); the rest side by side for you to judge.
- Output: a text report in the relay-assist container's log folder; nothing written to relay.db, no message
  content (monitoring data only, as always).
- Then choose: one model for everything, or e.g. Nemotron for the automatic explains and wrap-ups and GPT-5.4 for
  "Ask Analyst". The settings take a second model for that (`ai-ask-url`, `ai-ask-model`; empty = the same).
- `tools/ai_probe.py` stays for a quick "is this endpoint up and does it call tools" check.

## 7. The sim tests: 15, each a different reaction
| # | Test | What the monitor should show differently | Fault on / watch after |
|---|---|---|---|
| 1 | signal-cli loses the internet | Signal servers WARN, DOWN at 2 min, the Analyst | 3:45 / 2:00 |
| 2 | signal-cli on a bad network | no false DOWN | 2:00 / 1:00 |
| 3 | subscriber's link to signal-cli cut | events connection DOWN, subscriber WARN, signal-cli UP | 1:30 / 1:30 |
| 4 | monitoring route down | low side UNKNOWN, Monitoring marker DOWN | 3:00 / 1:30 |
| 5 | monitoring route lossy | Monitoring marker WARN (10-min window) | 4:00 / 1:00 |
| 6 | console out of reach for monitor-receiver | monitor-receiver WARN (waiting), replay, nothing STALE | 1:00 / 1:00 |
| 7 | subscriber stopped | subscriber DOWN (container stopped), chat routes "nothing can be sent" | 1:30 / 1:30 |
| 8 | receiver stopped | receiver DOWN, its routes and Gotham box | 1:30 / 1:30 |
| 9 | signal-cli frozen | signal-cli DOWN (paused), subscriber WARN | 1:10 / 2:00 |
| 10 | memory: near the limit, then out of memory | CONTAINER_MEM_HIGH, then CONTAINER_OOM | 2:30 / 0:30 |
| 11 | crash loop (test subscriber, broken routes file) | restarts, CONTAINER_RESTARTING | 1:30 / 0:30 |
| 12 | junk packet | the chat route WARN (junk), receiver UP | 1:00 |
| 13 | packets lost on the way (gap + incomplete message) | the route WARN, a lost-message notice | 1:30 |
| 14 | a room no receiver serves | that route DOWN "no receiver serves it" | 2:00 / 1:00 |
| 15 | a subscriber started twice | both WARN, SUBSCRIBER_TWICE | 2:30 / 2:00 |

Dropped as duplicates: the silent internet cut (= 1), subscriber killed (= 7), crash once (in 11), old header
(= 12), box removed (housekeeping, not a fault). Dropped (user, 2026-10-20): packet for the wrong room. The room
in a packet's header comes from routes.conf, never from code by hand; a receiver only gets another room's packets
through a set-up mistake (the diode's routes, swapped ports, a receiver's rooms setting, another app's traffic).
The receiver's ROOM_MISMATCH check stays. Test 15's twin uses the real subscriber's room (with the made-up group),
so it shows only SUBSCRIBER_TWICE; only test 14 uses room SIMTEST. The list also records the Analyst: a line when it starts on an
incident and when it answers.

Still not testable without messages or test switches: messages lost inside the diode, my_forward failing.

## 8. Where the changes go
- `low/monitor/signal_monitor.py`: subscriber state rules (4.2), `link` field.
- `low/subscriber/subscriber_udp.py`: plain words per stream code.
- `high/console/monitor.py`: boxes (3), connections (4), rooms and groups (4.1), Monitoring windows, header data,
  test labels (5.2).
- `high/console/incidents.py`: findings over the whole relay, incidents per problem, WARN wait, connections as parts.
- `high/console/console_web.html`: wires from connections, markers, header, Analyst dot, test line, Ask layout.
- `high/assist/relay_assist.py`: knowledge, get_state with connections, the tools of 6.1, the cleaning step, the
  Ask shape.
- `high/console/store.py` and `console_web.py`: the daily count table and the API behind the new tools (read-only,
  token as now).
- `high/console/runbook.json`: causes that now point at connections, new ones (route WARN codes, MONRX_WAITING).
- `tools/sim/`: the 16 tests, marker on one host, the Analyst lines.
- `high/docs/monitoring.md`: rewritten sections for boxes and connections.

## 9. Order and how it is checked
1. Boxes and connections (signal-monitor, console, page). Checked in a throwaway console fed made-up states for
   every case in sections 3 and 4, with screenshots, before anything restarts on dev.
2. Rooms, Monitoring windows, header.
3. Incidents (per problem, WARN wait, test labels): the 12 rule cases from 2026-10-17 again plus the new ones.
4. The Analyst: input, tools, history, cleaning, Ask shape. Checked by asking the three questions from 2026-10-20
   again, plus a set of about 15 typical questions (outages, losses, rates, one container, the setup, "is it OK
   now?"), each answer checked against the data by hand; then `--compare` on prod for the two models (6.3).
5. The sim tests. Run only when you say so.

## 10. Your calls
Decided (user, 2026-10-20):
1. A subscriber that runs but isn't connected: **WARN**.
2. Incidents per problem instead of per side: **yes**.
3. The Analyst failing: a red dot on its button, no box: **yes**.
4. Gotham box subtitle "hand-off to my_forward": **no**. Later the same day: drop its small-print description; its
   name stays each receiver's `FORWARD_TO` (keyed in by hand).
7. Room markers: **name and counts (sent → arrived), no colour or ✓/!/✕**; the wires show the state. The Monitoring marker keeps loss · delay, no colour.
8. Timing: **colour = now** (section 4a); what opens an incident (5.1).
9. The Analyst: **60 s wait is fine**; prod **faster and no limit** (6.2).
10. Which model: start with Nemotron, **measure both** with `--compare` (6.3).

5. Test boxes and room SIMTEST: **stay on the diagram** as now (the diagram may grow while tests 14-15 run).
6. The Analyst's tools (6.1): **yes**.
