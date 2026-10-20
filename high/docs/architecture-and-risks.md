# Architecture, rules and risks

What the Signal Chat Relay is, the rules it must never break, the decisions behind it, and the risks and limits we
have assessed. The details of each part are in its own document (see [README.md](README.md)).

## What it does

It streams Signal group chats, one way, through a **data diode** to another platform. Prod's destination is
**Palantir Gotham Chat** (air-gapped), fed one simple JSON record per message by `my_forward`. Building the pipeline
into Gotham, and Gotham itself, are out of scope: our part ends at the record. Dev's destination is the console.

```
LOW SIDE (internet)                 DATA DIODE         HIGH SIDE (air-gapped)
Signal's servers                    one way only
  -> signal-cli (in-house image)      CEF over UDP      receiver per room (cef_to_json.py)
  -> subscriber(s): routes.conf  ===>  CHAT|<ROOM>  ===>   -> my_forward: the Gotham record
     one stream per room               CHAT|MONITOR        -> the console (live diagram, chat)
  signal-monitor + container-facts ==> (monitoring) ==> monitor-receiver -> the console
                                                         relay-assist (the Analyst)
```

- **Low side** (`low/`, one compose project): signal-cli (the only part that talks to Signal), one or more
  subscribers, signal-monitor (health of the low side), container-facts (read-only Docker facts). Details:
  [low-side.md](low-side.md).
- **High side** (`high/`, one compose project): one receiver per room, monitor-receiver, the console (port 8090, local
  login), relay-assist (the Analyst), and on dev only diode-sim. Details: [high-side.md](high-side.md).
- **Rooms:** each Gotham room has its own diode route `CHAT|<ROOM>` and its own receiver. Which Signal groups go to
  which room: `low/routes.conf`. Design: [room-routing-design.md](room-routing-design.md).
- **Monitoring:** the low side reports states only; the console turns every change into an event, so a lost packet
  can't lose an alert. Boxes show their own health, every connection has its own state, incidents are per problem.
  Details: [monitoring.md](monitoring.md); the reasons: [monitoring-redesign.md](monitoring-redesign.md).
- **signal-cli:** our own image, built and verified from the release ([image.md](image.md)); why not the public image:
  [public-image-assessment.md](public-image-assessment.md); the plan: [in-house-proposal.md](in-house-proposal.md);
  the technical reference for engineers and security reviewers: [signal-cli-reference.md](signal-cli-reference.md).

## Dev and prod: what differs

This host's copy is **dev**. Prod is set up from the same files; anything below marked dev is never prod's value.
Instructions in these documents are for prod unless they say dev.

| | Prod | Dev |
|---|---|---|
| Diode routing | the **real data diode**, its own routes file: one route per room, `CHAT\|<ROOM>` → `<listen-ip>:<port>`, and `CHAT\|MONITOR` → monitor-receiver's port. Adding a room = a route on the diode | **diode-sim** (dev only) stands in for it: `high/diode-sim/routes.conf`, `ROOM  signal-receiver-<name>:6510`. Comment diode-sim out on prod |
| Receivers' ports | each receiver **publishes** the port its diode route points at (`ports:`, on `listen-ip`), each a different port; monitor-receiver publishes the `CHAT\|MONITOR` port | none published: diode-sim reaches them on the project's network |
| Low side → diode | `diode-in` = the diode's input address, `chat-port` / `monitor-port` = its ports | `diode-in` is this host's own address, both ports 6510 (diode-sim) |
| Hosts | prod's own low-side host(s) and the air-gapped high-side host | one host (`ubt2`) runs both sides |
| The Analyst's model | a **local** endpoint (vLLM, Nemotron 3 Super); `AI_KEY` empty | NVIDIA's free online API (internet), with a key |
| Analyst settings | `ai-timeout` 30, `ai-total` 90, `ai-reasoning` low, `ai-auto-per-hour` / `-per-incident` 0 | slower free tier: 90 / 300, limits 20 / 3 |
| Certificates | **prod's own CA**; the server certificate's CN is the name in `SIGNAL_URL` | the dev CA, whose key is in the (public) repository |
| Container user | the operator's own `id -u`:`id -g` in `relay-user` | 1000:1000 |
| `my_forward` | the prod block sending to MSS (Gotham) | writes records to `high/logs/<room>-records.jsonl` |
| Destination | Gotham Chat | the console |
| Sim tests | only before go-live, with `tools/sim/run setup` (the helper image as a file) | any time, when asked |

## Rules that must never break

1. **One way, always.** Nothing is sent back to Signal or to a sender: no acknowledgements, no resends.
   - Outside our control: signal-cli itself sends delivery receipts (the second tick) for every message, and there is
     no option to turn them off in 0.14.9. It also confirms messages to Signal's server and refreshes keys. No content
     goes back, and the gateway blocks `send` even for a valid certificate.
2. **Only CEF over UDP crosses the diode:** one route per room, one for monitoring. JSON after the header, via
   `comlibv3.string_to_cef` (the format already proven through the diode in prod; never key=value).
3. **The header is required by the diode:** chat `CEF:0|CHAT|<ROOM>|{i}|{n}|SIGNAL-RELAY|APP|`, monitoring
   `CEF:0|CHAT|MONITOR|1|1|SIGNAL-RELAY-MON|MON|`. The diode routes on fields 1 and 2. Room names: capitals, digits,
   `-` and `_`; `MONITOR` is reserved.
4. **One CEF line per datagram, at most 1472 bytes** (MTU 1500 − 28), split in the app so IP never fragments and a
   lost part is noticed.
5. **JSON on the wire stays ASCII**, or the parser's line splitting breaks.
6. **No host firewall or network changes on prod:** it is a shared host; controls stay inside our own containers.
7. **`comlibv3.py` is shared with other apps:** never edited here.
8. **Never two signal-cli instances on one account's data:** they compete for messages and corrupt its sessions.
9. **No message content in monitoring or the Analyst:** only states, codes and counts cross the monitoring route;
   the Analyst's tools never return chat text.

## Decisions and why

| Decision | Why |
|---|---|
| In-house signal-cli image instead of the public REST API image | the public image published port 8080 with no authentication, used `:latest`, and dropped frames for slow clients ([public-image-assessment.md](public-image-assessment.md)) |
| Mutual TLS 1.3 between signal-cli and each subscriber; a read-only allowlist (events, check, contacts) | the subscriber may run on another network; a stolen certificate still can't send |
| One client certificate per subscriber; `low/gateway/clients.conf` re-read | one leaked certificate can be locked out alone, with no restart |
| One receiver per room, routed by the diode on the header | each Gotham room separate; a fault in one room's receiver doesn't stop the others |
| Rules first, model second (the Analyst) | rules give the likely cause from facts; the model explains and ranks. In tests a model alone blamed the wrong part |
| Adding a subscriber or receiver restarts nothing else | other rooms keep flowing ([adding-subscribers-receivers.md](adding-subscribers-receivers.md)) |
| Prod high side air-gapped | the page and the Analyst load nothing from the internet; the model must be a local endpoint (vLLM) |
| Records use Signal's server time | a sender's clock can be wrong (prod's Desktop ran ~18.5 s fast: reactions came before their message) |

## Risks and limits we have assessed

**Message loss**
- signal-cli keeps the last 1000 events **in memory only**, and has already confirmed them to Signal's server. If the
  subscriber is away and signal-cli restarts before it reconnects, those messages are lost. The subscriber detects
  this (`CLI_RESTART_LOSS`, with the time window) from signal-cli's run start; a stopped signal-cli receives nothing,
  so time it was down doesn't count.
- If signal-cli itself is down, nothing is lost: new messages wait on Signal's servers.
- UDP through the diode has no resend (one way). Gaps and incomplete messages are detected by numbering and shown as
  "lost" notices; they can't be recovered.
- A diode outage of the monitoring route shows as the low side going silent (UNKNOWN), not as boxes DOWN.

**Security**
- **The dev CA and its key are in the repository, and the repository is public.** Anyone can issue a client
  certificate dev's gateway would accept, if they also know an allowed name. Prod must use **its own CA**, never
  dev's.
- There is no certificate revocation list: a certificate is cut off by removing its name from
  `low/gateway/clients.conf`, so every client needs its own name.
- prod's server certificate has only a CN (no alternative names): the subscriber's `SIGNAL_URL` must use exactly that
  name, never an IP address.
- Anything running as the account in `relay-user` can read the Signal account keys (`low/signal-data`). On prod that
  is the operator's own account, by choice.
- The console's page treats message text as untrusted (text only, never HTML).

**Operations**
- `my_forward` counts a send as delivered if it returns without an error. If the prod MSS call reports a failure some
  other way, a failed send would show green: check it on prod with a wrong endpoint before go-live.
- Prod has nothing deployed yet: [prod-deploy.md](prod-deploy.md).
- The dev AI model is a free online API, which can change without notice; prod's must be local.
- The console's database is SQLite: one console replica until it moves to Postgres (Kubernetes,
  [high-side.md](high-side.md)).
- Only group chats are relayed (no one-to-one chats).
- Monitoring can't see inside the diode, or past the hand-off to Gotham.

## Where to find things

[README.md](README.md) lists every document with one line on what it's for.
