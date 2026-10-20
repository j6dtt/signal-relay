# Adding or removing a subscriber or a receiver

Adding or removing one restarts nothing else, so the other rooms keep flowing. This page says what to edit, which
command to run, and how to check first.

## Why nothing else restarts

`docker compose up -d` looks at each container in the file and **recreates only the ones whose own settings changed**.
A container whose block you didn't touch keeps running.

So the rule is: adding a subscriber or receiver must not change any other container's block. Two lists used to be
in other containers' settings and are now files that their container reads again by itself:

| List | File | Read again by |
|---|---|---|
| who may connect to signal-cli (each subscriber, signal-monitor) | `low/gateway/clients.conf` | signal-cli (image r5), at the next connection |
| where each room's packets go (dev only) | `high/diode-sim/routes.conf` | diode-sim, within 2 s |

Prod has no diode-sim: there, the second list is the **real diode's routes file**, which isn't ours to restart.

## Check before you start anything

`--dry-run` shows what compose would do, without doing it:

```
docker compose --dry-run up -d
```

- `Running`: left alone.
- `Recreate`: would be stopped and started again with new settings.
- `Create` / `Start`: a new container.

Only your new container should say `Create`. If anything else says `Recreate`, its block changed: look at it before
going on (`git diff docker-compose.yml`).

## Add a subscriber (low side)

Example: `signal-subscriber-ops`, sending room `OPS`.

1. **Its certificate** (on dev, where the CA is):
   ```
   image/tlscerts client signal-subscriber-ops
   ```
   This makes `low/certs/signal-subscriber-ops/` and adds the line `signal-subscriber-ops` to
   `low/gateway/clients.conf`. signal-cli lets it in at its next connection. If signal-cli runs on another host,
   add the same line to that host's `low/gateway/clients.conf`.
2. **Its routes:** in `low/routes.conf`, one line per room it sends:
   ```
   signal-subscriber-ops    OPS    <group ID>
   ```
3. **Its block** in `low/docker-compose.yml`: copy the `subscriber-dtt` block and change the service name,
   `container_name`, `ROUTES_FOR`, `STATE_FILE`, `SENT_LOG` and the certificate folder:
   ```yaml
     subscriber-ops:
       ...
       container_name: signal-subscriber-ops
       environment:
         ROUTES_FOR: signal-subscriber-ops
         STATE_FILE: /logs/subscriber-ops-last-event-id
         SENT_LOG: /logs/subscriber-ops-udp-sent.jsonl
       volumes:
         - ./certs/signal-subscriber-ops:/certs:ro
   ```
4. **Check, then start only it:**
   ```
   cd low
   docker compose --dry-run up -d          # only signal-subscriber-ops: Create
   docker compose up -d --no-deps subscriber-ops
   ```
5. **The room needs a receiver** (below), or the console shows its route as "no receiver serves this room".

The new subscriber's box appears on the console at its first report (within 10 s).

## Add a receiver (high side)

Example: `signal-receiver-ops`, serving room `OPS`.

1. **Its block** in `high/docker-compose.yml`: copy the `receiver-dtt` block and change the service name,
   `container_name`, `ROOMS`, `FORWARD_TO`, `RECORD_LOG`, `PROGRESS_FILE`, and on prod its `ports:` (a port no other
   receiver uses):
   ```yaml
     receiver-ops:
       <<: *receiver
       container_name: signal-receiver-ops
       environment:
         <<: *receiver-env
         ROOMS: OPS
         FORWARD_TO: Gotham Chat              # or "off"
         RECORD_LOG: /logs/ops-records.jsonl
         PROGRESS_FILE: /logs/ops-progress.json
       # prod: ports: - {target: 6510, published: "6514", host_ip: *listen-ip, protocol: udp}
   ```
2. **Check, then start only it:**
   ```
   cd high
   docker compose --dry-run up -d          # only signal-receiver-ops: Create
   docker compose up -d --no-deps receiver-ops
   ```
3. **Its route:**
   - dev: a line in `high/diode-sim/routes.conf`: `OPS   signal-receiver-ops:6510`. diode-sim's log says
     "Routes changed … Added: OPS" within 2 s.
   - prod: a route on the diode: `CHAT|OPS` → `<listen-ip>:6514`.

The receiver's box (and its Gotham box, unless `FORWARD_TO` is off) appears at its first report (within 30 s).

`--no-deps`: start only the one named, not the containers it depends on (a receiver depends on the console: without
it, a changed console block would be recreated too).

## Remove one

Stop and remove only that container, by its service name:

```
docker compose rm -sf subscriber-ops        # or receiver-ops; -s stops it first, -f doesn't ask
```

Then tidy up, in any order (none of these restarts anything):
- a subscriber: its line in `low/gateway/clients.conf` (locks its certificate out) and its lines in `routes.conf`;
- a receiver: its route (dev: `high/diode-sim/routes.conf`; prod: the diode);
- its block in the compose file. Until the block is gone, a later `docker compose up -d` would start it again.

Its box leaves the console 2 minutes after its container is removed (high side: remove it by hand in its Log window
after an hour).

## Avoid

- **`docker compose up -d` with no service name after editing a shared block** (`x-settings`, `x-receiver`): every
  container using it is recreated. That's right when you mean it (a new image, a new setting for all); check with
  `--dry-run` first.
- **`docker compose down`:** stops everything on that side.
- **`docker compose up -d --remove-orphans`:** removes any container of the project not in the file, which includes
  one whose block you commented out to keep.
