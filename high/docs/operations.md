# Operations: checks, files, backups, renewals

Day-to-day running of the relay on prod: what to look at, where everything lives, what grows, what to back up, and
which dates to watch. For a problem the page shows, ask the Analyst (How-to tab): it reads the runbook.

## Daily: the console

Open the console (`https://<high-side host>.lab.int:8090`). Everything is on one page:

- **Top bar:** *problems* should be 0. A red first tile "monitor last checked … ago" means the console itself has
  stopped checking: restart it (`cd high && docker compose restart console`).
- **The bell:** open incidents. Loss incidents ("messages were lost") stay until someone opens them: open each.
- **Messages this run** (top bar): sent → arrived. Amber with a count means messages were lost on the way.
- **Gotham boxes:** the bar on the left shows whether the last hand-off to `my_forward` worked (green) or failed
  (red). The box says nothing about Gotham Chat itself.
- **The timeline** under the diagram: the last 24 h of every box; dots are warnings and errors.

Nothing on the page is sent anywhere else (no e-mail or chat alerts: the high side is air-gapped), so someone must
look.

## Weekly

| Check | How |
|---|---|
| Disk on both hosts | signal-monitor's box shows the low host's disk; the console's box its own. Or `df -h` on each host |
| Docker's own logs | capped at 5 files of 10 MB per container (`x-logging` in each compose file); nothing to do |
| Certificate dates | see [Dates to watch](#dates-to-watch) |
| Backups taken | see [Backups](#backups) |
| Incidents of the week | Ask Analyst (The relay now): "Compare this week with last week." |

## Monthly (on dev)

- `image/check-update`: is there a newer signal-cli, Wire or base image, and does the image still scan clean
  ([image.md](image.md), "Updating"). A new image goes to prod as in [updating.md](updating.md).

## Where everything lives

**Low side** (`low/`):

| Path | Holds | Content? |
|---|---|---|
| `signal-data/` | the Signal account's keys and its local database (contacts, groups) | keys: **secret** |
| `certs/signal/`, `certs/<client>/` | TLS certificates and keys (signal-cli's, each subscriber's, signal-monitor's) | keys: secret |
| `gateway/clients.conf` | who may connect to signal-cli | no |
| `routes.conf` | which groups go to which room | group IDs |
| `logs/<subscriber>-last-event-id` | where each subscriber stopped (resumes from it) | no |
| `logs/<subscriber>-udp-sent.jsonl` (first subscriber: `signal-udp-sent.jsonl`) | a copy of every event it sent, with names | **message content** |

**High side** (`high/`):

| Path | Holds | Content? |
|---|---|---|
| `data/relay.db` (+ `-wal`, `-shm`) | the console's database: chat (if `chat-view` on), monitoring, incidents, the Analyst's log, users, sessions, tokens | **message content** (chat table) |
| `logs/<room>-records.jsonl` | the simple record of every message, as given to `my_forward` (while `RECORD_LOG` is set) | **message content** |
| `logs/<room>-progress.json` | how far each sender got; my_forward's counts | no |
| `console/certs/` | the page's lab.int certificate and key | key: secret |
| `.env` | secrets: `RELAY_ASSIST_TOKEN`, `AI_KEY`, `AI_ASK_KEY` | secret |
| `docs/` | these documents (read by the Analyst) | no |

**Docker's logs** (`docker logs <container>`): plain-English lines from every container. They name senders (display
names, shortened numbers) but never message text; signal-cli's message log is `redacted` unless `message-log` says
otherwise. Kept by Docker's `json-file` driver: at most 5 files of 10 MB per container, the oldest dropped
(`x-logging` in each compose file), and gone when the container is removed.

## What grows

| What | Grows by | Trimmed? |
|---|---|---|
| `relay.db` | chat, monitoring, incidents (a few MB a day on dev; it levels off after 30 days) | yes: everything older than `retention-days` (30) is deleted every hour; minute figures after 48 h |
| `relay.db-wal` | written changes | folded back into `relay.db` by SQLite itself |
| `logs/<room>-records.jsonl` | one line per message | **no**: grows forever |
| `logs/<subscriber>-udp-sent.jsonl` | one line per message | **no**: grows forever |
| Docker's logs | every log line | yes: at most 5 × 10 MB per container, the oldest dropped (`x-logging`) |

To trim a records or sent log: it is only a copy (the console and Gotham already have the messages). Stop its
container, move the file away (or delete it if you don't need it), start the container: a new file is made.

```
cd high && docker compose stop receiver-relay && mv logs/relay-records.jsonl /secure/place/ && docker compose start receiver-relay
```

## Backups

| What | Why | How |
|---|---|---|
| The Signal account | losing it means registering the number again | an encrypted backup of `signal-data/`: [register-account.md](register-account.md), "Back up the account" (signal stopped) |
| The CA (`certs/ca/`, on the machine where certificates are made) | needed to make or renew any certificate | copy it somewhere safe and offline; never onto the relay's hosts |
| `relay.db` | chat history (30 days), incident history, users | an online copy, no stop needed (below), then off the host |
| `.env`, `console/certs/`, `low/certs/` | secrets and certificates | copy to a safe place; they can also be made again (new token, new certificates) |
| `routes.conf`, `gateway/clients.conf`, both `docker-compose.yml` | the set-up | in git, plus prod's own settings blocks: keep a copy of prod's versions |

**`relay.db`, while the console runs** (SQLite's online backup: a consistent copy):

```
cd high
docker exec signal-console python -c "import sqlite3; sqlite3.connect('/data/relay.db').backup(sqlite3.connect('/data/relay-backup.db'))"
mv data/relay-backup.db /secure/place/relay-$(date -u +%F).db
```

It holds message content: store it like the original (mode 600, only the relay's account).

**Restoring `relay.db`:** stop the console, put the copy back, start it.

```
cd high && docker compose stop console
cp /secure/place/relay-<date>.db data/relay.db && rm -f data/relay.db-wal data/relay.db-shm
docker compose start console
```

## Dates to watch

| What | Until | Renew |
|---|---|---|
| The page's `*.lab.int` certificate | 2028-07-13 | from the Lab Internal CA; replace `console/certs/`, then `docker compose restart console` |
| signal-cli's server certificate and each client certificate | 5 years from when they were made (dev's: 2031) | `image/tlscerts server …` / `client NAME` ([tlscerts.md](tlscerts.md)); signal-cli's box and signal-monitor's box show "certificate N days left" and turn WARN 30 days before it expires |
| The CA | 5 years from `init` | a new CA means new certificates for everything: plan it ([tlscerts.md](tlscerts.md), "Start over with a new CA") |
| The console's users | passwords don't expire; sessions last 12 h | `console_users.py` ([high-side.md](high-side.md), Users) |

## When something is wrong

- **The page shows a problem:** open its incident (the bell, or the `#n ›` chip on the box). The drawer has the
  likely cause, what to check, and the Analyst's explanation. The How-to tab answers "what do I check for …?" from
  the runbook.
- **The page doesn't load:** check the containers on the high host: `cd high && docker compose ps`.
- **Nothing arrives from the low side:** the low host's containers (`cd low && docker compose ps`), then the diode.

## Open issue: messages late or missing at signal-cli (prod, 22 Oct 2026)

Not solved; waiting for it to happen again.

**What happened:** for a while, signal-cli on prod got no new group messages, although they were sent. Some came late,
some never came:
```
A sent ─▶ nothing in signal-cli's log
B sent ─▶ nothing
C sent ─▶ B arrives (late)
restart signal-cli ─▶ all normal again; A and C never arrived
```
Read receipts did arrive in that window (23:50, 01:23 UTC), so signal-cli's connection to Signal was up. The monitor
showed nothing specific: every check passed (connected, keep-alives answered, receive loop running).

**Ruled out:** a second signal-cli on the account (the restart would not have fixed it); the subscriber and the high
side (nothing reached signal-cli's log). Fixed on the way, not the cause: the subscriber's name lookups held up
messages (now looked up beside sending, `NAMES_SLOW`).

**If it happens again, before restarting anything** (the evidence goes with the restart):
1. Signal Desktop (the account's other linked device): does it show the missing messages? Yes = Signal delivered
   them, so the fault is in signal-cli; no = the sender's phone or Signal.
2. The sender's phone: long-press the message → Info: is the relay number "Delivered"?
3. Low host, from `low/`:
   ```
   docker logs --since 1h signal-cli 2>&1 | grep -E "WARN|ERROR|could not be read|Signal servers" | tail -30
   find signal-data/data -path '*msg-cache*' -type f | wc -l
   docker exec -i signal-sub-relaypoc python - < ../tools/rpctime.py
   docker stats --no-stream signal-cli
   ```
   Files in `msg-cache` = messages signal-cli received but did not process.
4. Note the times of the missing messages, then restart: `docker compose restart signal`.

**Monitoring to add once the cause is known:** a delay check (Signal's `serverReceivedTimestamp` vs arrival at the
subscriber: WARN over 60 s, DOWN over 10 min), and a warning for the pattern found.
