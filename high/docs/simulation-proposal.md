# Fault simulation package for the relay's monitoring (proposal)

Status: **groups A, B, C built** (2026-10-19): `tools/sim/` (instructions: `high/docs/sim-tests.md`). Not yet: hooks (D),
the rig (E). **Changed as built (user, 2026-10-19): the tests observe, they don't judge.** Each one keeps its fault on
for a fixed time and lists everything the monitor showed; there is no PASS/FAIL, so the "Expected" columns below are
only what we thought would show. Run from a menu, one test at a time (on prod: the menu on both hosts).
Original goal: inject real faults (network, Docker, application, warnings) on dev, and check automatically that the
monitoring sees each one: the right box, state, code, incident and cause, within the expected time, and that
everything recovers after.

## 1. Shape
```
tools/sim/
  sim.py          one Python CLI (standard library only), run in a container
  scenarios.py    the catalogue: each test = setup, expected result, revert
  helpers/        tiny helper images' scripts (network faults in a container's own namespace)
  rig/            a throwaway copy of the relay with a fake signal-cli, for tests that need messages
```
- **Runs in a container:** `python:3.13-alp-u3` with the Docker socket mounted. It talks to Docker through the
  Engine API over the socket (stdlib `http.client`, no Docker CLI, no extra packages).
- **Network faults** run in small helper containers that join the target container's own network namespace
  (`--network container:<name>`, as `tools/signal-block` does): iptables (block, reject) and `tc netem` (delay,
  loss, jitter). Never the host's firewall or network (hard constraint), never another app.
- **Checks** read the console's read-only monitoring API with a token (`console_users.py token add sim`):
  `/api/monitor/state`, `/events`, `/incidents`. No database access, no message content.
- **Every test reverts itself:** on success, failure, timeout or Ctrl-C. `sim.py restore` undoes anything left
  (helpers removed, limits and settings put back, rules tagged `sim` cleared).

## 2. Using it
```
cd /apps/signal-relay
tools/sim/run list                        # every test, its group, how long it takes
tools/sim/run test net.signal-reject      # one test
tools/sim/run group docker                # a group
tools/sim/run all --quick                 # everything under ~3 min each
tools/sim/run restore                     # undo anything a stopped run left
```
`tools/sim/run` is a short wrapper around `docker run --rm -v /var/run/docker.sock:… -v tools/sim:/sim …`.

Output, one line per test, then a JSON report in `tools/sim/reports/`:
```
PASS  net.signal-reject     Signal servers WARN in 28 s, DOWN (SIGNAL_UNREACHABLE) in 2:34, incident #26, back UP 7 s after
FAIL  docker.oom-subscriber expected CONTAINER_OOM within 60 s, saw CONTAINER_CRASHED (exit 137)
```

**Safety:** refuses to start unless the console reports a dev host (a `sim-allow` setting naming allowed host names;
prod names never listed); a test never runs longer than its limit; never touches `signal-data`, certificates or
`relay.db`; chat packets it sends are invalid or for a test room only (no fake messages in the live console).

## 3. Tests (catalogue)
Each line: what it does → what the monitoring must show (deadline).

### A. Network (helpers in the target's own namespace)
| Test | Fault | Expected |
|---|---|---|
| `net.signal-reject` | signal-cli → internet refused | Signal servers WARN `SIGNAL_RECONNECTING` (60 s), DOWN `SIGNAL_UNREACHABLE` (3 min), incident; subscriber stays UP |
| `net.signal-drop` | signal-cli → internet silently dropped | same, later (keep-alive timeout) |
| `net.signal-flaky` | 30% loss + 1 s jitter on signal-cli | reconnects counted; `SIGNAL_FLAPPING` or WARN; no false DOWN |
| `net.sub-link` | subscriber ↔ signal-cli (8443) blocked | subscriber DOWN `SUB_NOT_CONNECTED`; after: `EVENTS_MISSED` 0 (resumes) |
| `net.mon-route-lossy` | 20% loss into monitor-receiver | Monitoring marker WARN, `DIODE_LOSS` |
| `net.mon-route-down` | monitoring packets dropped | low side STALE → UNKNOWN, `LEFT_SILENT` or `MONITOR_ONLY_SILENT` |
| `net.console-unreachable` | receivers can't reach the console | `DELIVER_FAILED`; monitor-receiver holds and replays (no loss after) |

### B. Docker
| Test | Fault | Expected |
|---|---|---|
| `docker.stop-subscriber` | `stop` | `CONTAINER_STOPPED` (exit 0, "on purpose"), subscriber DOWN; recovery when started |
| `docker.kill-receiver` | SIGKILL | `CONTAINER_CRASHED`/exit 137, restart counted; receiver DOWN then UP |
| `docker.restart-loop` | 4 kills in 2 min | `CONTAINER_RESTARTING`, flapping guard (one event, "settled") |
| `docker.oom-subscriber` | memory limit lowered to OOM it | `CONTAINER_OOM`; limit restored |
| `docker.pause-signal-cli` | frozen (`pause`) | health check `CLI_TIMEOUT`, subscriber stream stalls |
| `docker.mem-high` | limit set just above use | `CONTAINER_MEM_HIGH` WARN |
| `docker.remove-test-box` | a throwaway subscriber container started, then removed | its box appears, then goes (`PART_REMOVED`) |

### C. Application (invalid or test traffic only)
| Test | Fault | Expected |
|---|---|---|
| `app.bad-packet` | junk datagrams to the chat input | receiver WARN `BAD_PACKET` (10 min window) |
| `app.misrouted` | a packet for a room the receiver doesn't serve | `ROOM_MISMATCH`, room marker "misrouted" |
| `app.old-header` | the retired `J6-DTT` header | `BAD_PACKET` |
| `app.lost-message` | first 2 of 3 parts of a test-session message | `MESSAGE_LOST` after 30 s, lost notice in "All" |
| `app.gap` | a packet number skipped | `PACKETS_MISSING` |
| `app.room-unserved` | test subscriber with a room no receiver serves | subscriber WARN `ROOM_UNSERVED`, idle marker |
| `app.subscriber-twice` | test subscriber with the same `ROUTES_FOR` | `SUBSCRIBER_TWICE` on both |
| `app.routes-broken` | test subscriber with a bad routes file | refuses to start, `CONTAINER_RESTARTING`, log says why |
| `app.analyst-down` | relay-assist pointed at a dead model URL | console WARN `AI_FAILED` |

### D. Hooks (need test switches in the code, off unless `SIM_HOOKS=1`)
| Test | Fault | Expected |
|---|---|---|
| `hook.forward-fail` | my_forward raises | Gotham box DOWN, `FORWARD_FAILED`, route wire red |
| `hook.forward-slow` | my_forward sleeps 3 s | Gotham WARN `FORWARD_SLOW` |
| `hook.rx-stall` | receiver loop blocks 30 s | `RX_STALLED` from the watchdog |
| `hook.db-error` | console write fails | `DB_ERROR`, Monitor still updates |

### E. Rig (a throwaway relay with a fake signal-cli; needed for anything with messages)
A separate compose project `simrig`, own networks, spare ports, scratch database, removed after. The fake
signal-cli (Python) serves `/api/v1/events`, `/api/v1/check` and `listContacts`, with controls:
| Test | Fault | Expected |
|---|---|---|
| `rig.chat-not-arriving` | messages sent, the room's route dropped | `CHAT_NOT_ARRIVING` (60 s), `CHAT_LOST` (15 min, `--long`) |
| `rig.cli-restart-loss` | fake signal-cli restarts with new event IDs | `CLI_RESTART_LOSS` |
| `rig.events-missed` | event IDs skip | `EVENTS_MISSED` |
| `rig.burst` | 500 messages at once | `QUEUE_BACKLOG`, maybe `RX_OVERFLOW`; nothing lost |
| `rig.oversize` | 20 KB message | split into ≤ 1472-byte parts, rebuilt whole |
| `rig.cert-expiring` | certificate valid 10 days | `CERT_EXPIRING` WARN |
| `rig.group-two-rooms` | one group in two rooms | both routes counted, shown once |
| `rig.windows` | messages per group | group windows' sent → arrived match; pulses |

## 4. Phases and effort
| Phase | What | Effort |
|---|---|---|
| 1 | runner, safety, checks via the API, `restore`; groups A and B; `app.bad-packet`, `misrouted`, `old-header`, `gap`, `lost-message`, `analyst-down` | ~1 day |
| 2 | test subscriber container (`room-unserved`, `subscriber-twice`, `routes-broken`, `remove-test-box`) | ~2-3 h |
| 3 | hooks (D), behind `SIM_HOOKS=1`, default off | ~2-3 h |
| 4 | the rig with a fake signal-cli (E) | ~1 day |

## 5. Open points
- **Hooks in the real code (D):** small test switches, off unless `SIM_HOOKS=1`. OK, or keep tests to faults from
  outside only?
- **Live dev or rig for A-C?** Proposed: A-C on live dev (no fake messages; Signal messages wait meanwhile), E in the rig.
- **Scheduled runs?** e.g. `run all --quick` nightly on dev, report kept 30 days.
