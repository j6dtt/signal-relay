# Updating and rolling back

How to bring a change to prod (new code, a new setting, a new signal-cli image) and how to undo it. Prod is
air-gapped on the high side: everything goes over as files. Adding or removing a subscriber or receiver is in
[adding-subscribers-receivers.md](adding-subscribers-receivers.md) instead.

## The rules

1. **Know what will restart before it does:** `docker compose --dry-run up -d` lists every container compose would
   touch. `Recreate` = stopped and started with new settings; `Running` = left alone.
2. **Restart only what changed,** by name: `docker compose up -d --no-deps <service>` (after a compose edit) or
   `docker compose restart <service>` (after a script edit: the scripts are mounted, not built in).
   **`restart` does not apply a changed compose file** (settings, environment, ports, image): it restarts the old
   container as it was. After editing `docker-compose.yml`, always `up -d` (it recreates the container with the new
   settings).
3. **High side first, then low side,** so the receivers listen before packets arrive.
4. **Keep the old version** until the new one has run a while: copy the files you replace, keep the old image.
5. **Never two signal-cli on one account's data:** compose stops the old signal-cli before it starts the new one;
   never start a second by hand.

## Which change needs what

| Changed | Then | What restarts |
|---|---|---|
| A script (`*.py`, `console_web.html`) | `docker compose restart <service>` | that container; `console_web.html` needs nothing (read at each page load) |
| A service's block in `docker-compose.yml` | `docker compose up -d --no-deps <service>` | that container |
| The settings block (`x-settings`) or a shared block (`x-receiver`) | `--dry-run` first, then `docker compose up -d` | **every container using it**: check the list |
| `low/gateway/clients.conf` | nothing (signal-cli reads it again) | none |
| `high/diode-sim/routes.conf` (dev) | nothing (diode-sim reads it again) | none |
| `low/routes.conf` | restart the subscriber(s) whose lines changed | those subscribers |
| `high/docs/*.md` | nothing (the Analyst reads them again) | none |
| `high/console/runbook.json` | `docker compose restart console` | the console |
| The signal-cli image | [A new signal-cli image](#a-new-signal-cli-image) | signal-cli (about 15 s: messages wait on Signal's servers, nothing is lost) |
| `high/.env` | `docker compose up -d --no-deps relay-assist` | relay-assist |

## New code from dev

1. **On dev:** commit, and note what changed (`git log`, `git diff --stat <last prod commit>`).
2. **Copy the changed files** to prod (the whole `low/` or `high/` folder, or only the changed files). Never copy over
   prod's own files: the settings blocks, `.env`, `routes.conf`, `gateway/clients.conf`, `signal-data/`, `certs/`,
   `logs/`, `data/`. For a changed compose file, merge prod's settings block into the new one by hand.
3. **Keep a copy** of every prod file you replace, e.g. `cp -a high/console high/console.before-<date>`.
4. **High side,** in `high/`: `docker compose --dry-run up -d`, then restart what the table above says.
5. **Low side,** in `low/`: the same.
6. **Check:** every box UP on the console within 2 minutes, the problems tile at 0, and a test message through.

**Rolling back:** put the copies back and restart the same containers.

## A new signal-cli image

1. **On dev:** `image/check-update --prepare --apply --build` builds `signal-cli:<version>-rN` and scans it
   ([image.md](image.md), "Updating"). Test it on dev for a while.
2. **Export it:**
   ```
   cd image
   docker save -o signal-cli-<tag>.tar signal-cli:<tag>
   sha256sum signal-cli-<tag>.tar > signal-cli-<tag>.tar.sha256
   ```
3. **On the prod signal-cli host:** copy both files, then:
   ```
   sha256sum -c signal-cli-<tag>.tar.sha256
   docker load -i signal-cli-<tag>.tar
   ```
4. **Switch:** in `low/docker-compose.yml`'s settings block, `signal-image: &signal-image signal-cli:<tag>`, then
   ```
   cd low
   docker compose --dry-run up -d          # only signal-cli: Recreate
   docker compose up -d --no-deps signal
   docker logs --since 1m signal-cli 2>&1 | grep -E "Clients allowed|listening"
   ```
   The subscribers reconnect by themselves; their boxes go WARN for a few seconds, then UP.

**Rolling back:** put the previous `signal-image` value back and run the same `up -d --no-deps signal`. Keep the
previous image (`docker images signal-cli`) until the new one has run a week.

## A new Python image (`python:3.13-alp-u3`)

It runs every container except signal-cli. Bring it over like signal-cli's image (`docker save` / `load`), then
recreate the containers one side at a time: high first (`--dry-run`, then `docker compose up -d`), then low.

## After any update

- The console's Log has a line for every box that restarted: check each came back UP.
- Ask Analyst (The relay now): "What happened in the last hour?"
- After a model or Analyst change: `docker exec signal-relay-assist python relay_assist.py --compare > compare.txt`
  ([monitoring.md](monitoring.md), "Comparing models").
