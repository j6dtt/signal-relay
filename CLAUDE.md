# signal-relay: one-way Signal → CEF/UDP relay

Streams Signal group chats (one subscriber per group) to another platform through a **data diode**. Production target is Palantir
(not built yet); `playback` is a temporary stand-in.

## Layout: mirrors prod (since 2026-10-11)
Two separate compose projects, one per side of the diode. Each folder is copied as-is to its host(s).
- **`left/`** (project `signal-left`): signal-cli and the subscriber.
  - Contents: `signal-data/` (account keys only), `subscriber/`, `certs/{signal,subscriber}`, `logs/`, `README.md`.
- **`right/`** (project `signal-right`): the receiver and playback.
  - Contents: `receiver/`, `playback/` (and `playback/certs`, the lab.int key), `README.md`.
- **Not deployed:** `image/` (build, `tlscerts`, `signalctl`), `certs/ca/` (the CA key, offline), `docs/`, `backup/`,
  and `tools/` (dump.py, which reads the old rest-api websocket; obsolete with the in-house image, plus a sample).
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
  - See `image/README.md`.
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
    The CA is in `certs/ca/`, kept offline. `left/certs/signal/` and `left/certs/subscriber/` hold the others.
    Server names: `signal`, `ubt2`, `172.16.0.46`.
  - This is the only mutual TLS link (user's decision, 2026-10-11). Receiver → playback 8091 is plain HTTP on the
    right project's network: they always run together. Old mTLS version: `backup/*.with-ingest-mtls`.
  - The subscriber saves the last handled event ID in `left/logs/signal-last-event-id` and resumes from it.
  - If signal-cli has restarted, it doesn't know that ID, so it resends all it has kept (up to 1000).
  - **One host or split:** switch by commenting out in `left/docker-compose.yml` (see Layout). When split,
    `SIGNAL_URL=https://<host>:<port>` must use a name in the server certificate.
    - Moving the subscriber: stop it, then copy `logs/signal-last-event-id`, `subscriber/` and `certs/subscriber/`.
    - Split mode was tested on this host (before the left/right layout) with the port published on `127.0.0.1:443`.
    - Note: host port 8443 is already used by another app on this host.
  - Backup of `./data` before the switch: `backup/data-before-signal-cli-20261011-031039Z` (+ `.sha256`).
- Pending:
  - A live message through the left/right layout (started 2026-10-11 22:06Z); only synthetic UDP tested so far.
    Live messages already went through the in-house image (Wire 6.3.0) at 03:27Z and through the mutual-TLS link
    at 15:05Z on 2026-10-11 (`left/logs/signal-udp-sent.jsonl`).
  - Don't unlink "signal-api" (device 2) on the phone: signal-cli now runs as that same device, with its keys.
- Never run two signal-cli instances on the same account data: they compete for messages and corrupt sessions.
  Use a second linked device instead.
- `/tmp` must be tmpfs with `exec`: libsignal unpacks and loads its .so from there.
Sprint 1 review issues with the old image, all resolved by the switch:
- 8080 published with no auth: there's no published port now.
- `:latest` not pinned: the in-house image is pinned and verified.
- Websocket frames dropped for slow clients: SSE with Last-Event-ID instead.
- `./data` still holds the account keys.

**Registering a new number (prod service account, e.g. a landline):** runbook in `docs/register-account.md`.
- Covers the voice call and captcha, registration lock, joining the group by link, `-a` in `left/docker-compose.yml`, and an encrypted backup.
- `NUM` in `subscriber_udp.py` is used only by the old rest-api mode.

**Executive document: "Signal Chat Relay Container Review".**
- Artifact (private): https://claude.ai/artifact/WQHmFEdcaaX4dfivhXhb1X. Source and versions are in the session scratchpad.
- PDF: `docs/Signal-Chat-Relay-Container-Review.pdf`, rendered with headless Chromium in a throwaway Debian container.
  The host has no PDF tool.
- Content:
  - Part 1 assesses the public `bbernhard/signal-cli-rest-api` image.
  - Part 2 proposes "an in-house Signal Chat Relay": build → test → deploy, 4–6 days, with the encrypted read-only link.

**Sprint 3: Palantir delivery in `deliver()`. Not started.**
- Target product undecided (Gotham Chat vs Foundry stream); questions pending with the Palantir contact.
- Generic chat record in the receiver: proposed, not approved.

Not assigned to a sprint:
- One-to-one chat filtering (group-only today).
- A login on 8090.

## Hard constraints
- **One-way, always.** Nothing is ever sent back to Signal or to the sender. No acks, no resend.
- **CEF over UDP is the only thing that crosses the diode.** Uses `comlibv3.string_to_cef` → `parse_string_in_cef`
  (JSON after the header, not key=value). This format is already proven through the diode in prod; don't switch
  to `data_to_cef` key=value.
- **Header:** `CEF:0|CENTCOM|J6-DTT|{i}|{n}|SIGNAL|DEV|` — `{i}/{n}` = part number / part count; Name is always `SIGNAL`.
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
  - Keeps a copy of each sent event in `logs/signal-udp-sent.jsonl`, with names added.
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
- **playback_web.py + playback_web.html + chat_render.py:** display only, with no UDP/CEF or receiver imports.
  - Two views (chat bubbles, terminal) on the same stream. Times and date lines are in UTC.
  - Ingest port 8091 is internal to Docker only, never published; 8090 is open to the network with no login.
  - 8090 is HTTPS (since 2026-10-11): `WEB_TLS_CERT`/`WEB_TLS_KEY` = `right/playback/certs/ssl.lab.int.{crt,key}`.
    - The certificate is `*.lab.int`, from the Lab Internal CA, valid until 2028-07-13, TLS 1.2+.
    - Browse to a `*.lab.int` name. Browsers need the Lab Internal CA installed.
    - Plain HTTP on 8090 no longer answers. The page switches to `wss://` on its own.
  - The page loads nothing from the internet. It uses `textContent` only, because message text is untrusted.
- **tools/dump.py:** the user's raw-data viewer for the old rest-api websocket (pretty-prints events). Its output file is disabled (`LOG = None`).

## Changes made to Signal's JSON (subscriber only)
Names come from JSON-RPC `listContacts` with `allRecipients` (rest-api mode: `/v1/contacts/{num}?all_recipients=true`). A per-uuid lookup fails for people who aren't saved
contacts, so the subscriber loads the full list and reloads it at most once every 60 s when it meets an unknown.
- `mentions[].name`: the display name replaces the number/uuid copy signal-cli puts there.
- `quote.authorName`: added (signal-cli has no name in quotes).

Everything else is forwarded as signal-cli sent it. If a lookup fails, the field is left unchanged.

## Working here
- **Containers run as the uid:gid set in `x-relay-user`** (an anchor at the top of each compose file). `signalctl` takes the same
  value as `RELAY_USER`. The default is 1000:1000.
  - **Dev:** 1000 (tpham).
  - **Prod:** the user's own account. That was their decision on 2026-10-13, after hearing the trade-off that anything
    running as them can read the Signal keys. On prod, uid 1000 belongs to someone else, so they set their own `id -u`:`id -g` in both compose files and in `RELAY_USER`.
  - A system account (`useradd --system`) was the alternative offered. Running as another uid works without a rebuild:
    the image's `USER 1000:1000` is overridden by compose (tested with 2400 on 2026-10-13).
  - These must be owned by that user, mode 700: `left/signal-data`, `left/certs`, `left/logs`, `right/playback/certs`.
    The scripts can be anyone's but must be world-readable. `certs/ca` is never mounted.
- Containers run `python:3.13-alp-u3`, with single-file read-only mounts. **After editing a script, use
  `docker compose up -d --force-recreate <service>`** (in `left/` or `right/`); a plain restart can keep the old file.
- `left/signal-data` holds only the Signal account keys (`data/`, `attachments`, `avatars`, `jsonrpc2.yml`).
  Keep scripts and certificates out of it: signal-cli mounts it read-write.
- Logs use `logging`, written in plain English for a non-developer reader ("Sent message #3 (reply from …)").
  Keep new log lines in that style.
- Test without real Signal traffic:
  - From inside `signal-subscriber`: exec the script source up to `asyncio.run` and call `split`/`string_to_cef`/
    `send_events_over_udp` (or `Names`) directly.
  - Or POST events from `signal-receiver`.
    - The endpoint is `http://playback:8091/event`.
    - Easiest: exec `cef_to_json.py` up to `main()`, then call `post("/event", {...})`.
  - Read the browser state over `wss://127.0.0.1:8090/ws` (certificate check off) from inside `signal-playback` (aiohttp is in the image).
  - Recreate receiver/playback afterwards to clear test messages and counters.
