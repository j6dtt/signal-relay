# Prod deployment

The first install of the relay on prod, as it was done on 2026-10-21, step by step. Each step ends with a check (✔).
Background for each part: [high-side.md](high-side.md), [low-side.md](low-side.md), [tlscerts.md](tlscerts.md),
[register-account.md](register-account.md), [settings.md](settings.md), [monitoring.md](monitoring.md).

**Order:** prepare on dev → high side → low side → checks. The high side goes first, so the receivers are
listening before the first packet arrives.

```
1. dev:  images, certificates, a clean copy of the code
          │
          ▼
2. high: receivers, monitor-receiver, console, relay-assist
          │   (listening on the diode's ports)
          ▼
3. low:  signal-cli, subscriber(s), signal-monitor, container-facts
          │   (sending to the diode's input)
          ▼
4. checks on the console: boxes UP, a test message, Gotham, the Analyst
          │
          ▼
5. before go-live: the mss_stream failure test
```

## 0. Before you start

- [ ] Two prod hosts: a **low-side** host with internet (signal-cli reaches Signal's servers) and the air-gapped
      **high-side** host.
- [ ] The account the containers run as (`relay-user`). Prod uses the operator's own account (`id -u`:`id -g`);
      a system account is the other option ([settings.md](settings.md), "Choosing relay-user").
- [ ] The diode's routes: one per room, `CHAT|<ROOM>` → a port on the high host, and `CHAT|MONITOR` → another port.
- [ ] Prod's Signal number registered ([register-account.md](register-account.md)), its account data backed up.
- [ ] The `*.lab.int` certificate and key for the console page.
- [ ] The models, reachable from the high side:
  - Nemotron 3 Super on vLLM (no key): the main model;
  - GPT-5.4 through the LLM proxy (a key): Ask Analyst and How-to. Test it once with `curl` and note the exact
    model ID it takes (a long ID such as `ri.language-model-service..language-model.gpt-5-4`, not `gpt-5.4`).
- [ ] Your `mss_stream.py` (the Gotham hand-off), and each room's MSS stream URL.

## 1. On dev: make the files

**1a. The images as files:**
```
cd /apps/signal-relay/image
docker save -o signal-cli-0.14.9-r5.tar signal-cli:0.14.9-r5
docker save -o python-3.13-alp-u3.tar python:3.13-alp-u3
sha256sum signal-cli-0.14.9-r5.tar python-3.13-alp-u3.tar > images.tar.sha256
```

**1b. Prod's own CA and certificates,** outside the repository (never dev's CA: its key is in the public
repository):
```
cd /apps/signal-relay/image
export TLS_CA_DIR=$HOME/prod-ca TLS_CERTS=$HOME/prod-certs
./tlscerts init signal-cli.<number without +>
./tlscerts client signal-sub.<room>
./tlscerts client signal-monitor
./tlscerts show
```
- The server certificate always covers `signal` too, so `signal-url` stays `https://signal:8443`.
- One client certificate per subscriber. Several rooms: join them with dots, e.g. `signal-sub.relay.selftest`.
- With `TLS_CERTS` pointing elsewhere, `tlscerts` doesn't touch dev's `clients.conf`; prod's is written by hand (3d).
- Keep `$HOME/prod-ca` offline: it is only needed to make or renew certificates.

**1c. A clean copy of the code** (dev's `low/` and `high/` also hold dev's data: `relay.db`, logs, `.env`,
certificates; never copy those):
```
cd /apps/signal-relay && git archive --format=tar.gz -o $HOME/relay-prod.tar.gz HEAD low high
```

**1d. Carry:**
- **low host:** `relay-prod.tar.gz`, both image tars, `images.tar.sha256`, `$HOME/prod-certs/`, the account backup;
- **high host:** `relay-prod.tar.gz`, the python tar, `images.tar.sha256`, the lab.int certificate and key,
  `mss_stream.py`.

## 2. High side

On the high host. All commands from `high/` after 2a.

**2a. Unpack and load:**
```
tar xzf ~/relay-prod.tar.gz
sha256sum -c --ignore-missing ~/images.tar.sha256
docker load -i ~/python-3.13-alp-u3.tar
cd high
```
✔ `python-3.13-alp-u3.tar: OK`.

**2b. The settings block** at the top of `docker-compose.yml`:
```yaml
  relay-user:     &relay-user "<id -u>:<id -g>"
  listen-ip:      &listen-ip <the address the diode's routes point at>
  ai-url:         &ai-url http://<vllm host>:<port>/v1
  ai-model:       &ai-model <the name vLLM serves>
  ai-ask-url:     &ai-ask-url https://<proxy host>/api/v2/llm/proxy/openai/v1
  ai-ask-model:   &ai-ask-model <GPT-5.4's exact model ID>
  ai-timeout:     &ai-timeout "30"
  ai-total:       &ai-total "90"
  ai-reasoning:   &ai-reasoning low
  ai-auto-per-hour:     &ai-auto-per-hour "0"
  ai-auto-per-incident: &ai-auto-per-incident "0"
```
- `ai-ask-url` is the base: relay-assist adds `/chat/completions` itself.
- `ai-ask-*` empty (`""`) = Ask uses the main model too.
- Leave `ai-log summary`, `chat-view`, `chat-text`, `retention-days 30` as they are.

**2c. The services,** in the same file:
- **Receivers:** one block per prod room; delete dev's others. In each one:
  - `ROOMS`: the room, as in the diode's route;
  - `FORWARD_TO`: `Gotham Chat` (or `"off"`);
  - `MSS_URL`: uncomment it and set this room's MSS stream URL;
  - `ports:` uncomment it, with `published:` = the port this room's diode route points at.
- **monitor-receiver:** uncomment `ports:`, `published:` = the `CHAT|MONITOR` route's port.
- **diode-sim:** comment out the whole service (dev only).

✔ `docker compose config -q && docker compose config --services` lists your receivers, `monitor-receiver`,
`console`, `relay-assist`, and no `diode-sim`.

**2d. `my_forward` (Gotham):** in `receiver/cef_to_json.py`, uncomment `import mss_stream` and the
`""" Prod only """` block in `my_forward`; put `mss_stream.py` in `receiver/` (mounted as `/app`). Your
`mss_stream()` takes `(payload_str, url)`: each receiver passes its own `MSS_URL`.

**2e. The page certificate and the modes:**
```
cp <your>.crt console/certs/ssl.lab.int.crt
cp <your>.key console/certs/ssl.lab.int.key
chmod 700 logs data console/certs
chmod 600 console/certs/*.key
chmod -R a+rX assist receiver console monitor shared docs
```
Your own account owns everything already (it unpacked the files), so no `chown`. Folders 700: message content,
`relay.db` and the key are yours only; scripts readable: they are mounted read-only.

**2f. Start:**
```
docker compose --dry-run up -d
docker compose up -d
docker compose ps
```
✔ Every container `Up`. relay-assist logs "AI_URL, AI_MODEL and RELAY_TOKEN must be set": it has no token
yet (2h); it waits, it doesn't crash.

**2g. The first console user.** It asks for the password twice and shows nothing as you type (at least 12
characters):
```
docker exec -it signal-console python console_users.py add admin
```
(`--random` instead makes one and shows it once.) ✔ Log in at `https://<this host>.lab.int:8090`. The low side
shows as not reporting yet.

**2h. The Analyst's token and keys.** The console must be running: the token is made in its database.

1. Make the token:
   ```
   docker exec signal-console python console_users.py token add relay-assist
   ```
   It prints `Token (shown once, store it now): <token>`.
2. `vi .env`, with:
   ```
   RELAY_ASSIST_TOKEN=<the token>
   AI_ASK_KEY=<GPT-5.4's key>
   ```
   No `AI_KEY`: vLLM takes none (relay-assist then logs `(no key)`).
3. `chmod 600 .env && clear`.

Or in one line, never showing the token: [monitoring.md](monitoring.md), "Setting it up on a new host".

**2i. Apply and check:**
```
docker compose up -d --no-deps relay-assist
docker logs signal-relay-assist 2>&1 | tail -1
```
✔ `Answering on port 8092. Model … (no key)`. Ask Analyst "Is the relay OK now?": an answer, no red dot.

**2j. The names the main model's endpoint serves** (`ai-model` must be one of them):
```
docker exec signal-relay-assist python -c "import json,os,ssl,urllib.request; print([m['id'] for m in json.load(urllib.request.urlopen(os.environ['AI_URL'].rstrip('/') + '/models', context=ssl._create_unverified_context()))['data']])"
```

## 3. Low side

On the low host. All commands from `low/` after 3a.

**3a. Unpack and load:**
```
tar xzf ~/relay-prod.tar.gz && cd low
sha256sum -c --ignore-missing ~/images.tar.sha256
docker load -i ~/signal-cli-0.14.9-r5.tar
docker load -i ~/python-3.13-alp-u3.tar
```
✔ Both `OK`, both `Loaded image: …`.

**3b. Account data and certificates:**
```
cp -a <account backup>/. signal-data/
cp -r ~/prod-certs/signal-cli.<number>/. certs/signal/
cp -r ~/prod-certs/signal-monitor        certs/
cp -r ~/prod-certs/signal-sub.<room>     certs/
```

**3c. `.env`:**
```
echo 'SIGNAL_ACCOUNT=+<prod number>' > .env && chmod 600 .env
```

**3d. `gateway/clients.conf`:** replace dev's lines with one per subscriber certificate, plus the monitor:
```
signal-sub.<room>
signal-monitor  check
```

**3e. The settings block** at the top of `docker-compose.yml`:
```yaml
  relay-user:   &relay-user "<id -u>:<id -g>"
  diode-in:     &diode-in <the diode's input address>
  chat-port:    &chat-port <the diode's chat port>
  monitor-port: &monitor-port <the diode's monitoring port>
  docker-gid:   &docker-gid "<stat -c %g /var/run/docker.sock>"
```
Leave `signal-url https://signal:8443` and `message-log redacted`.

**3f. Subscribers:** one block per subscriber (delete dev's others); `container_name` and `ROUTES_FOR` the same
name; its own certificate:
```yaml
      - ./certs/signal-sub.<room>:/certs:ro
```

**3g. Group IDs,** read from the account data while signal-cli isn't running (`image/signalctl` isn't in the
copy):
```
. ./.env
docker run --rm --user "$(id -u):$(id -g)" --read-only --tmpfs /tmp:rw,exec,size=400m \
  -v "$PWD/signal-data:/data" signal-cli:0.14.9-r5 -a "$SIGNAL_ACCOUNT" listGroups
```
✔ One line per group: `Id: <ID> Name: <name> …`.

**3h. `routes.conf`:** replace dev's lines; columns subscriber, room, group ID:
```
signal-subscriber    <ROOM>    <group ID>
```
`<ROOM>` in capitals, the same as the receiver's `ROOMS` and the diode's `CHAT|<ROOM>` route.

**3i. Modes, check, start:**
```
chmod 700 signal-data certs certs/* logs
docker compose config -q && docker compose --dry-run up -d
docker compose up -d
docker compose ps
```
✔ Every container `Up`.

## 4. Check on the console

- [ ] Every box UP within 2 minutes; the top bar's problems tile at 0.
- [ ] The subscriber is connected and has its rooms:
      `docker logs signal-subscriber 2>&1 | grep -iE 'connected|room|route' | tail -5`
- [ ] A test message in a relayed group: it shows in its window (the dock at the bottom), the room marker counts it,
      its Gotham box's delivered count goes up and its bar stays green, and it arrives in that room's Gotham chat.
- [ ] Ask Analyst "Is the relay OK now?": an answer within ~30 s, no red dot on the button.
- [ ] Optional, the models on prod's data: `docker exec signal-relay-assist python relay_assist.py --compare > compare.txt`

## 5. Before go-live

- [ ] **my_forward / mss_stream:** point one receiver's `MSS_URL` at a wrong endpoint once (`up -d --no-deps` that
      receiver), send a message, and confirm its Gotham box's bar turns red; then put the URL back. If
      `mss_stream()` reports a failure without raising or returning False, a failed send shows green.
- [ ] Optional: the sim tests (`tools/sim/run setup`, then one test at a time; [sim-tests.md](sim-tests.md)).

## What we ran into (2026-10-21)

**Apply a change the right way:**
- Edited a **script** (`relay_assist.py`, `cef_to_json.py`): `docker compose restart <service>`. `up -d` does
  nothing when the compose file hasn't changed, so the old code keeps running.
- Edited the **compose file or `.env`**: `docker compose up -d --no-deps <service>`. `restart` doesn't read them.

**The Analyst** (the dot on Ask Analyst shows its last error; the details are in relay-assist's log):
```
docker logs --since 5m signal-relay-assist 2>&1 | tail -30 | fold -w 80
```

| You see | Cause | Fix |
|---|---|---|
| "Could not reach relay-assist (500, … unexpected mimetype …)" | relay-assist crashed on the question | its log has the traceback |
| `ClientTimeout.__init__() got an unexpected keyword argument 'ssl'` | a hand edit put `ssl=False` inside `ClientTimeout(...)` | it is in the code since bf79c4c: copy the current `relay_assist.py` |
| "the model's endpoint answered HTTP 400 … unsupported parameter max_tokens" | GPT-5 models refuse `max_tokens` | fixed in code: GPT-5/o-series get `max_completion_tokens`, no `temperature` |
| `JSONDecodeError: Invalid \escape` (once, then fine) | a stray `\_` or `\d` in the model's answer | fixed in code: kept as a backslash |
| HTTP 400 naming `model` | `ai-model`/`ai-ask-model` isn't a name the endpoint serves | 2j lists them; copy one exactly |
| "RELAY_TOKEN must be set" | no token in `.env` | 2h, then `up -d --no-deps relay-assist` |

**Certificates:** relay-assist doesn't check the model endpoints' certificates (`ssl=False` in `chat()`; prod's
aren't trusted by the container). The 2j check skips it too (`_create_unverified_context`).

**Filtering logs for secrets** (`grep -v -iE 'pass|pwd|key|token|secret'`) also hides traceback lines such as
`KeyError`. relay-assist never logs keys or tokens, so read its log unfiltered when looking for an error.

## Later: adding or removing a subscriber or receiver

[adding-subscribers-receivers.md](adding-subscribers-receivers.md): nothing else restarts.
