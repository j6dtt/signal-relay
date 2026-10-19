# tools/sim: fault-simulation tests for the relay's monitoring

Each test injects a real fault, checks that the monitoring shows it (the right box, state, code, event or incident,
within a deadline), then puts everything back. **Test tool: dev, or prod before the relay goes live.**
Design and the full catalogue: [`../../docs/simulation-proposal.md`](../../docs/simulation-proposal.md).

## Safe by construction
- **Only the relay's containers:** the runner acts on containers of the compose projects `signal-low` and
  `signal-high`, and on the ones it starts itself (label `signal-sim=1`). It refuses any other container.
- **Network faults stay inside one relay container:** a helper joins that container's own network namespace
  (`--network container:<name>`) and changes rules there only, tagged `signal-sim`. Never the host's firewall or
  network, never another app.
- **No fake messages:** chat packets it sends are junk, for a room nobody serves, or parts of messages that never
  complete. Test subscribers are copies of the real one (its image, settings, certificate, network, user and
  limits, so they work with prod's `SIGNAL_URL`), but with their own routes file and no read-write mount, and they
  send to `127.0.0.1:9` (nowhere). Nothing reaches the console's chat or Gotham. Two tests
  leave a "a message … was lost" notice in the "All" window (`app.gap`, `app.lost-message`).
- **It puts everything back:** after every test, on failure, on Ctrl-C, and with `run restore`.
- **Only on listed hosts:** `sim.conf` `allow_hosts` (the hosts' `/etc/hostname`). Prod's names go in only for the
  pre-live test window, and out again after.
- **What a test costs while it runs:** Signal messages wait on Signal's servers (`net.signal-*`,
  `docker.pause-signal-cli`) or in signal-cli (`net.sub-link`, `docker.*-subscriber`); during
  `docker.stop-receiver`, messages for that receiver's room are lost (UDP is one-way). Run on a quiet relay.

## Setting up (once per host)
```
cd /apps/signal-relay
tools/sim/run setup
```
It builds the helper image `signal-sim-helper:1` (alpine + iptables + tc; needs the internet once) and, where the
console runs, makes a console token `sim` that can only read the monitoring API (kept in `tools/sim/.token`, mode
600, gitignored). Split hosts: run it on each.
Air-gapped host: on dev, `docker save signal-sim-helper:1 python:3.13-alp-u3 -o sim-images.tar`; there,
`docker load -i sim-images.tar`. Remove the token after the test window:
`docker exec signal-console python console_users.py token remove sim`.

## Running
```
tools/sim/run list                         # every test, how long it takes
tools/sim/run test net.signal-reject       # one test (or several)
tools/sim/run group c                      # a group: a network, b Docker, c application
tools/sim/run all --quick                  # everything; --quick skips the long waits
tools/sim/run restore                      # undo anything a stopped run left (also run at every start)
```
Start from a healthy relay (every box UP). One line per test, then a JSON report in `tools/sim/reports/`:
```
PASS  app.misrouted            signal-receiver-relay WARN ROOM_MISMATCH in 2 s
FAIL  app.box-removed          expected event PART_REMOVED on /subscriber/signal-sim-removed$ within 200 s
```
Watch the Monitor diagram while it runs: you see each fault, its incident and the recovery live.

## Prod: low and high side on different hosts (split mode)
The runner works out which side a host is from the relay containers on it. When it has only one side, give both
hosts the same command, with the same start time (UTC, a few minutes ahead), and start both before that time:
```
tools/sim/run all --at 14:30        # on the low host
tools/sim/run all --at 14:30        # on the high host
```
- Both print the same plan: one time slot per low-side fault (`run list` shows each test's side), and a plan code;
  check that both show the same code.
- **Low host:** injects each fault in its slot and takes it away after a fixed time. Its lines say `DONE`.
- **High host:** checks what the monitoring shows in each slot (`PASS`/`FAIL`), then runs the high-side tests.
  **Its report has the results.** It warns if it doesn't see the low side's runner (a marker container,
  `signal-sim-low-<code>`, that container-facts reports across the diode).
- The hosts' clocks must agree (NTP). A slot that has already begun is skipped. Ctrl-C on either side puts its
  own side back; stop both.
- The low side starts a marker for each fault it has applied. A high-side FAIL whose fault never got one says so
  ("the low side never signalled this fault as applied"): look at the low side's report first.
- `all --at 14:30` takes about 75 minutes (`--quick`: about 40, then the high-side tests).
- Try it on one host: add `--side low` to one runner and `--side high` to the other.

## The tests
| Group | Tests |
|---|---|
| A. network | `net.signal-reject` / `net.signal-drop` (signal-cli cut off from Signal: refused / silently), `net.signal-flaky` (30% loss: must not look like an outage), `net.sub-link` (subscriber ↔ signal-cli), `net.mon-route-down` / `net.mon-route-lossy` (the monitoring route), `net.console-away` (monitor-receiver holds and replays) |
| B. Docker | `docker.stop-subscriber`, `docker.stop-receiver`, `docker.kill-subscriber`, `docker.restart-loop`, `docker.pause-signal-cli`, `docker.oom` and `docker.mem-high` (on a test container) |
| C. application | `app.bad-packet`, `app.old-header`, `app.misrouted`, `app.gap`, `app.lost-message`, `app.room-unserved`, `app.subscriber-twice`, `app.routes-broken`, `app.box-removed` |

## What the runs showed (2026-10-19, dev)
- **Docker doesn't restart a container killed through Docker** (`docker kill`, like `docker stop`, counts as stopped
  by hand: `restart: unless-stopped` leaves it down). A program that dies by itself, or is killed for memory (OOM),
  is restarted. `docker.kill-subscriber` therefore starts it again itself; `docker.crash` tests the restart.
- **A false alarm, fixed in image r3:** after `docker.pause-signal-cli`, signal-monitor's timed-out health check met
  the gateway as a broken TLS handshake ("Broken pipe"), which r2 counted as a refused certificate (`GATEWAY_REFUSED`,
  WARN for 10 min). r3 counts only real refusals; the test passes with r3 (and still fails on r2, naming it).
- **The two monitoring-route tests run last:** they cost monitoring packets, so the diode link (the Monitoring
  marker) stays WARN for up to an hour (loss is counted over the last hour), and its incident stays open meanwhile.
- **Between tests** the runner waits (up to 5 min) for every box to be UP again, else it skips the next test with
  why. A box keeps its last report for a while, so "back UP" counts only a report heard after the recovery.

## Limits
- **Split mode: one low host.** Prod's shipped layout (signal-cli and the subscriber on one host). With the
  subscriber on its own host, run the low side where the test's container is.
- `tools/sim` can sit anywhere on a host (it no longer needs to be next to `low/`).
- In split mode the low side can't see the monitoring, so each fault stays on for its full slot (not just until it
  shows), and a relay that isn't healthy at a slot's start gets the fault anyway (the high side says SKIP).
- The helper needs the internet once (setup); the runner and tests don't.
- Each test needs its parts to report: start with every box UP, or a test may wait for the wrong reason.
- A network fault that can't be applied in its container (iptables or tc failing there) fails at once with "the
  fault was NOT applied in …", not as a monitoring miss.
- The token is read from `tools/sim/.token` inside the runner, never put on a command line.
