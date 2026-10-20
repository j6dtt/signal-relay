# Fault-simulation tests (`tools/sim`): how the monitoring reacts to real faults

Each test puts a real fault on (a cut network, a stopped container, a bad packet), takes it away after a while,
and lists everything the monitor showed meanwhile. Test tool: dev, or prod before go-live.

## Dev

**Once:**
```
cd /apps/signal-relay
tools/sim/run setup
```

**Each time:**
```
cd /apps/signal-relay
tools/sim/run
```
Type a test number, Enter, Enter. Read the list. Enter goes back to the menu, `q` quits.

## Prod

Prod has two hosts:
- **LOW host:** signal-cli and the subscriber (`low/`)
- **HIGH host:** the receivers and the console (`high/`)

**Once, on dev:**
```
cd /apps/signal-relay
tools/sim/run export
```
Copy the `tools/sim` folder to `/apps/signal-relay/tools/sim` on the LOW host and on the HIGH host.

**Once, on the LOW host, then on the HIGH host:**
```
cd /apps/signal-relay
tools/sim/run setup
```
Then, on each of the two, add its name (`cat /etc/hostname`) to `allow_hosts` in `tools/sim/sim.conf`.

**Each time:** open a terminal on each host and run:
```
cd /apps/signal-relay
tools/sim/run
```
1. **HIGH host:** type the test number, Enter, Enter. It waits.
2. **LOW host:** type the **same** number, Enter, Enter.
3. **HIGH host:** read the list.

Tests marked `[low host]` in the HIGH menu need step 2. The others run on the HIGH host alone (skip step 2).

**After the test window:** take the host names out of `sim.conf`, and on the HIGH host run
`docker exec signal-console python console_users.py token remove sim`.

## What you see
```
  [0:00] Test     Putting the fault on in signal-subscriber:
                  its connections to signal-cli refused
  [0:01] Test     The fault is on. Watching the monitor for 1:30.
  [0:06] Log      signal-subscriber (on ubt2) is WARN: running, but
                  not connected to signal-cli: connection refused
                  (was UP).
  [0:27] Log      signal-cli to signal-subscriber (events) is DOWN:
                  connection refused; for 21 s (was WARN).
  [1:06] Log      Incident #41 on the low side (ubt2) (test
                  net.sub-link): likely cause: the subscriber can't
                  reach signal-cli.
  [1:30] Test     Taking the fault away
  [1:36] Log      signal-subscriber (on ubt2) is UP (was WARN).
  [2:39] Log      Incident #41 ... closed after 93 s

  The monitor showed 3 thing(s) while the fault was on,
  2 after it was taken away.
```
- `[0:00]` is when the fault went on.
- **Test:** what the test did.
- **Log:** a new line in the monitor's Log.
- **Diagram:** a change on the Monitor page that has no Log line.
- **Analyst:** the Analyst started on an incident, or answered (how long, its headline).
- Every test is **labelled** at the console: its incidents and Log lines show a dashed **test** chip and never count
  as real history (the Analyst leaves them out).
- Each run is saved as text in `tools/sim/reports/` (on prod: on the high host).
- **Ctrl-C** stops a test and puts everything back. **r** in the menu undoes anything a stopped test left.

## Before you start
- Start from a healthy relay: the menu shows its state at the top, and asks before running on one that isn't.
- A one-off problem (a junk packet, a lost message) leaves no colour behind: it is a Log line, and a lost message
  is an incident that stays in the bell until someone opens it.
- Test 5 (monitoring packets partly lost) keeps the Monitoring wires amber for up to 5 min after it ends.
- What a test costs while it runs: Signal messages wait on Signal's servers (1, 2, 9) or in signal-cli (3, 7).
  During 8 (receiver stopped), messages for that receiver's room are lost. Run on a quiet relay.

## The tests
15 tests, each makes the monitor react differently (2026-10-20; was 24, several reacted the same way):

| # | Test | Fault on / watched after |
|---|---|---|
| 1 | signal-cli loses the internet | 3:45 / 2:00 |
| 2 | signal-cli on a bad network | 2:00 / 1:00 |
| 3 | subscriber loses its link to signal-cli | 1:30 / 1:30 |
| 4 | monitoring packets stop arriving | 3:00 / 1:30 |
| 5 | monitoring packets partly lost | 4:00 / 1:00 |
| 6 | console out of reach for monitor-receiver | 1:00 / 1:00 |
| 7 | subscriber stopped | 1:30 / 1:30 |
| 8 | receiver stopped | 1:30 / 1:30 |
| 9 | signal-cli frozen | 1:10 / 2:00 |
| 10 | memory: near the limit, then out of memory | 2:30 / 0:30 |
| 11 | a container in a crash loop (test subscriber, broken routes file) | 1:30 / 0:30 |
| 12 | junk packet | watched 1:00 |
| 13 | packets lost on the way (a gap, an incomplete message) | watched 1:30 |
| 14 | a room no receiver serves (SIMTEST) | 2:00 / 1:00 |
| 15 | a subscriber started twice (the real name and room) | 2:30 / 2:00 |

## Safe by design
- **Only the relay's containers:** it acts on containers of the compose projects `signal-low` and `signal-high`,
  and on the ones it starts itself (label `signal-sim=1`). It refuses any other container.
- **Network faults stay inside one relay container:** a helper joins that container's own network namespace and
  changes rules there only. Never the host's firewall or network, never another app.
- **No fake messages:** test packets are junk, for a room nobody serves, or parts of messages that never complete.
  Test subscribers are copies of the real one that send nowhere. Nothing reaches the console's chat or Gotham.
  Test 13 may leave a "message … was lost" notice in the "All" window.
- **It puts everything back:** after every test, on an error, on Ctrl-C, and with **r**.
- **Only on listed hosts** (`sim.conf`).

## What earlier runs showed (2026-10-19, dev)
- **Docker doesn't restart a container killed through Docker** (`docker kill`, like `docker stop`, counts as stopped
  by hand). A program that dies by itself, or is killed for memory, is restarted. Test 10 starts it again itself.
- **A false alarm, fixed in image r3:** after the signal-cli frozen test, the gateway counted signal-monitor's
  broken health check as a refused certificate (`GATEWAY_REFUSED`, WARN for 10 min). r3 counts only real refusals.

## Limits
- Prod: one low host (the shipped layout: signal-cli and the subscriber together).
- Test subscribers add boxes to the diagram while they run (11, 14, 15).

## Advanced: no menu (dev only)
```
tools/sim/run list                         # every test, by number and name
tools/sim/run test net.sub-link app.bad-packet   # tests by name, one after another
tools/sim/run all                                # every test (about 50 min)
tools/sim/run restore                      # undo anything a stopped run left
```
`--side low|high` tries the two-host steps on one host (two terminals). Design and catalogue:
[`high/docs/simulation-proposal.md`](simulation-proposal.md).
