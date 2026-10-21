# Settings reference

Every setting, where it is, dev's value and what prod needs. Per-host settings are in the **settings block**
(`x-settings`) at the top of each `docker-compose.yml`; secrets in `high/.env`; per-container settings in each
container's block. After changing one, apply it with `docker compose up -d --no-deps <service>` (or `docker compose up -d` for the
settings block): `docker compose restart` does **not** pick up a changed compose file. Details:
[updating.md](updating.md).

## Choosing `relay-user`: which account the containers run as

Every container runs as the numeric `uid:gid` in `relay-user` (each host's settings block: a system account may get
a different uid on each host, which is fine). Whatever runs as that account can read the Signal account keys, the logs and `relay.db`.

| Option | `relay-user` | Who can read the keys, logs, database | Day to day |
|---|---|---|---|
| **A system account** (recommended) | its uid:gid, e.g. `"998:998"` | root, the `docker` group, and nothing else (no one can log in as it) | `ls` shows its name; reading logs, `relay.db` or taking backups needs `sudo` |
| The operator's own account (prod's choice, 2026-10-13) | `id -u`:`id -g` | everything that runs as the operator | no `sudo` needed |
| A bare number with no account | e.g. `"2400:2400"` | root, the `docker` group | like a system account, but `ls` shows a number, and an account created later with that uid could read everything |

**Making the system account,** on each host:

```
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin signal-relay
id signal-relay                     # e.g. uid=998(signal-relay) gid=998(signal-relay)
```

Then:

1. Put that host's numbers in its `relay-user`, e.g. `relay-user: &relay-user "998:998"`.
2. Give it the folders it writes, mode 700:
   ```
   cd low  && sudo chown -R signal-relay: signal-data certs logs && sudo chmod 700 signal-data certs certs/* logs
   cd high && sudo chown -R signal-relay: logs data console/certs && sudo chmod 700 logs data console/certs
   ```
   The scripts, compose files, `routes.conf` and `gateway/clients.conf` stay yours (they must be readable by
   everyone, and you edit them).
3. Use the same numbers for the admin tools: `RELAY_USER=998:998 image/signalctl ...` (else it makes account files
   the containers can't read).
4. Apply: `docker compose up -d` on each side (high first): every container is recreated with the new user.

**Changing later** works the same way: new numbers in `relay-user`, `chown` the folders to them, `up -d`. Nothing
needs a rebuild (the images' own user is overridden by `relay-user`; tested with `2400:2400`).

## Low side: `low/docker-compose.yml`, settings block

**To apply a change:** `cd low && docker compose --dry-run up -d`, then `docker compose up -d` (or `up -d --no-deps
<service>` for one container). `docker compose restart` does **not** apply a changed compose file.

| Setting | What it is | Dev | Prod |
|---|---|---|---|
| `relay-user` | the uid:gid every container runs as; `signal-data`, `certs`, `logs` must be owned by it | `"1000:1000"` | a system account's uid:gid, or the operator's `id -u`:`id -g` ([Choosing relay-user](#choosing-relay-user-which-account-the-containers-run-as)) |
| `signal-image` | the in-house signal-cli image | `signal-cli:0.14.9-r5` | the same tag, loaded from a file |
| `account` | the Signal number signal-cli runs as | dev's number | prod's number ([register-account.md](register-account.md)) |
| `signal-url` | where subscribers reach signal-cli's TLS gateway; its host name must be in signal-cli's server certificate | `https://signal:8443` | one host: `https://signal:8443` (the server certificate's CN must be `signal`); a subscriber on another host: `https://<CN>:<port>` |
| `diode-in` | the diode's input address: chat and monitoring are sent here | this host's address (diode-sim) | **the diode's input address** |
| `chat-port` / `monitor-port` | the diode's input ports for chat and monitoring (may be the same: the header decides) | `6510` / `6510` (diode-sim) | the diode's ports |
| `docker-gid` | the Docker socket's group, for container-facts | `"988"` | `stat -c %g /var/run/docker.sock` on that host |
| `message-log` | what `docker logs signal-cli` shows per message: `redacted` (content hidden), `full` (**includes text**), `off` | `redacted` | `redacted` |

**Other low-side files:** `routes.conf` (which groups go to which room, per subscriber:
[low-side.md](low-side.md)), `gateway/clients.conf` (who may connect: [tlscerts.md](tlscerts.md)).

**Per subscriber** (in its block): `container_name`, `ROUTES_FOR` (its name in `routes.conf`), `STATE_FILE`,
`SENT_LOG` (`""` = no sent log), its certificate folder mounted at `/certs`. Optional: `MONITOR_NAME`/`RELAY_ID` only
for a subscriber on another host.

**signal-cli** (its block): `TLS_LISTEN` (`0.0.0.0:8443`), `TLS_CLIENTS_FILE` (`/gateway/clients.conf`),
`RPC_ALLOW` (`listContacts`: nothing else, so nothing can be sent). A subscriber on another host: uncomment its
`ports:` (e.g. `"443:8443"`).

## High side: `high/docker-compose.yml`, settings block

**To apply a change:** `cd high && docker compose --dry-run up -d`, then `docker compose up -d` (or `up -d --no-deps
<service>` for one container). `docker compose restart` does **not** apply a changed compose file.

| Setting | What it is | Dev | Prod |
|---|---|---|---|
| `relay-user` | as on the low side; `logs`, `data`, `console/certs` must be owned by it | `"1000:1000"` | the same as the low side's choice ([Choosing relay-user](#choosing-relay-user-which-account-the-containers-run-as)) |
| `listen-ip` | the address of this host the receivers (and monitor-receiver) listen on for the diode's packets | this host's address | **the address the diode's routes point at** |
| `web-bind` | where the console page listens (HTTPS 8090) | `0.0.0.0` | `0.0.0.0`, or one address |
| `web-cert` / `web-key` | the page's certificate and key, in `console/certs/` | `/certs/ssl.lab.int.crt` / `.key` | the same names, prod's `*.lab.int` certificate |
| `ai-url` | the Analyst's model endpoint (OpenAI-compatible) | NVIDIA's free API (internet) | **a local endpoint**, e.g. `http://<vllm-host>:8000/v1` |
| `ai-model` | the model's name, exactly as the endpoint serves it | `nvidia/nemotron-3-ultra-550b-a55b` | e.g. `nvidia/nemotron-3-super-120b-a12b` (`<ai-url>/models` lists them) |
| `ai-ask-url` / `ai-ask-model` | a second model for Ask Analyst only; `""` = the main one | `""` | `""`, or GPT-5.4 if it can be reached from the high side |
| `ai-timeout` / `ai-total` | seconds per model call / for a whole question | `"90"` / `"300"` | `"30"` / `"90"` |
| `ai-reasoning` | the model's thinking: `off`, `low`, `high`, `""` (its default) | `""` | `low` |
| `ai-auto-per-hour` / `ai-auto-per-incident` | automatic Analyst questions; `"0"` = no limit | `"20"` / `"3"` | `"0"` / `"0"` |
| `ai-log` | how much of each question is kept: `summary`, `full` (the exact prompt too), `off` | `summary` | `summary` |
| `chat-view` | message text on the console: `on` (shown, kept in `relay.db`) or `off` (never stored or shown) | `"on"` | your call: `off` keeps no content on the console ([data-handling.md](data-handling.md)) |
| `chat-text` | how much of a message the group windows show: `full`, or `short` (first 5 words) | `short` | your call |
| `retention-days` | days of chat, monitoring and the Analyst's log kept in `relay.db` | `30` | `30`, or what your data rules allow |

**Per receiver** (its block): `container_name`, `ROOMS` (the room(s) it serves, comma-separated: one receiver can serve several, see
[adding-subscribers-receivers.md](adding-subscribers-receivers.md)), `FORWARD_TO` (the destination
box's name, `"off"` = no `my_forward`), `RECORD_LOG` (`""` = no record log), `PROGRESS_FILE`, and on prod `ports:`
(the port its diode route points at, a different one per receiver). The `x-receiver-env` block holds the defaults
every receiver shares.

**monitor-receiver:** on prod, publish the port the diode's `CHAT|MONITOR` route points at.

**diode-sim (dev only):** `ROUTES_FILE` (`/app/routes.conf` = `high/diode-sim/routes.conf`). Comment the whole
service out on prod.

## Secrets: `high/.env`

Mode 600, gitignored; secrets only, never settings.

| Name | What | Prod |
|---|---|---|
| `RELAY_ASSIST_TOKEN` | the Analyst's read-only token for the console's API | made on prod: [monitoring.md](monitoring.md), "Setting it up on a new host" |
| `AI_KEY` | the main model's API key (was `NVIDIA_API_KEY`, still read) | empty for vLLM |
| `AI_ASK_KEY` | the Ask model's key | only if `ai-ask-*` is set and needs one |

Show which are set without showing them: `sed -E 's/=(.+)$/=<set>/; s/=$/=<empty>/' high/.env`.
