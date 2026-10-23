# Manual tests on prod

Hand-run checks of how the relay and its diagram behave when parts change. Unlike the sim tests
([sim-tests.md](sim-tests.md)) nothing is faked: you stop, remove and move the real containers. Run them only when a
short pause in the relay is acceptable. Commands from `low/` unless a step says otherwise.

## 1. Remove a subscriber and add it back

Uses the existing subscriber (here `signal-sub-relaypoc`). A second subscriber for the same group and room is not
allowed (the subscriber refuses to start: everything would be sent twice), so the test removes and re-adds the one
there is.

**1a. Stop it:**
```
docker compose stop signal-sub-relaypoc
```
✔ At once: its box DOWN ("stopped"), an incident opens, the room (RELAYPOC) shows no sender.

**1b. Send a test message in the relayed group.** ✔ It does not arrive yet: it waits in signal-cli's memory (up to
1000 messages).

**1c. Remove the container:**
```
docker compose rm -f signal-sub-relaypoc
```
✔ After about 2 minutes the box is gone (container-facts reported the container removed), and so is its group's
chip in the dock (unless its window is open). The RELAYPOC room marker stays: its receiver still serves it. About
1 minute later the incident closes.

**1d. Add it back:**
```
docker compose up -d --no-deps signal-sub-relaypoc
docker logs signal-sub-relaypoc 2>&1 | tail -5
```
✔ Within about 10 s:
- the box is back and UP, its wire to the room is back, the group's chip is back in the dock;
- no new incident ("starting up" opens one only after 90 s);
- the message from 1b arrives: the subscriber resumes from its saved position (`STATE_FILE`), so nothing is lost;
  its log says nothing about lost messages.

## 2. Move the low side to another host

The steps are also in the comment at the top of `low/docker-compose.yml` ("Moving to another host").

**2a. On the new host, before:**
- the image: `docker load -i signal-cli-0.14.9-r5.tar`; the python image from the registry;
- if the diode only takes packets from known hosts: the new host's address added;
- your account is in the docker group there, with the same `id -u`/`id -g` (else change `relay-user` and the files'
  owner).
- Signal reachable from containers there:
  `docker run --rm python:3.13-alp-u3 python -c "import socket; socket.create_connection(('chat.signal.org',443),5); print('ok')"`.

**2b. Old host:**
```
docker compose down
docker ps -a --format '{{.Names}}' | grep signal
```
✔ The `grep` prints nothing. **Never start the new host before this:** two signal-cli on one account knock each
other off Signal's servers and break the sessions (`ProtocolInvalidMessageException`).

**2c. New host.** If it mounts the same `/nfs/prod/signal-chat-relay`, there is nothing to copy; else copy all of
`low/`, with `signal-data/`, `certs/`, `.env` and `logs/`.
```
cd /nfs/prod/signal-chat-relay/low
stat -c %g /var/run/docker.sock; grep -n docker-gid docker-compose.yml
id -u; id -g; grep -n relay-user docker-compose.yml
docker compose up -d && docker compose logs -f
```
✔ The numbers match (fix `docker-gid` if not); signal-cli says "Signal servers: connected".
Start as soon as the old host's `grep` printed nothing: no need to wait for the console. A few "Lost the connection
to signal-cli (EOF occurred in violation of protocol)" warnings in the first seconds are normal (signal-cli still
starting).

**2d. On the console:**
- at once: a new low-side panel named after the new host; its boxes UP within about 30 s;
- the old host's boxes go STALE, then DOWN/UNKNOWN, with an incident for the old host: expected;
- after an hour: the old host's signal-monitor box → its Log → **Remove this box** (removes the whole old host);
  they do not go by themselves, since the old host's signal-monitor is gone too;
- a test message arrives. Messages sent during the move waited at Signal's servers (signal-cli was down) and come
  once it is up.
- No `ProtocolInvalidMessageException` in signal-cli's log on the new host (it would mean a second signal-cli is
  still running on this account).
