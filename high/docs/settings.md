# Settings reference

Every setting, where it is, dev's value and what prod needs. Per-host settings are in the **settings block**
(`x-settings`) at the top of each `docker-compose.yml`; secrets in `high/.env`; per-container settings in each
container's block. After changing one, see [updating.md](updating.md) for what to restart.

## Low side: `low/docker-compose.yml`, settings block

| Setting | What it is | Dev | Prod |
|---|---|---|---|
| `relay-user` | the uid:gid every container runs as; `signal-data`, `certs`, `logs` must be owned by it | `"1000:1000"` | the operator's `id -u`:`id -g` (or a system account) |
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

| Setting | What it is | Dev | Prod |
|---|---|---|---|
| `relay-user` | as on the low side; `logs`, `data`, `console/certs` must be owned by it | `"1000:1000"` | the operator's `id -u`:`id -g` |
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

**Per receiver** (its block): `container_name`, `ROOMS` (the room(s) it serves), `FORWARD_TO` (the destination
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
