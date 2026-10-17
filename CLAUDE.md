# signal-relay: one-way Signal → CEF/UDP relay

Streams Signal group chats (one subscriber per group) to another platform through a **data diode**. Production target is Palantir
(not built yet); `playback` is a temporary stand-in.

## Layout: mirrors prod (since 2026-10-11)
Two separate compose projects, one per side of the diode. Each folder is copied as-is to its host(s).
- **`left/`** (project `signal-left`): signal-cli and the subscriber.
  - Contents: `signal-data/` (account keys only), `subscriber/`, `certs/{signal,subscriber}`, `logs/`, `README.md`.
- **`right/`** (project `signal-right`): the receiver and playback.
  - Contents: `receiver/`, `playback/` (and `playback/certs`, the lab.int key), `README.md`.
- **Not deployed:** `image/` (build, `tlscerts`, `signalctl`), `certs/ca/` (the CA and its key; never mounted), `docs/`, `backup/`,
  and `tools/` (dump.py, the raw-event viewer, plus a sample).
- **Monitoring path (user, 2026-10-15):** the HSG *is* the data diode. Left containers log via gelf → the left host's
  logship (`:9000`) → HSG → app-monitor. The right side is on app-monitor's own network: receiver and playback send
  gelf straight to app-monitor (UDP 12101 / TCP 12201), no logship or HSG. Working assumption (user's call): logship
  and the subscriber share one HSG flow, so a diode outage shows as left containers DOWN. No cross-diode heartbeat.
- **The only link between the projects is UDP.** The subscriber sends to `UDP_DEST=172.16.0.46`, and the right
  side publishes `172.16.0.46:6510/udp`. In prod these are the diode's input and output.
  - The projects share no Docker network and can't resolve each other's names. Tested.
- **No profiles, `.env` or overlay files** (the user's choice): the user switches setups by commenting
  services or options out in `left/docker-compose.yml`.
  - One host as shipped. signal-cli host: comment out `subscriber`, uncomment `ports:`. Subscriber host: comment out
    `signal`, set `SIGNAL_URL`. Details: `left/README.md`.
- Run `docker compose` from inside `left/` or `right/`. **Start `right/` first, then `left/`**, so the receiver is
  listening before packets arrive. Stop in the reverse order. One "Lost the connection" warning from the subscriber
  at start-up is normal: it starts a few seconds before signal-cli.
- The folder was renamed from `/apps/signal-api` on 2026-10-12. Paths inside `backup/` archives still say signal-api.
  The phone's linked device is still named "signal-api" (that name stays).
- Old single-file setup, data and env templates: `backup/before-left-right-20261011-*`
  (`project.tar`, `account-data.tar` taken with signal-cli stopped, and the old compose and env files).
  The old image's `legacy` rollback service was dropped. It's in the backed-up compose file.

## Status
**Sprint 1: complete (2026-10-11).**
- End-to-end relay: subscriber → CEF/UDP → receiver → playback browser.
- Oversized messages split to ≤1472 bytes. Loss, gaps and duplicates detected and shown as lost notices.
- Plain-English logging in every service.
- Browser playback with chat and terminal views (terminal is the default; each browser remembers its last choice), in UTC with date lines.
- Display names for mentions and quote authors.
- TCP scripts removed.

**Sprint 2: replace the signal-cli-rest-api container with an in-house image. In progress.**
- Done: `image/` builds `signal-cli:0.14.9` (193 MB), on distroless `cc-debian13` with no shell.
  - Trivy: 0 CRITICAL/HIGH.
  - The build verifies GPG signatures, SHA-256s and the libsignal hash, and uses a jlinked Debian JRE.
  - It swaps in Wire 6.3.0 (CVE-2026-45799) until upstream ships it.
  - Java starts from `java @/opt/signal-cli/java.args`.
  - Alpine was tested and rejected: Signal's libsignal is glibc-only and fails even with gcompat.
  - `image/signalctl` does link (QR), register/verify, set-pin, accounts and devices.
  - `image/signalctl` and `image/check-update` take the image from `image:` of the signal service in
    `left/docker-compose.yml` (since 2026-10-15), so an update changes only that line (plus the Dockerfile).
  - Updates: `image/check-update` (monthly, dev host): checks signal-cli, Wire, base digests and Trivy;
    `--prepare [--apply [--build]]` downloads and verifies a release, writes the Dockerfile ARGs (and new base digests),
    then builds `signal-cli:VERSION` (or `-rN` if taken) and scans it; no build if a manual Dockerfile edit is pending. Steps: "Updating" in `image/README.md`.
    Verified 2026-10-15: `--prepare 0.14.9` reproduces every pinned value.
  - See `image/README.md`.
  - Distribution: `image/signal-cli-0.14.9.tar` (+ `.sha256`, both gitignored), and Docker Hub
    `th0pham/signal-cli:0.14.9` (**public** repository, pushed 2026-10-12, digest `sha256:ccddd25b…8ff8d8`).
- **Switched over 2026-10-11** (the user chose the fast path: same account data, one image at a time).
  - Service `signal` (container `signal-cli`) runs `daemon --http` on `left/signal-data`, with no published port.
  - The subscriber uses `SIGNAL_MODE=signal-cli`: `/api/v1/events` plus JSON-RPC `listContacts`.
  - The message log is redacted: `MESSAGE_LOG=redacted|full|off` on the signal service (default redacted).
    - The image's launcher (`image/launcher/MessageLogLauncher.java`) runs signal-cli with `-o json` and turns each
      line into a plain-English summary.
    - Content is hidden: text, quotes, previews, captions, file names, polls, contacts, and any unknown field.
    - Phone numbers are shortened, e.g. `+1941…805`.
    - Self-test: `docker run --rm -i signal-cli:0.14.9 --summarize-stdin < events.jsonl`.
- **TLS between signal-cli and the subscriber, done 2026-10-11** (so they can run on different networks).
  - Mutual TLS gateway in the launcher (`image/launcher/TlsGateway.java`, `TLS_LISTEN`), enforcing TLS 1.3.
  - Only the `signal-subscriber` client certificate is accepted.
  - A read-only allowlist (events, check, RPC `listContacts`) means `send` is blocked even with a valid certificate.
  - signal-cli itself listens on `127.0.0.1:8080` inside its container.
  - Certificates: `image/tlscerts`, our own self-signed CA, all valid 5 years (until 2031-10-11).
    The CA is in `certs/ca/`, never deployed. The dev CA and its key are committed to the repo: uploaded
    2026-10-14 and kept by the user's choice (2026-10-15). **The repo is PUBLIC** (checked 2026-10-16; the 2026-10-15
    note wrongly said private), so anyone can issue certificates this CA's gateway accepts. Raised with the user.
    `left/certs/signal/` and `left/certs/subscriber/` hold the others.
    Dev server names (reissued 2026-10-13): `signal`, `ubt2`, `172.16.0.46`, `portal.baysidetech.net`.
  - **Prod's server certificate has only a CN, no alternative names.** Python then checks the CN, so `SIGNAL_URL`
    (and `dump.py`'s `SIGNAL_NAME`) must use exactly that CN, as a host name: a CN never matches an IP address.
    One-host prod only works if the CN is `signal`. Tested with a throwaway certificate on 2026-10-15.
  - This is the only mutual TLS link (user's decision, 2026-10-11). Receiver → playback 8091 is plain HTTP on the
    right project's network: they always run together. Old mTLS version: `backup/*.with-ingest-mtls`.
  - The subscriber saves the last handled event ID in `left/logs/signal-last-event-id` and resumes from it.
  - If signal-cli has restarted, it doesn't know that ID, so it resends all it has kept (up to 1000).
  - Loss case: those 1000 are in memory only, and signal-cli has already confirmed them to Signal's server. Messages
    that arrive while the subscriber is down are lost if signal-cli also restarts before it reconnects.
  - **One host or split:** switch by commenting out in `left/docker-compose.yml` (see Layout). When split,
    `SIGNAL_URL=https://<host>:<port>` must use a name in the server certificate.
    - Moving the subscriber: stop it, then copy `logs/signal-last-event-id`, `subscriber/` and `certs/subscriber/`.
    - Split mode was tested on this host (before the left/right layout) with the port published on `127.0.0.1:443`.
    - Note: host port 8443 is already used by another app on this host.
  - Backup of `./data` before the switch: `backup/data-before-signal-cli-20261011-031039Z` (+ `.sha256`).
- Pending:
  - Nothing open here. Live traffic through the left/right layout is confirmed: 70 real events in
    `left/logs/signal-udp-sent.jsonl` after 2026-10-11 22:06Z, the last on 2026-10-15 23:54Z (checked 2026-10-16).
  - Don't unlink "signal-api" (device 2) on the phone: signal-cli now runs as that same device, with its keys.
- Never run two signal-cli instances on the same account data: they compete for messages and corrupt sessions.
  Use a second linked device instead.
- `/tmp` must be tmpfs with `exec`: libsignal unpacks and loads its .so from there.
Sprint 1 review issues with the old image, all resolved by the switch:
- 8080 published with no auth: there's no published port now.
- `:latest` not pinned: the in-house image is pinned and verified.
- Websocket frames dropped for slow clients: SSE with Last-Event-ID instead.
- Not an issue but a reminder: the account keys moved from `./data` to `left/signal-data`, which still holds them.

**Registering a new number (prod service account, e.g. a landline):** runbook in `docs/register-account.md`.
- Covers the voice call and captcha, registration lock, joining the group by link, `-a` in `left/docker-compose.yml`, and an encrypted backup.
- `NUM` in `subscriber_udp.py` is used only by the old rest-api mode.
- **Prod (2026-10-14):** the new number is registered with signal-cli as its main device (device 1). Signal Desktop is
  linked as a second device: `signal` stopped, then `./image/signalctl cli -a NUM addDevice --uri 'sgnl://linkdevice?…'`.
  Desktop replaces its QR about every minute, and `addDevice` returns 404 for a stale link: convert and run at once.

**Reference doc:** `docs/signal-cli-reference.md` (+ PDF), since 2026-10-17, replacing `signal-cli-architecture.md`: the
signal-cli technical package for engineers and security reviewers, built around diagrams 01–05 and 08 in `docs/diagrams/`
(each `.drawio` + `.pdf` + `.png`; 06 receiver and 07 registration are kept out as relay/runbook material). No host names,
IPs, numbers or group IDs in it or the diagrams, so it can be shared. Includes the licensing reading (not legal advice).

**Executive document: "Signal Chat Relay Container Review".**
- Artifact (private): https://claude.ai/artifact/WQHmFEdcaaX4dfivhXhb1X. The HTML source is no longer in a scratchpad: re-read the artifact.
- PDF: `docs/Signal-Chat-Relay-Container-Review.pdf`, rendered with headless Chromium in a throwaway Debian container.
  The host has no PDF tool.
- Content:
  - Part 1 assesses the public `bbernhard/signal-cli-rest-api` image.
  - Part 2 proposes "an in-house Signal Chat Relay": build → test → deploy, 4–6 days, with the encrypted read-only link.
- Markdown copies (2026-10-15), linked from the top of `image/README.md`: Part 1 in `image/public-image-assessment.md`,
  Part 2 in `image/in-house-proposal.md`.

**Sprint 3: Palantir delivery in `deliver()`. Not started.**
- Target product undecided (Gotham Chat vs Foundry stream); questions pending with the Palantir contact.
- Generic chat record in the receiver: built 2026-10-15 as `Chat.record()` (see `my_forward` under Pipeline).

Not assigned to a sprint:
- One-to-one chat filtering (group-only today).
- A login on 8090.

## Hard constraints
- **One-way, always.** Nothing is ever sent back to Signal or to the sender. No acks, no resend.
  - Exception outside our control: signal-cli itself sends delivery receipts (the second tick) for every message, by
    default, with no option to turn them off in 0.14.9 (its man page). It also confirms messages to the server and
    refreshes keys. No content goes back, and the gateway blocks `send`. The user was told on 2026-10-15.
- **CEF over UDP is the only thing that crosses the diode.** Uses `comlibv3.string_to_cef` → `parse_string_in_cef`
  (JSON after the header, not key=value). This format is already proven through the diode in prod; don't switch
  to `data_to_cef` key=value.
- **Header:** `CEF:0|J6-DTT|CHAT|{i}|{n}|SIGNAL-RELAY|APP|`: vendor `J6-DTT`, product `CHAT`, `{i}/{n}` = part
  number / part count, Name always `SIGNAL-RELAY`, severity field `APP` (since 2026-10-15; before that
  `CEF:0|CENTCOM|J6-DTT|{i}|{n}|SIGNAL|DEV|`). The receiver checks only the Name: subscriber `HEADER` and receiver
  `TAG` must match.
- **One CEF line per datagram, max 1472 bytes** (`MAX_DGRAM`, MTU 1500 − 28). We split in the app so IP never
  fragments and lost parts are logged. (dataminr sends 4 KB datagrams through the same diode path, relying on
  IP fragmentation; we deliberately don't.)
- **JSON on the wire stays ASCII** (`json.dumps` default `ensure_ascii=True`): `parse_string_in_cef` uses
  `splitlines()`, which would also break on ` `/`\x85`.
- **Prod runs on a shared multi-container host: no host-level firewall or network changes.** They would break
  other apps. Security controls must be scoped to our own containers (per-container DNS, own network namespace,
  container options).
- **`comlibv3.py` is shared with other apps in /apps — don't edit it.** Note `string_to_cef` calls `exit()` on error
  (caught as `SystemExit` in the subscriber).

## Pipeline
```
left/  (project signal-left)
signal-cli (in-house image/, daemon --http 127.0.0.1:8080 + TLS gateway :8443)
  ═HTTPS mutual TLS, SSE /api/v1/events═▶ signal-subscriber  left/subscriber/subscriber_udp.py   group filter, names, split → CEF → UDP
  ─UDP to UDP_DEST:6510─────────────────── (prod: the data diode) ───────────────────
right/ (project signal-right, publishes 6510/udp)
  ▶ signal-receiver    right/receiver/cef_to_json.py      reassemble → Signal JSON → deliver()
  ─HTTP POST :8091─▶ signal-playback    right/playback/playback_web.py     browser chat on :8090 (stand-in for Palantir)
```
- **subscriber_udp.py:** reads the event stream (it resumes with Last-Event-ID) into an `asyncio.Queue`, then `sender()` sends paced (`PACE`).
  - Packet payload: `{"s":session,"q":seq,"m":msgid,"i":i,"n":n,"d":"<json slice>"}`.
  - Keeps a copy of each sent event in `logs/signal-udp-sent.jsonl`, with names added. Each line is wrapped as
    `{"s","m","event":{…}}`: unwrap `["event"]` before feeding it to `Chat` (unwrapped, nothing renders).
  - `GROUP` (compose setting, required) is the group ID it relays; several IDs can be comma-separated. With none set
    it refuses to start, so it never relays every chat by accident.
  - Different groups: one subscriber service per group, all reading the one signal-cli (commented `subscriber-2`
    in `left/docker-compose.yml`). Each needs its own container_name, GROUP, STATE_FILE and SENT_LOG; they share
    the certificate. Two concurrent streams tested 2026-10-12.
  - Playback shows all groups in one chat (the title lists them). The terminal view can tag each line with its
    group (`Chat(show_group=True)`); not turned on.
- **cef_to_json.py:** `Reassembler` handles duplicates `(s,q)`, gaps in `q`, a 30 s timeout, and lost notices.
  - `deliver()` POSTs to `PLAYBACK_URL`/event, or prints if unset.
  - Lost notices go to `/notice`, counters to `/stats`.
  - **The Palantir adapter goes in `deliver()`.**
  - **`my_forward(record)`** (2026-10-15): placeholder for the user's own forwarding code, called before
    `deliver()` with a **simple record** of every message the chat view shows (none for receipts/typing):
    `{"id","time","group","group_id","from","type","text","ref"}`, a fixed schema (user, 2026-10-15): every record
    has all eight keys, `ref` is `null` when there is nothing to point to. `text` is the terminal view's text after
    the name. `id` = `<sender uuid>:<sent timestamp>` (Signal's own message key); `ref` (replies, reactions,
    edits, deletes) = the `id` it points to, so the platform can link them even after a receiver restart.
    In the 82 real events, all 18 refs matched an earlier id.
    - Built by `chat_render.py`'s `Chat.record()` (a `Chat(color=False)` in the receiver; `render()` and
      `record()` share `_parts()`; render output verified unchanged on 82 real events and 13 synthetic ones).
      The receiver mounts `right/playback/chat_render.py`.
    - For now it appends to `RECORD_LOG` (`right/logs/signal-records.jsonl`, message content, gitignored).
    - **`time` is Signal's server clock** (`serverReceivedTimestamp`, via `Chat.shown_ms()`), since 2026-10-16;
      playback's terminal and bubble times use it too. The sender's own timestamp put reactions before their
      message: prod's Signal Desktop (relay account, device 2) runs ~18.5 s fast. `id`/`ref` and all matching
      keep the sender timestamp (Signal's key). Trade-off: a message sent while offline shows when it reached
      the server. Tested on 175 real records: 3 reactions were out of order before, 0 after; nothing else changed.
    - Errors in it are logged and don't stop playback. The user writes the real version themselves: an API-post
      version (queue, retries, OAuth2) was built, then rolled back at their request.
    - `Chat` remembers the last `MAX_MSGS` = 5000 messages (oldest dropped; it grew forever before 2026-10-15).
    - Limits: quotes and old text are cut to 40 characters (as in the browser); after a restart, reactions/edits
      to older messages say "(earlier message)" in `text` (`ref` still links them). `group` comes from `groupInfo.groupName`, which all 82 real
      events in the sent log carry (0.14.9); the first 8 characters of the ID are only a fallback.
- **playback_web.py + playback_web.html + chat_render.py:** display only, with no UDP/CEF or receiver imports
  (the receiver imports `chat_render.py`, not the other way round).
  - Two views (chat bubbles, terminal) on the same stream. Times and date lines are in UTC.
  - Ingest port 8091 is internal to Docker only, never published; 8090 is open to the network with no login.
  - 8090 is HTTPS (since 2026-10-11): `WEB_TLS_CERT`/`WEB_TLS_KEY` = `right/playback/certs/ssl.lab.int.{crt,key}`.
    - The certificate is `*.lab.int`, from the Lab Internal CA, valid until 2028-07-13, TLS 1.2+.
    - Browse to a `*.lab.int` name. Browsers need the Lab Internal CA installed.
    - Plain HTTP on 8090 no longer answers. The page switches to `wss://` on its own.
  - The page loads nothing from the internet. It uses `textContent` only, because message text is untrusted.
- **tools/dump.py:** the user's raw-event viewer (pretty-prints one group's events; `""` = all). Runs on the host, not in a
  container (user's choice, 2026-10-15), standard library only. Reads `/api/v1/events` through the TLS gateway with
  `left/certs/subscriber`: connects to the container's IP from `docker inspect signal-cli`, checks the certificate as
  `signal`. Split setup: `SIGNAL_ADDR=host:port SIGNAL_NAME=host`. Keeps no state and writes no file.

## Changes made to Signal's JSON (subscriber only)
Names come from JSON-RPC `listContacts` with `allRecipients` (rest-api mode: `/v1/contacts/{num}?all_recipients=true`). A per-uuid lookup fails for people who aren't saved
contacts, so the subscriber loads the full list and reloads it at most once every 60 s when it meets an unknown.
- Still unnamed after that (prod's new number, 2026-10-15: no contacts, group members' profiles never fetched, so
  mentions showed "@someone"): `Names.refresh()` calls `listContacts` with `recipient: [uuid]`, which makes signal-cli
  fetch that person's profile from Signal. Once per person per `PROFILE_RETRY` (600 s). Read-only, and allowed by
  the gateway (it checks only the method). A person whose profile has no name stays unnamed.
- `mentions[].name`: the display name replaces the number/uuid copy signal-cli puts there.
- `quote.authorName`: added (signal-cli has no name in quotes).

Everything else is forwarded as signal-cli sent it. If a lookup fails, the field is left unchanged.

## Working here
- **Containers run as the uid:gid set in `x-relay-user`** (an anchor at the top of each compose file). `signalctl` takes the same
  value as `RELAY_USER`, which defaults to whoever runs it (`id -u`:`id -g`); its data folder defaults to `left/signal-data` (both since 2026-10-14). The compose default is 1000:1000.
  - **Dev:** 1000 (tpham).
  - **Prod:** the user's own account. That was their decision on 2026-10-13, after hearing the trade-off that anything
    running as them can read the Signal keys. On prod, uid 1000 belongs to someone else, so they set their own `id -u`:`id -g` in both compose files and in `RELAY_USER`.
  - A system account (`useradd --system`) was the alternative offered. Running as another uid works without a rebuild:
    the image's `USER 1000:1000` is overridden by compose (tested with 2400 on 2026-10-13).
  - These must be owned by that user, mode 700: `left/signal-data`, `left/certs`, `left/logs`, `right/logs`, `right/playback/certs`.
    The scripts can be anyone's but must be world-readable. `certs/ca` is never mounted.
- Containers run `python:3.13-alp-u3`, with single-file read-only mounts. **After editing a script, use
  `docker compose up -d --force-recreate <service>`** (in `left/` or `right/`); a plain restart can keep the old file.
- `left/signal-data` holds only the Signal account keys (`data/`, `attachments`, `avatars`, `jsonrpc2.yml`).
  Keep scripts and certificates out of it: signal-cli mounts it read-write.
- Logs use `logging`, written in plain English for a non-developer reader ("Sent message #3 (reply from …)").
  Keep new log lines in that style.
- Test without real Signal traffic:
  - From inside `signal-subscriber`: exec the script source up to `try:\n    asyncio.run` (cutting at plain
    `asyncio.run` leaves a dangling `try:`) and call `split`/`string_to_cef`/`send_events_over_udp` (or `Names`).
    Sending the parts to `DEST` exercises the whole right side: receiver, `my_forward` records and playback.
    Afterwards delete `right/logs/signal-records.jsonl` and recreate receiver and playback.
  - Or POST events from `signal-receiver`.
    - The endpoint is `http://playback:8091/event`.
    - Easiest: exec `cef_to_json.py` up to `main()`, then call `post("/event", {...})`.
  - Read the browser state over `wss://127.0.0.1:8090/ws` (certificate check off) from inside `signal-playback` (aiohttp is in the image).
  - Recreate receiver/playback afterwards to clear test messages and counters.
