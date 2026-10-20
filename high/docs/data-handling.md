# Data handling: where message content is, how long, who can see it

For security reviewers and approvers. "Content" here means what people wrote: message text, quotes, captions, file
names, poll and contact contents. "Metadata" means who sent what kind of message, when, in which group, and its size.

## In one paragraph

Message content exists only in the places listed below: Signal's servers and signal-cli on the low side (as Signal requires),
the subscriber's sent log (a copy on the low host), the receivers' record logs and the console's database on the
high side, plus the hand-off to Gotham through `my_forward`. The monitoring route, the Analyst and every container's
log carry **no content**: only states, codes, counts and, in the logs, senders' display names. On the console,
content can be switched off entirely (`chat-view off`). Nothing is ever sent back to Signal or to a sender.

## Where content is

| Where | What | Kept | Readable by |
|---|---|---|---|
| Signal's servers | messages until signal-cli has received them | Signal's own rules | Signal (end-to-end encrypted) |
| signal-cli's memory (low) | the last 1000 events, for subscribers that reconnect | until it restarts | the relay's account (`relay-user`) on the low host |
| `low/signal-data/` | the account's keys and signal-cli's own database (contacts, groups); no message history, only, briefly, messages it couldn't decrypt yet | as long as the account | the relay's account, mode 700 |
| `low/logs/<subscriber>-udp-sent.jsonl` | **every sent event**, with names: for manual recovery | **forever** (until moved or deleted; `SENT_LOG=""` turns it off) | the relay's account, mode 700 |
| The diode | CEF over UDP, one way: the message JSON | not kept by the relay | the diode's operators |
| `high/logs/<room>-records.jsonl` | the simple record of every message (id, time, group, sender, type, text, ref), as given to `my_forward` | **forever** (until moved or deleted; `RECORD_LOG=""` turns it off) | the relay's account, mode 700 |
| `my_forward` → Gotham (prod) | the same record, sent to Gotham through MSS | Gotham's rules (out of our scope) | Gotham's users |
| `high/data/relay.db`, table `chat` | the console's chat history, if `chat-view` is `on` | `retention-days` (30), deleted hourly | the relay's account, mode 700; on the page, every logged-in console user |
| The console page | the group windows, if `chat-view` is `on`; `chat-text short` shows only each message's first 5 words | while the page is open | logged-in console users (local accounts, 12 h sessions) |
| Backups of `relay.db` | as `relay.db` | as you keep them | wherever you store them: treat them like the original |

**To hold the least content:** `chat-view: "off"` (the console counts messages but never stores or shows them),
`SENT_LOG: ""` on each subscriber, and `RECORD_LOG: ""` on each receiver (the record still goes to Gotham; it is just
not also written to a file).

## Where content is not

| What | Carries | Never |
|---|---|---|
| Monitoring (`CHAT\|MONITOR` route, signal-monitor, monitor-receiver) | states, fixed codes, counts, ages, message numbers | content, log text, names |
| The console's monitoring data (`mon_*` tables, incidents) | states, codes, counts; group names (to label the windows) | content |
| The Analyst (relay-assist) | monitoring data, the runbook, these documents, counts of messages per group per hour | content: its tools can't read chat |
| The Analyst's log (`ai_log` in `relay.db`) | the questions people typed, the answers, which tools ran | message content (unless someone types it into a question) |
| Container logs (`docker logs`) | plain-English lines, e.g. "Sent message #3 to RELAY (text message from <display name>, 709 bytes)"; signal-cli's lines show phone numbers shortened (`+1561…751`) | message text (signal-cli's message log is `redacted`; `full` would include text: never on prod) |
| The Analyst's model | the above monitoring data and documents | content. On prod it is a local endpoint; dev sends monitoring data to a free online API, so dev uses dev data only |

## Who can get at it

- **On the hosts:** whatever runs as the account in `relay-user` (on prod: the operator's own account) can read the
  Signal keys, the logs and `relay.db`. Folders are mode 700 for that account.
- **On the console:** local users only (`console_users.py`); passwords hashed (scrypt), 5 failed logins lock an
  address; every logged-in user sees every group window. Tokens (for the Analyst and tools) can only read the
  monitoring API: no chat.
- **Over the network:** the page is HTTPS only (TLS 1.2+, `*.lab.int` certificate); the receivers' and monitoring's
  internal port 8091 is on Docker's own network, never published.

## What goes back to Signal

Nothing the relay sends: it is one way. signal-cli itself, as any Signal device does, sends delivery receipts (the
second tick), confirms messages to Signal's server and refreshes its keys; there is no option to turn the receipts
off in signal-cli 0.14.9. Its TLS gateway blocks every command that could send a message.

## Deleting

| To delete | Do |
|---|---|
| The console's chat history | it goes by itself after `retention-days`; at once: stop the console, then delete `data/relay.db` (also loses users and monitoring history) |
| Questions to the Analyst | Clear in Ask Analyst (each tab) or in an incident's Ask box |
| Record and sent logs | stop the container, delete the file, start it ([operations.md](operations.md), What grows) |
| Container logs | remove the container (`docker compose up -d --force-recreate --no-deps <service>` starts a fresh one) |
| The account | unlink or deregister the number, then delete `low/signal-data/` ([register-account.md](register-account.md)) |
