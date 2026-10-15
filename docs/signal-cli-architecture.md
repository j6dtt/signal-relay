# signal-cli: architecture and how it works

signal-cli is the program in the relay's `signal-cli` container that talks to Signal. This page explains what it is,
how it receives and decrypts messages, where it keeps the account, and how the subscriber reads from it. For how our
image is built and locked down, see `image/README.md`.

**`signal-cli` vs `signalctl`:** `signal-cli` is the Signal program, and it exists only inside the container. `image/signalctl`
is our host script that runs it in a throwaway container. A command shown here as `signal-cli -a … listGroups` is
typed on the host as `./image/signalctl cli -a … listGroups`, with signal-cli stopped (section 3).

Facts about signal-cli's behaviour come from its own manual (`man signal-cli`, `man signal-cli-jsonrpc`), which ships
inside the image under `/opt/signal-cli/man`. Version described: **0.14.9**.

## 1. What it is
- An **unofficial, open-source Signal client** with no screen: a command line and a background service.
  - Made by Sebastian Scheibner (AsamK) and contributors: https://github.com/AsamK/signal-cli (GPLv3, Java).
  - Signal (the company) neither makes nor supports it. "Official" in our docs means AsamK's signed release.
- Built for servers: alerts, bots and relays like ours. `bbernhard/signal-cli-rest-api`, which we used before, is a
  wrapper around it.
- To Signal's servers it's just another device on the account, like a phone or Signal Desktop.

## 2. The layers inside
```
┌─────────────────────────────────────────────────────────────────────────┐
│ signal-cli                    commands, daemon, JSON output, account DB │  signal-cli-*.jar, libsignal-cli-*.jar
├─────────────────────────────────────────────────────────────────────────┤
│ signal-service-java           Signal's server API: websocket, messages, │  signal-service-java, signal-network,
│ (Signal-Android's library,    groups, profiles, attachments             │  core-network, models … (2.15.3_unofficial)
│  as maintained for signal-cli)                                          │
├─────────────────────────────────────────────────────────────────────────┤
│ libsignal                     all the cryptography: Signal Protocol,    │  libsignal-client-0.103.0.jar
│ (Signal's own, Rust)          sealed sender, group credentials          │  (libsignal_jni_amd64.so inside)
├─────────────────────────────────────────────────────────────────────────┤
│ Java 25 runtime + SQLite + supporting libraries (Jackson, OkHttp, Kotlin, Micronaut, Bouncy Castle …)│
└─────────────────────────────────────────────────────────────────────────┘
```
- **signal-cli implements no encryption itself.** Everything cryptographic is in libsignal, which is Signal's own code
  and the same library the phone apps use. Our build checks it's byte-identical to Signal's release.
- signal-service-java holds the knowledge of Signal's server API. Its version tag says "unofficial" because it's
  the signal-cli-maintained build of Signal-Android's library.
- That's why signal-cli must be **Java**: Signal publishes libsignal for Java, Swift and TypeScript, not Python.

## 3. The account is a folder
Everything that makes this device *this device* lives in the data folder: `/data` in the container,
`left/signal-data` on the host.
```
left/signal-data/
├─ data/
│  ├─ accounts.json          which accounts are in this folder (number, internal id)
│  ├─ 109511                 the account file (JSON): number, device id, password, identity and profile keys
│  └─ 109511.d/
│     ├─ account.db          SQLite: sessions, pre-keys, contacts, groups, recipients
│     ├─ account.db-wal/-shm SQLite's write-ahead files (part of the database while running)
│     └─ storage-manifest    state of the account's encrypted storage on Signal's servers
├─ attachments/  avatars/    downloads (we turn these off; see section 7)
└─ jsonrpc2.yml              left over from the old rest-api image
```
- **Copying the folder copies the device; losing it loses the device.** That's why it's mode 700 and backed up
  only with signal-cli stopped.
- **The database changes with every message** (sessions move forward with each one, section 5). That's why **two
  signal-cli processes must never use the same folder**: they'd both move the same sessions and break them. signal-cli
  locks the account to stop this, which is also why you stop the daemon before running a one-off command such as
  `addDevice`.

### Main device or linked device
| | `register` + `verify` | `link` |
|---|---|---|
| Becomes | the **main device** (in place of a phone) | a **linked device** (like Signal Desktop) |
| Needs | the number, an SMS or voice code, usually a captcha | a main device that scans a QR code |
| In our setup | the new prod number (`docs/register-account.md`) | the old "signal-api" device on the test number |

A main device can link other devices with `addDevice --uri 'sgnl://linkdevice?…'` (how Signal Desktop was linked
to the prod number). A linked device gets messages from the moment it's linked, never earlier history.

## 4. Connecting to Signal
```
signal-cli ══ HTTPS / websocket, port 443, outbound only ══▶ chat.signal.org (and Signal's other servers)
```
- signal-cli connects **out**; nothing connects in. No port has to be opened towards the container.
- It signs in with the device credentials from the account file, then keeps a websocket open. Signal's server
  pushes messages down it as they arrive.
- Signal keeps a **separate queue for each device** on the account. Each device receives its own encrypted copy of
  every message, so the phone, Desktop and signal-cli don't take messages from each other.
- The same connection is used for everything else: fetching group state, contact profiles, and topping up the
  device's one-time keys (pre-keys) so others can start new sessions with it.

## 5. Receiving a message, step by step
```
 sender's phone ──encrypts for every device on our account──▶ Signal server queue (one per device)
                                                                   │
                                                                   ▼  pushed down the websocket
 signal-cli ① takes the envelope (encrypted, sender often hidden by "sealed sender")
            ② libsignal decrypts it with the session for that sender, then moves the session forward
            ③ confirms it to the server, which deletes its copy
            ④ sends a delivery receipt to the sender (the second tick) — default, see section 8
            ⑤ updates the database: session, contact's profile key, group state if it changed
            ⑥ outputs one event: plain text, or JSON with -o json / in daemon mode
```
- **Sessions:** the Signal Protocol moves the keys forward with every message (the "double ratchet"). A copy of an
  old database can't read new messages, and two processes on one database corrupt each other (section 3).
- **Trust:** the first identity key seen for a person is trusted automatically (trust on first use,
  `--trust-new-identities on-first-use`, the default). If a person's key later changes (for example, a new phone),
  the new key must be accepted with `signal-cli trust`. If a member's messages stop arriving after they change phones,
  check this first.
- **Your own messages:** a message sent from another of the account's devices arrives as a `syncMessage.sentMessage`,
  not a `dataMessage`. This was the cause of the `dump.py` "nothing returned" problem in October 2026.

### What an event looks like (trimmed)
```json
{"envelope": {
   "source": "+1813…", "sourceName": "…", "sourceDevice": 1, "timestamp": 1791514641191,
   "dataMessage": {
     "message": "…text…",
     "groupInfo": {"groupId": "zCV6AAXf4WH28L62kT/iMZ4m6Mrck6ZIwiJhHupA5Ck=", "type": "DELIVER"},
     "mentions": [], "quote": {…}
   }},
 "account": "+1941…"}
```
Other kinds use other keys in `envelope`: `syncMessage`, `receiptMessage`, `typingMessage`, `editMessage`,
reactions inside `dataMessage`, and so on.

## 6. Groups
- A group's state (name, members, admins, settings) is stored **encrypted on Signal's servers**. Each member device
  holds the group's master key; the server can't read the member list or the name.
- Members prove they belong with anonymous credentials (zero-knowledge proofs, from libsignal), so the server lets
  them in without learning who they are.
- signal-cli downloads and decrypts the state when it joins and whenever the revision changes, and keeps a copy in
  `account.db`.
- `groupInfo.groupId` in events is the group's **internal id**, a base64 string that ends in `=`. That's the value the
  subscriber's `GROUP` setting must match exactly.

## 7. Ways to run it
### One command at a time
```
signal-cli -a +15551234567 listGroups
signal-cli -a +15551234567 addDevice --uri 'sgnl://linkdevice?uuid=…&pub_key=…'
```
Each run loads the account, does one job and exits. It needs the account to itself (stop the daemon first).
`./image/signalctl cli …` runs these in a throwaway container.

### Daemon (how the relay runs it)
A long-running process that keeps the connection open and receives continuously. It can be controlled through any
of: JSON-RPC on stdin (`jsonRpc`), a Unix socket (`--socket`), TCP (`--tcp`), **HTTP (`--http`)**, or D-Bus. We use HTTP:

| Endpoint | What it does |
|---|---|
| `GET /api/v1/events` | **Server-Sent Events stream** of incoming messages. Stays open. |
| `POST /api/v1/rpc` | JSON-RPC: the same commands as the command line (`listContacts`, `listGroups`, `send` …), with parameters in camelCase. |
| `GET /api/v1/check` | 200 OK if the daemon is running. |

How the event stream behaves (from `man signal-cli-jsonrpc`):
- Each event has an **id**. signal-cli keeps the **last 1000 events in memory**.
- A client that reconnects with a `Last-Event-ID` header first gets the events after that id. Our subscriber saves the
  id in `left/logs/signal-last-event-id`.
- If the id is unknown (for example, signal-cli was restarted), **all kept events are sent again**.
- Events kept in memory are lost when signal-cli restarts. Messages it has already confirmed to Signal's server
  (section 5, step ③) aren't queued there any more, so a message that arrived while the subscriber was down *and*
  before a signal-cli restart is gone.
- Keep-alive messages every 15 s (`--sse-keepalive-interval`) let signal-cli notice when a client has gone.

### Our daemon command (`left/docker-compose.yml`)
```
-a +1… daemon --http 127.0.0.1:8080 --receive-mode on-start
       --ignore-attachments --ignore-stories --ignore-avatars --ignore-stickers
```
| Option | Meaning |
|---|---|
| `-a +1…` | Run only this account. |
| `--http 127.0.0.1:8080` | HTTP JSON-RPC and events, reachable only inside the container (no login of its own). |
| `--receive-mode on-start` | Start receiving at once, not when the first client connects (`on-connection`) or on request (`manual`). |
| `--ignore-attachments/-avatars/-stickers` | Don't download files; the event still says an attachment exists. |
| `--ignore-stories` | Don't receive stories at all. |

## 8. What signal-cli sends back
The relay is one-way across the diode, but signal-cli, like every Signal device, talks both ways with Signal:
| Sent | When | Can we stop it? |
|---|---|---|
| Confirmation to the server | every message received | No: without it the server keeps re-sending. |
| **Delivery receipts** (second tick) to the sender | every message received, **by default** (the manual: "in addition to the default delivery receipts") | Not with an option in 0.14.9's manual. |
| Read receipts | only with `--send-read-receipts` | We don't use it. |
| Pre-key top-ups, profile and group fetches | as needed | No: part of being a device. |
| Messages | only if someone calls `send` | Yes: the TLS gateway blocks every RPC except `listContacts`. |

So group members see that the relay's device **received** their message, but nothing they wrote is ever answered
and no content goes back.

## 9. What our image adds around it
```
java @java.args ─▶ relay.MessageLogLauncher ─┬─▶ signal-cli's own Main (unchanged)
                                             └─▶ TlsGateway :8443 ─▶ 127.0.0.1:8080   (when TLS_LISTEN is set)
```
- **MessageLogLauncher:** signal-cli normally prints every message, text included, to its log. The launcher replaces
  that with one redacted line per event (`MESSAGE_LOG=redacted|full|off`).
- **TlsGateway:** mutual TLS 1.3 in front of the HTTP service, accepting only the subscriber's certificate and only the
  read-only calls listed above.
- signal-cli's own code, and libsignal, are not modified. The only library change is the Wire runtime swap for
  CVE-2026-45799. Details: `image/README.md`.

## 10. Keeping it working
- **Update about monthly.** Signal changes its servers and stops serving clients that fall too far behind (roughly a
  few months). Most signal-cli releases simply follow those changes. Steps: "Updating" in `image/README.md`.
- **Rate limits and captchas** apply as for any phone: mostly when registering or sending a lot.
- **No calls.** signal-cli handles text, attachments, reactions, edits, groups and receipts, not voice or video.
- **Back up `left/signal-data` with signal-cli stopped,** so the SQLite database is consistent.
