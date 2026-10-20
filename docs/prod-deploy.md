# Prod deployment checklist

A first install of the relay on prod (nothing is deployed there yet). The details of each step are in
`low/README.md`, `high/README.md`, `image/tlscerts.md` and `docs/register-account.md`; this list puts them in order.

**Order:** prepare on dev → high side → low side (the receivers must listen before packets arrive).

## 0. Before you start

- [ ] The prod hosts: a **low-side** host with internet (signal-cli reaches Signal's servers) and an air-gapped
      **high-side** host, plus an account on each to run the containers as (`relay-user`).
- [ ] The diode's routes: one per room, `CHAT|<ROOM>` → a port on the high host, and `CHAT|MONITOR` → another port.
- [ ] Prod's Signal number registered (`docs/register-account.md`), with its account data backed up.
- [ ] The `*.lab.int` certificate and key for the console page.
- [ ] The vLLM endpoint on the high side, serving Nemotron 3 Super with tool calling.

## 1. On dev: make the files

```
cd /apps/signal-relay/image
docker save -o signal-cli-0.14.9-r4.tar signal-cli:0.14.9-r4
docker save -o python-3.13-alp-u3.tar python:3.13-alp-u3
sha256sum signal-cli-0.14.9-r4.tar python-3.13-alp-u3.tar > images.tar.sha256
```

- [ ] Prod's TLS certificates: `image/tlscerts` for signal-cli's server certificate (a CN prod uses as a host name,
      `signal` for one host) and one client certificate **per subscriber** (named after its container:
      `signal-subscriber`, `signal-subscriber-2`, …) plus `signal-monitor` (`image/tlscerts.md`).
- [ ] Optional, for the sim tests: `tools/sim/run export` (makes `tools/sim/signal-sim-helper-1.tar`).
- [ ] Copy `low/` + both image tars + `images.tar.sha256` to the low host; `high/` + the python tar +
      `images.tar.sha256` to the high host.
      Images, keys and logs are never in git: copy them as files.

## 2. High side

On the high host, where the files were copied, then in `high/`:

```
sha256sum -c --ignore-missing images.tar.sha256
docker load -i python-3.13-alp-u3.tar
```

Settings block at the top of `docker-compose.yml`:

| Setting | Prod value |
|---|---|
| `relay-user` | your `id -u`:`id -g` |
| `listen-ip` | the address the diode's routes point at |
| `ai-url` / `ai-model` | your vLLM, e.g. `http://<vllm-host>:8000/v1` / the name it serves |
| `ai-ask-url` / `ai-ask-model` | GPT-5.4 only if it can be reached from the high side; else `""` |
| `ai-timeout` / `ai-total` | `"30"` / `"90"` |
| `ai-reasoning` | `low` |
| `ai-auto-per-hour` / `ai-auto-per-incident` | `"0"` / `"0"` (no limit) |
| `ai-log` | `summary` |

- [ ] One receiver block per room (`ROOMS`, own `FORWARD_TO`); **uncomment each receiver's `ports:`** with the port
      its diode route points at; **comment out `diode-sim`** (dev only); monitor-receiver publishes the `CHAT|MONITOR`
      port.
- [ ] `my_forward`: enable your prod block in `receiver/cef_to_json.py` and mount `mss_stream.py` next to it.
- [ ] Put the lab.int certificate and key in `console/certs/`, then set owner and mode (`high/README.md`, Deploying).
- [ ] Start: `docker compose up -d`
- [ ] First user: `docker exec -it signal-console python console_users.py add admin --random`
- [ ] The Analyst's token, written into `.env` without showing it: the command in `docs/monitoring.md`, "Setting it up
      on a new host". `.env` then holds `RELAY_ASSIST_TOKEN`; `AI_KEY` stays empty for vLLM; `AI_ASK_KEY` only for
      GPT-5.4. Then `docker compose up -d relay-assist`.
- [ ] Check the model name vLLM serves:

  ```
  docker exec signal-relay-assist python -c "import json,os,urllib.request; print([m['id'] for m in json.load(urllib.request.urlopen(os.environ['AI_URL'].rstrip('/') + '/models'))['data']])"
  ```

## 3. Low side

On the low host, where the files were copied, then in `low/`:

```
sha256sum -c --ignore-missing images.tar.sha256
docker load -i signal-cli-0.14.9-r4.tar
docker load -i python-3.13-alp-u3.tar
```

- [ ] Account data into `signal-data/`; certificates into `certs/signal`, `certs/signal-monitor`, and each subscriber's
      own (`certs/subscriber` for the first, `certs/<its name>/` for the others, each mounted in its own block).
- [ ] `TLS_CLIENT_NAMES` on the `signal` service: every subscriber's name and `signal-monitor`.
- [ ] Settings block: `relay-user`, `signal-image` (`signal-cli:0.14.9-r4`), `account`, `signal-url` (its host name
      must be the server certificate's CN), `diode-in`, `chat-port`, `monitor-port`, `message-log`.
- [ ] `routes.conf`: which groups go to which room (`low/README.md`, Groups and rooms). Get group IDs with
      `listGroups` while `signal` is stopped.
- [ ] container-facts' socket group (one line, `low/README.md`, Deploying).
- [ ] Owner and mode: `sudo chown -R <user>: signal-data certs logs && sudo chmod 700 signal-data certs certs/* logs`
- [ ] Start: `docker compose up -d`

## 4. Check on the console

- [ ] Every box UP within 2 minutes; the top bar's problems tile at 0.
- [ ] Send a test message in a relayed group: it shows in its window, the room marker counts it, its Gotham box's
      delivered count goes up and its bar is green.
- [ ] Ask Analyst "Is the relay OK now?": an answer within ~30 s, no red dot on the button.
- [ ] Compare the models on prod's data: `docker exec signal-relay-assist python relay_assist.py --compare > compare.txt`

## 5. Before go-live

- [ ] **my_forward / mss_stream:** point the prod block at a wrong endpoint once and confirm the Gotham box's bar turns
      red. If `mss_stream.mss_stream()` reports a failure without raising or returning False, a failed send shows green.
- [ ] Optional: the sim tests (`tools/sim/run setup`, then one test at a time; `tools/sim/README.md`).
