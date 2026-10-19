# signal-relay: one-way Signal → CEF/UDP relay

Streams Signal group chats (one subscriber per group) to another platform through a **data diode**.

## Dev vs prod (user, 2026-10-17)
- **Prod's final destination: Palantir Gotham Chat**, in an air-gapped environment, fed the **simple JSON record**
  (`my_forward`, schema under Pipeline). The Gotham pipeline and Gotham set-up are out of scope: we deliver the
  record. It **can't be replicated in dev**: nothing here can reach or mimic it.
- **Dev's final destination is the console.** Dev testing ends at the console and `high/logs/signal-records.jsonl`.
- **The console also runs in prod for now,** next to the Gotham feed, for backend checks and troubleshooting. Whether it
  stays in prod permanently is **not decided** (user, 2026-10-17): keep it deployable there, but don't treat it as
  permanent or as a stand-in to remove.
- This host is dev.

## Layout: mirrors prod (since 2026-10-11)
Two separate compose projects, one per side of the diode. Each folder is copied as-is to its host(s).
- **`low/`** (project `signal-low`): signal-cli and the subscriber.
  - Contents: `signal-data/` (account keys only), `subscriber/`, `monitor/` (signal-monitor),
    `certs/{signal,subscriber,signal-monitor}`, `logs/`, `README.md`.
- **`high/`** (project `signal-high`): the receiver and the console.
  - Contents: `receiver/`, `monitor/` (monitor-receiver), `console/` (and `console/certs`, the lab.int key),
    `data/` (relay.db: the console's SQLite database, message content), `README.md`.
- **Not deployed:** `image/` (build, `tlscerts`, `signalctl`), `certs/ca/` (the CA and its key; never mounted), `docs/`, `backup/`,
  and `tools/` (dump.py, the raw-event viewer, plus a sample).
- **Monitoring path (user, 2026-10-15):** the HSG *is* the data diode. Left containers log via gelf → the left host's
  logship (`:9000`) → HSG → app-monitor. The right side is on app-monitor's own network: receiver and the console send
  gelf straight to app-monitor (UDP 12101 / TCP 12201), no logship or HSG. Working assumption (user's call): logship
  and the subscriber share one HSG flow, so a diode outage shows as left containers DOWN. That is app-monitor's
  view; our own relay monitoring (2026-10-17, see "Relay monitoring") does cross the diode, on its own route (`CHAT|MONITOR`).
- **The only link between the projects is UDP.** The subscriber and signal-monitor send to the diode's input
  (low settings `diode-in` 172.16.0.46, `chat-port`/`monitor-port` 6510 on dev); the diode routes each packet by its
  CEF header fields 1+2 (`CHAT|<ROOM>` to that room's receiver, `CHAT|MONITOR` to monitor-receiver; the diode's
  own routes file, user 2026-10-18). Dev has no diode: `diode-sim` publishes 6510 and routes the same way. Prod:
  each receiver and monitor-receiver publishes the port its diode route points at, on `listen-ip`.
  - The projects share no Docker network and can't resolve each other's names. Tested.
- **No profiles, `.env` or overlay files** (the user's choice): the user switches setups by commenting
  services or options out in `low/docker-compose.yml`.
  - Exception (user, 2026-10-17): `high/.env` holds secrets only, not settings: `NVIDIA_API_KEY=` (dev AI key) and
    `RELAY_ASSIST_TOKEN=` (relay-assist's the console token).
    Gitignored, mode 600. Compose reads it for `${…}` substitution, so a compose line using `${NVIDIA_API_KEY}`
    would pick it up. Read by `tools/ai_probe.py`.
  - One host as shipped. signal-cli host: comment out `subscriber`, uncomment `ports:`. Subscriber host: comment out
    `signal`, set `SIGNAL_URL`. Details: `low/README.md`.
- Run `docker compose` from inside `low/` or `high/`. **Start `high/` first, then `low/`**, so the receiver is
  listening before packets arrive. Stop in the reverse order. One "Lost the connection" warning from the subscriber
  at start-up is normal: it starts a few seconds before signal-cli.
- The folder was renamed from `/apps/signal-api` on 2026-10-12. Paths inside `backup/` archives still say signal-api.
  The phone's linked device is still named "signal-api" (that name stays).
- Old single-file setup, data and env templates: `backup/before-left-right-20261011-*`
  (`project.tar`, `account-data.tar` taken with signal-cli stopped, and the old compose and env files).
  The old image's `legacy` rollback service was dropped. It's in the backed-up compose file.

## Status
**Sprint 1: complete (2026-10-11).**
- End-to-end relay: subscriber → CEF/UDP → receiver → the console browser.
- Oversized messages split to ≤1472 bytes. Loss, gaps and duplicates detected and shown as lost notices.
- Plain-English logging in every service.
- Browser the console with chat and terminal views (terminal is the default; each browser remembers its last choice), in UTC with date lines.
  Since 2026-10-17: one tab per group plus "All" (see Pipeline).
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
    `low/docker-compose.yml` (since 2026-10-15), so an update changes only that line (plus the Dockerfile).
  - Updates: `image/check-update` (monthly, dev host): checks signal-cli, Wire, base digests and Trivy;
    `--prepare [--apply [--build]]` downloads and verifies a release, writes the Dockerfile ARGs (and new base digests),
    then builds `signal-cli:VERSION` (or `-rN` if taken) and scans it; no build if a manual Dockerfile edit is pending. Steps: "Updating" in `image/README.md`.
    Verified 2026-10-15: `--prepare 0.14.9` reproduces every pinned value.
  - See `image/README.md`.
  - Distribution: `image/signal-cli-0.14.9.tar` (+ `.sha256`, both gitignored), and Docker Hub
    `th0pham/signal-cli:0.14.9` (**public** repository, pushed 2026-10-12, digest `sha256:ccddd25b…8ff8d8`).
  - Licensing: r2 (2026-10-18) ships notices in `/usr/share/doc/signal-cli-image/` (NOTICE + GPL-3.0, AGPL-3.0,
    Apache-2.0 from Debian base-files, qrencode/libpng copyright files). Still the user's call: a LICENSE for the repo
    (the launcher runs in signal-cli's JVM, so GPLv3) and publishing signal-cli/libsignal source next to the image.
- **Switched over 2026-10-11** (the user chose the fast path: same account data, one image at a time).
  - Service `signal` (container `signal-cli`) runs `daemon --http` on `low/signal-data`, with no published port.
  - The subscriber uses `SIGNAL_MODE=signal-cli`: `/api/v1/events` plus JSON-RPC `listContacts`.
  - The message log is redacted: `MESSAGE_LOG=redacted|full|off` on the signal service (default redacted).
    - The image's launcher (`image/launcher/MessageLogLauncher.java`) runs signal-cli with `-o json` and turns each
      line into a plain-English summary.
    - Content is hidden: text, quotes, previews, captions, file names, polls, contacts, and any unknown field.
    - Phone numbers are shortened, e.g. `+1941…805`.
    - Self-test: `docker run --rm -i signal-cli:0.14.9 --summarize-stdin < events.jsonl`.
- **TLS between signal-cli and the subscriber, done 2026-10-11** (so they can run on different networks).
  - Mutual TLS gateway in the launcher (`image/launcher/TlsGateway.java`, `TLS_LISTEN`), enforcing TLS 1.3.
  - Only client certificates whose CN is in `TLS_CLIENT_NAMES` are accepted (comma-separated; default
    `signal-subscriber`). `tlscerts client NAME` (2026-10-17) makes one per host/tool in `low/certs/NAME/`; removing a
    name from the list locks that client out (tested), the only revocation there is.
  - A read-only allowlist (events, check, RPC `listContacts`) means `send` is blocked even with a valid certificate.
  - signal-cli itself listens on `127.0.0.1:8080` inside its container.
  - Certificates: `image/tlscerts` (guide: `image/tlscerts.md`), our own self-signed CA, all valid 5 years (until 2031-10-11).
    Since 2026-10-17 it checks every name first and refuses URLs, ports, bad IPv4 and bad host names before touching
    any file; a failed run leaves the old pair as it was.
    The CA is in `certs/ca/`, never deployed. The dev CA and its key are committed to the repo: uploaded
    2026-10-14 and kept by the user's choice (2026-10-15). **The repo is PUBLIC** (checked 2026-10-16; the 2026-10-15
    note wrongly said private), so anyone can issue certificates this CA's gateway accepts. Raised with the user.
    `low/certs/signal/` and `low/certs/subscriber/` hold the others.
    Dev server names (reissued 2026-10-13): `signal`, `ubt2`, `172.16.0.46`, `portal.baysidetech.net`.
  - **Prod's server certificate has only a CN, no alternative names.** Python then checks the CN, so `SIGNAL_URL`
    (and `dump.py`'s `SIGNAL_NAME`) must use exactly that CN, as a host name: a CN never matches an IP address.
    One-host prod only works if the CN is `signal`. Tested with a throwaway certificate on 2026-10-15.
  - This is the only mutual TLS link (user's decision, 2026-10-11). Receiver → the console 8091 is plain HTTP on the
    right project's network: they always run together. Old mTLS version: `backup/*.with-ingest-mtls`.
  - The subscriber saves the last handled event ID in `low/logs/signal-last-event-id` and resumes from it.
  - If signal-cli has restarted, it doesn't know that ID, so it resends all it has kept (up to 1000).
  - Loss case: those 1000 are in memory only, and signal-cli has already confirmed them to Signal's server. Messages
    that arrive while the subscriber is down are lost if signal-cli also restarts before it reconnects.
  - **One host or split:** switch by commenting out in `low/docker-compose.yml` (see Layout). When split,
    `SIGNAL_URL=https://<host>:<port>` must use a name in the server certificate.
    - Moving the subscriber: stop it, then copy `logs/signal-last-event-id`, `subscriber/` and `certs/subscriber/`.
    - Split mode was tested on this host (before the left/right layout) with the port published on `127.0.0.1:443`.
    - Note: host port 8443 is already used by another app on this host.
  - Backup of `./data` before the switch: `backup/data-before-signal-cli-20261011-031039Z` (+ `.sha256`).
- Pending:
  - Nothing open here. Live traffic through the left/right layout is confirmed: 70 real events in
    `low/logs/signal-udp-sent.jsonl` after 2026-10-11 22:06Z, the last on 2026-10-15 23:54Z (checked 2026-10-16).
  - Don't unlink "signal-api" (device 2) on the phone: signal-cli now runs as that same device, with its keys.
- Never run two signal-cli instances on the same account data: they compete for messages and corrupt sessions.
  Use a second linked device instead.
- `/tmp` must be tmpfs with `exec`: libsignal unpacks and loads its .so from there.
Sprint 1 review issues with the old image, all resolved by the switch:
- 8080 published with no auth: there's no published port now.
- `:latest` not pinned: the in-house image is pinned and verified.
- Websocket frames dropped for slow clients: SSE with Last-Event-ID instead.
- Not an issue but a reminder: the account keys moved from `./data` to `low/signal-data`, which still holds them.

**Registering a new number (prod service account, e.g. a landline):** runbook in `docs/register-account.md`.
- Covers the voice call and captcha, registration lock, joining the group by link, `account` in `low/docker-compose.yml`'s settings block, and an encrypted backup.
- `NUM` in `subscriber_udp.py` is used only by the old rest-api mode.
- **Prod (2026-10-14):** the new number is registered with signal-cli as its main device (device 1). Signal Desktop is
  linked as a second device: `signal` stopped, then `./image/signalctl cli -a NUM addDevice --uri 'sgnl://linkdevice?…'`.
  Desktop replaces its QR about every minute, and `addDevice` returns 404 for a stale link: convert and run at once.
- New groups (2026-10-16): prod's number was invited to more groups and Desktop accepted them. Membership is per
  account, so signal-cli receives them with no change; relaying one only needs its ID in `low/routes.conf` (a room
  for it, with a receiver and a diode route). Get IDs with `listGroups` (signal stopped).

**Reference doc:** `docs/signal-cli-reference.md` (+ PDF), since 2026-10-17, replacing `signal-cli-architecture.md`: the
signal-cli technical package for engineers and security reviewers, built around diagrams 01–05 and 08 in `docs/diagrams/`
(each `.drawio` + `.pdf` + `.png`; 06 receiver and 07 registration are kept out as relay/runbook material). No host names,
IPs, numbers or group IDs in it or the diagrams, so it can be shared. Includes the licensing reading (not legal advice).
- **Rebuild the PDF:** `./docs/render/render.sh` (throwaway `debian:trixie-slim`: pandoc gfm → HTML, `move_figs.py` puts
  each diagram after its section's text, headless Chromium prints; each figure on its own A4-landscape page, ~20 pages).
- **Diagrams (draw.io):** edit the `.drawio` files directly (01 hand-written; 02–08 were generated by a scratchpad script
  that is gone). Export with `rlespinasse/drawio-desktop-headless:v1.74.0`: needs `--shm-size=1g` (Electron's renderer
  crashes without it) and `-u $(id -u):$(id -g) -e HOME=/tmp` (else root-owned files); `-x -f pdf --crop` and
  `-x -f png -s 2`, one file per run (a folder export wrote PNGs in place and skipped one). Always preview the PNG
  (Read) and fix overlapping labels/kinked arrows before sending. Style: same palette for the same part everywhere
  (blue signal-cli, green subscriber, purple right side, yellow data/keys, red diode/refused), legend for line styles.
- Not yet done: the reference's scan figures are "as of 11 Oct 2026"; rerun `check-update` before handing it out.

**Executive document: "Signal Chat Relay Container Review".**
- Artifact (private): https://claude.ai/artifact/WQHmFEdcaaX4dfivhXhb1X. The HTML source is no longer in a scratchpad: re-read the artifact.
- PDF: `docs/Signal-Chat-Relay-Container-Review.pdf`, rendered with headless Chromium in a throwaway Debian container.
  The host has no PDF tool.
- Content:
  - Part 1 assesses the public `bbernhard/signal-cli-rest-api` image.
  - Part 2 proposes "an in-house Signal Chat Relay": build → test → deploy, 4–6 days, with the encrypted read-only link.
- Markdown copies (2026-10-15), linked from the top of `image/README.md`: Part 1 in `image/public-image-assessment.md`,
  Part 2 in `image/in-house-proposal.md`.

**Palantir delivery: out of scope** (user, 2026-10-17). Prod's destination is Gotham Chat, but building the pipeline
into Gotham and setting up Gotham are not part of this project. **Our scope ends at the simple record** handed to
`my_forward` (`Chat.record()`, built 2026-10-15; see Pipeline). The user's prod-only MSS block there is their own
hand-off; don't design, build or plan anything past it.

**Signal Chat Relay Console (user, 2026-10-18): "playback" renamed** (it is now monitoring + chat + Analyst, not a
playback). Service `console`, container `signal-console`, folder `high/console`, files `console_web.py/.html`,
`console_login.html`, `console_users.py`; settings `CONSOLE_URL` (receiver, monitor-receiver), `CONSOLE_API`
(relay-assist); part key `high/console`. relay.db re-keyed (backup `backup/relay.db.before-console`; project
`backup/before-console-rename/`). Backup file names `backup/playback_web.html.before-*` keep the old name. Not yet
renamed: diagrams 01/06/09 and `docs/signal-cli-reference.md` (step 8 of `docs/room-routing-design.md`).

**Room routing (user, 2026-10-18): built and live on dev, steps 0-8 done 2026-10-19** (`docs/room-routing-design.md`; step 7 (2026-10-19): the Monitor diagram is the only page (tabs gone, h1 "Monitor"); one floating window per Signal group + "All" (`makeWin`/`openChat`, the `floating()` helper; open windows remembered per browser in `chatwins`; phone: one at a time; Escape closes), opened from the **dock** at the bottom (chip per group: delivery dot + unread), a **room marker** or its **wire** (menu of its groups, `roomMenu`), or `#g=<8 chars>`; **pulses** run along a group's room wires on each new message (`pulse`); figures on top of each window (`drawFigs`): per route `via CHAT|<ROOM> -> receiver · sent -> arrived` this run + last arrival, its rooms' lost messages (group unknown); the header's receiver totals line is gone; header: title "Signal Chat Relay Console" (user, 2026-10-19: in the diagram's colours, a slow green-magenta-red gradient and three hopping dots, still with prefers-reduced-motion; own line on phones; the wires' flow ignores it, since remote desktops turn it on, user 2026-10-19), no Log button (user: too many ways in; the Log opens from a box's ≡, a timeline lane or CHAT|MONITOR, and its part list has "all parts"); panels rounded in the terminal look too, and fainter (tints 5-7%, borders 22-25%); group windows: smaller text (13 px, terminal 12 px); **`chat-text`** (high settings block, `full` | `short`; console `CHAT_TEXT`, user 2026-10-19): short = each message's first 5 words then " …" (`Chat(words=5)` in chat_render.py `cut()`, also quoted text; the browser never gets more; records for my_forward never cut), noted on top of each window; pulses start at Signal servers (servers → signal-cli → subscriber → room → receiver → Gotham + console, 650 ms a leg); subscriber boxes have a **rooms** button instead of ≡ (user, 2026-10-19: `roomsb`, under the name: "2 rooms ▾" lists its rooms, sent this run, and their groups; a group opens its window; its Log: its timeline lane); **Rooms are shown by their real names** (user, 2026-10-19: never the CEF header `CHAT|…` or the short name): `monitor.room_titles()` = the Signal group names the subscribers send to each room ("Signal Relay", several joined with " + "), `MONITOR` = "Monitoring"; state `room_titles`, page `roomTitle()`; also in event texts, receiver boxes' rooms row, Gotham boxes, lost notices (console rewrites "for SELFTEST was lost"), runbook explains, and the Analyst's rules; short names stay only in config (routes.conf, ROOMS, diode routes); **Gotham Chat boxes** (user, 2026-10-19): one per receiver, level with it in the last column (`gothamTile`, drawn from the receiver's counters: `forwarded` = my_forward returned no error = delivered, `forward_failed`, `forward_slow`, new `forward_at`; UNKNOWN if the receiver is silent, DOWN on FORWARD_FAILED in 10 min, WARN on FORWARD_SLOW, IDLE before the first); amber, bolder (the relay's destination); the forwarding rows moved off the receiver box; the console box sits above the receivers (`minor`, dimmer) with thin wires up the gap (`bus`); my_forward's counts are kept in the receiver's progress file (`forward`), so they survive a restart; the receiver -> destination wire is the worse of the receiver's and the destination's state; **`FORWARD_TO`** per receiver, set in each receiver's own block (dev: RELAY `Gotham Chat`, SELFTEST `"off"`, user 2026-10-19; the `x-receiver-env` value is only the default for every receiver; default "Gotham Chat", the box's title; "off" = my_forward isn't called, no records, no destination box); **my_forward contract** (docstring): returns normally = delivered, raises or returns False = failed (FORWARD_FAILED); the caller also catches SystemExit; the user's prod MSS block is unchanged. **OPEN (user asked to be reminded, 2026-10-19): check how `mss_stream.mss_stream()` reports a failure** (raise / False / status / None) and make the prod block raise or return False on it, else a failed send shows green; steps in memory `mss-stream-check`; data: subscriber status `flow.g` = [[group ID start, room, sent this run, age]] (prefilled 0 for every routed group), passed by signal-monitor; receivers send `X-Relay-Session`/`X-Relay-Room` headers with each POST /event; console `monitor.arrived()` counts per session+group before the duplicate check (kv `mon_arrivals`), group names (kv `mon_groups`, also from history at start), state `groups` (`groups_summary()`); `CHAT_VIEW` (high settings `chat-view`, on|off): off = events counted, never stored or shown, nothing restored, windows show figures only (tested in a throwaway console); backups `backup/before-chatwin/`; step 5: console checks on subscribers (`ROOM_UNSERVED`: a room no receiver serves; `SUBSCRIBER_TWICE`: the same ROUTES_FOR live twice, from status `flow.rf`), kept as part `checks` and shown over an UP report (`monitor.shown()`, also for incidents and the page); `rooms_summary()` -> state `rooms` -> a slim one-line marker per room in the diode band (user, 2026-10-18: full tiles repeated the boxes): `CHAT|<ROOM>  sent -> arrived` + ✓/!/✕/·, coloured IDLE/UP/WARN/DOWN, details in its tooltip; wires meet a marker at its middle, wires subscriber -> room tile -> receiver, one grid row per room; the monitoring link is a slim `CHAT|MONITOR` marker on each host's signal-monitor row (`loss · delay`, click = its Log; its 24 h = the timeline's lane; the full box with bars is gone, user 2026-10-18); runbook entries ROOM_UNSERVED, SUBSCRIBER_TWICE; Analyst knows rooms; fixed: a stored `flow` held the room list as text (crashed the page once, 23:43), page websocket sends to closing pages no longer log errors; backups `backup/before-roomtiles/`; step 4: `low/routes.conf` (subscriber, room, groups; dev: Signal Relay -> RELAY, Signal-API-SelfTest -> SELFTEST, the other 3 groups no longer relayed) replaces `groups`/`GROUP`; subscriber `ROUTES_FOR`, refuses to start on any routes problem (incl. same group+room twice on any subscriber); one stream per room (own session, q, m, header `CEF:0|CHAT|<ROOM>|`); status `flow.rooms` = [[room, session, m, m_age]] passed through signal-monitor; console checks arrival per room (holders, else receivers serving the room, else all); backups `backup/before-routes/`. NOTE: CLAUDE.md's Pipeline text about GROUP is outdated until step 8; dev high side (user, 2026-10-18): two receivers, `receiver-relay` (ROOMS RELAY, container signal-receiver-relay, logs/relay-records.jsonl) and `receiver-selftest` (SELFTEST), from one `x-receiver` template, no published ports; DEV ONLY `diode-sim` (high/diode-sim/diode_sim.py, container signal-diode-sim) publishes 6510 and routes by CEF header fields 1+2 like the diode (ROUTES=ROOM=host:port; prod: comment it out, uncomment each receiver's ports); old `receiver` service/container removed; `ACCEPT_OLD_HEADER` no longer set; then (user, 2026-10-18) diode-sim commented out for the user's own diode simulator: receivers publish `listen-ip:6512` (RELAY) and `:6513` (SELFTEST); the user tried their own diode simulator (`cef_forwarder.diode`, host network, UDP 536; it drops sources not on its allowlist, and container sources are 172.24.x), then asked for diode-sim back: diode-sim on 6510 again, receivers unpublished; low settings `chat-port` (6510) = the diode's chat input port (subscriber `UDP_PORT: *chat-port`); step 6 done (user: "make it like prod"): monitoring header `CEF:0|CHAT|MONITOR|…` (signal-monitor), sent to the same diode input (low settings `monitor-port`, 6510 on dev), diode-sim route `MONITOR=monitor-receiver:6511`, monitor-receiver checks fields 1+2 (its `ACCEPT_OLD_HEADER` off) and publishes nothing on dev (prod: publish the port the diode's CHAT|MONITOR route points at); diagram wires labelled by route (`CHAT|RELAY`, `CHAT|MONITOR`); backups `backup/before-monhdr/`; monitoring 6511 still goes straight to monitor-receiver until step 6; step 3: receiver `ROOMS` (settings block `rooms`, dev `RELAY,SELFTEST` placeholders), room read from header field 2 by the receiver itself, a packet for another room (or `CHAT|MONITOR`) dropped and counted `ROOM_MISMATCH` (runbook entry), per-room counters `room_stats`, `sess_room`, lost notices name the room, refuses to start on a bad room name; `ACCEPT_OLD_HEADER: "1"` TEMPORARY (old `J6-DTT|CHAT` = room ""), remove in step 8; backups `backup/before-rooms/`; step 2: boxes are removed automatically 120 s after container-facts reports a container removed (unless it is back: compose recreate), or by hand with "Remove this box" in the Log window for a box silent over 1 h (`POST /api/monitor/forget`, logged-in users only; a signal-monitor/diode link removes the whole host); kv `mon_removed` makes signal-monitor's repeated "still down" reports be ignored, any live report brings a box back; backups `backup/before-remove/`; step 1: receivers are `high/receiver/<container name>`, named by the console from Docker's reverse DNS of the `/stats` sender, `RECEIVER_NAME` overrides; stats per receiver (`kv rx_stats`), summed in the status line; subscriber parts carry `to` = the receivers holding their session; backups `backup/before-dynrx/`, `backup/relay.db.before-dynrx`):
one receiver per Gotham room, chat header `CEF:0|CHAT|<ROOM>|…`, monitoring `CEF:0|CHAT|MONITOR|…` (the diode routes
on fields 1+2), `low/routes.conf`, Monitor as main page with room windows, `CHAT_VIEW=on|off`.

**Words on the page (user, 2026-10-18): "low side" (before the diode: the `low/` folder) and "high side" (after it:
`high/`); hosts by their real Docker host name (signal-monitor mounts `/etc/hostname`); each signal-cli by its
container name, found by signal-monitor (no settings: `SIGNAL_TARGETS=https://signal:8443`; container-facts gives each
container's compose `service`; subscribers by Docker DNS of their status packet's source; `MONITOR_NAME`/`RELAY_ID` only
for a subscriber on another host). Folders are `low/` and `high/` since 2026-10-18 (were `left/`, `right/`;
projects `signal-low`/`signal-high`, were signal-left/-right); the internal part keys too (`low/<host>/…`, `high/receiver`; incident scopes `low/<host>`, `high`), and relay.db was
re-keyed (backup `backup/relay.db.before-rekey`). A subscriber shows by its container name as is ("signal-subscriber"; "subscriber <name>" only when the name doesn't say
it). Was `left-1` / `relay-1` (placeholders); dev's saved history in
relay.db was rewritten to the new names the same day (backup: `backup/relay.db.before-rename`, message content).**

**High side on k8s (user, 2026-10-18): TLS ends at ingress-nginx; the diode reaches the receiver via a UDP
LoadBalancer/NodePort.** Code ready: the console `TRUST_PROXY=1` (X-Real-IP/X-Forwarded-*; Secure cookie, lockout per
real address; tested), SIGTERM stops receiver and the console at once (the console closes open pages first). Rest is
manifests: see `high/README.md`, "Moving to Kubernetes" (1 replica + Recreate; externalTrafficPolicy Local; MTU).

**The page = the live diagram (user, 2026-10-18/19):** one grid (columns 255 | 255 | 220 diode | 255 | 255, widened up to 1.5x when the window has width to spare (`widenDiagram`, user 2026-10-19), gaps 44 / rows 22, Signal servers +26 px below; fitted to width AND to the height between header and dock, `fitDiagram`, min 55%, centred; panels low/high tints 6%/5%, borders 30%; pulses run subscriber → room → receiver → Gotham + console, on their own layer (`svg.pulses`, re-found each frame, so redraws do not cut them)), low side
(light green) | diode (light magenta) | high side (light red), boxes and SVG wires coloured by state; scales to the
window. Events: a floating **Log** window (from a timeline lane or the Monitoring marker; no header button, no ≡ on
boxes) + a 24 h **timeline** under the diagram (`/api/monitor/timeline`). Full details: `docs/monitoring.md`, "What
you see on the page". Old pages: `backup/playback_web.html.before-diagram*`, `backup/before-log/`, `backup/before-chatwin/`.

**Relay monitoring, history and login: built 2026-10-17, live on dev.** Doc: `docs/monitoring.md` (the one place for monitoring; the READMEs only point to it). Design: `docs/diagrams/09-monitoring-design.pdf`.
Alongside app-monitor, not replacing it (user). Requirements in memory (relay-monitoring).
- Left: `signal-monitor` (one per host, `low/monitor/signal_monitor.py`) checks each signal-cli's `/api/v1/check`
  with its own cert (`tlscerts client signal-monitor`, in `TLS_CLIENT_NAMES`), hears each subscriber's status (UDP
  6520, every 10 s; `MONITOR_ADDR`), checks disk (`DISK_PATHS=/certs`: never mount
  `logs/`, it holds message content) and cert expiry; sends `hb`/`st`/`ev` as CEF to the diode's input, header
  `CEF:0|CHAT|MONITOR|1|1|SIGNAL-RELAY-MON|MON|`, one datagram each, `boot`+`seq` for loss counting.
  The left side reports **states only**; the console (`monitor.py`) turns every change into an event, so a lost packet
  can't lose an alert. Start-up: "not answering yet" is WARN for 60 s.
- Right: `monitor-receiver` (own container, `high/monitor/monitor_receiver.py`, no reassembly) → the console `/monitor`,
  plus its own `rx_hb`. Each receiver's `/stats` also goes every 30 s unchanged: its heartbeat.
- The console: SQLite `high/data/relay.db` (`store.py`, the only file with SQL: swap it for Postgres when moving to k8s;
  until then one console replica). Chat events/notices restored at start; same message twice (two accounts) kept once
  (`chat_key`); `RETENTION_DAYS=30`, hourly cleanup. Local login (`auth.py`: scrypt, 12 h session cookie, lock after 5
  failures; `console_users.py`; one `admin` user, created 2026-10-17, password given to the user once). The diagram
  (states UP/WARN/STALE 90 s/DOWN 3 min/UNKNOWN). Alerts: a bell next to "Live" (user, 2026-10-17): it lists the parts with an
  open problem (not UP), worst first; a part drops off when it recovers, so it doesn't repeat the Log. No
  desktop pop-ups (removed at the user's request). The dock's chips use `.badge`: the bell's is `.bbadge`. The
  Monitoring marker (diode link): STALE/DOWN from the last packet's age (`last_ms`) first, then loss. Old page: `backup/playback_web.html.before-bell`. `/api/monitor/state`
  and `/api/monitor/events` for tools and the later AI assistant.
- Gotchas found in testing: `Referrer-Policy: no-referrer` makes Chrome send `Origin: null` on the login POST (so it
  is `same-origin`); Python as PID 1 ignores SIGTERM (new scripts raise KeyboardInterrupt on it).
- Gateway per-client rules since r2: `TLS_CLIENT_ALLOW=signal-monitor=check` (tested: its cert gets 403 on events).

**AI troubleshooting assistant: steps 1 and 2 built and live on dev (2026-10-17), then reworked the same day (see
"Rework"); step 3 built 2026-10-18: the Ask box (in each incident's drawer, and **Ask Analyst** (button right of the bell,
next to the bell) for the relay as a whole: a floating window (drag by title bar, resize from the corner, place
remembered), Clear (deletes the box's questions from the log, for everyone, after a confirm; each incident's Ask box has one
too, which keeps its explain cards and wrap-ups), multi-turn (last 4 Q&As by id, read back from the log by the console);
the incident drawer floats the same way (one `floating()` helper; last clicked on top); old pages
`backup/*.before-askwin`, `.before-analyst`, `.before-multiturn`, `.before-floatdrawer`; relay-assist `/ask`, kind `ask`) and the automatic wrap-up when an incident that mattered closes
(`/wrapup`, kind `wrapup`, `WRAP_ALWAYS` causes; "Write the wrap-up" by hand). On the page it is called **the Analyst** everywhere (one name, user 2026-10-18). One tool loop (`run`) and one
`KNOWLEDGE` text shared by explain/ask/wrap-up. Old page: `backup/playback_web.html.before-ask`.**
Memory: ai-assistant.
- Right side only, own container `relay-assist` next to the console. Read-only tools over the console's monitoring API
  (state, events, link, incidents, runbook): no actions, no message content, no direct `relay.db` access. It only
  knows what crosses the diode.
- **Rules first, model second:** `monitor.py` groups events into incidents with a rule-based likely cause; the model
  explains, ranks and suggests checks. Reason: in the probe the model, asked why signal-cli is UNKNOWN, always looked
  at signal-cli instead of the silent signal-monitor.
- Steps: (1) incidents + likely cause, fixed error codes from signal-monitor, runbook file, service API tokens,
  all useful without AI; (2) `relay-assist` + "Explain", plus the Q&A log (moved here from step 1: nothing to log
  before there are questions); (3) Ask box, automatic summaries.
- **Step 1 as built** (details: `docs/monitoring.md`, "Incidents and likely causes", "Tokens for tools"):
  - `high/console/incidents.py`: one incident per scope (`right`, `low/<host>`); opens when a part leaves UP
    ("starting up" alone only after 90 s), closes after 60 s all clear; `diagnose()` = the rules, `PRIORITY` = order.
    Extra clue: `traffic_at` on `high/receiver` (chat packets still arriving) splits LEFT_SILENT from
    MONITOR_ONLY_SILENT. Tested: 12 rule cases with made-up states, plus open → recover → close end to end.
  - `high/console/runbook.json`: cause code → title, explain, checks (where + what). Texts only; mounted in the console.
  - Codes: signal-monitor sends `code` on `st` and `hb` (its change detection is now on the code, not the state);
    the subscriber sends `stream_code` (`stream_code()` maps aiohttp errors; check `ClientSSLError` before
    `ClientConnectorError`, its parent).
  - Tokens: `console_users.py token add|remove|list NAME`, table `api_tokens` (hashed); a token may only GET
    `/api/monitor/*` (page, ws, POST refused: tested). New: `/api/monitor/incidents`, `/incidents/<id>`, `/runbook`.
  - Page: (superseded by the rework below: bell rows + incident drawer).
  - Backups: `backup/*.before-incidents`.
- **Step 2 as built** (details: `docs/monitoring.md`, "AI assistant"): `high/assist/relay_assist.py`, container
  `signal-relay-assist`. Browser → the console `POST /api/assist/explain` → relay-assist `/explain` (port 8092, internal)
  → model with 4 read-only tools, which GET the console's **internal port 8091** with the token (ingest app now has a
  GET guard: token required; POSTs as before). The console saves the log entry (stays the only DB writer), publishes
  op `ai`. `POST /api/assist/rate`, `GET /api/assist/log`. Secrets via `high/.env` substitution:
  `AI_KEY=${NVIDIA_API_KEY:-}`, `RELAY_TOKEN=${RELAY_ASSIST_TOKEN:-}`. Setup per host: one command that makes the
  token and writes it into `.env` without showing it (docs/monitoring.md, "Setting it up"; tested on dev 2026-10-17).
  Gotchas: Nemotron Super needs `max_tokens` well above 900 (reasoning eats it; answers were cut off, now 4096 and a
  `finish_reason=length` check); told to copy runbook commands exactly (it wrote `up -d signal-cli`, the service is
  `signal`). Backups: `backup/*.before-incidents` (step 1 state).
- **Rework (user, 2026-10-17): "the AI explain doesn't provide anything the runbook didn't"; bell and Incidents box
  served the same purpose; Incidents took the whole screen.** Built and live on dev:
  - Layout: bell = one row per open incident (badge = open incidents); **incident drawer** (`#drawer`, opened from
    a bell row, a tile's `#n ›` chip, or an "Incident #n" Events row) = all details; Incidents box and the
    "Explain (AI)" cards removed. Old page: `backup/playback_web.html.before-drawer`.
  - 2026-10-18 (user: nobody would know a plain-looking log line is clickable): bell footer **Incident history ›**
    opens the list of all incidents in the drawer (`openHistory`, "‹ History" back); Events log incident lines
    get a visible `#n ›` chip and a **category** filter (`#evcat`) next to the level one. Old page:
    `backup/playback_web.html.before-history`.
  - AI job changed from "explain" to what rules can't do: JSON card `headline, verdict, cause_code, story, impact,
    risk, unusual, first_step, question, confidence, confidence_why, handover` (`parse_card`); domain knowledge in
    its instructions (1000 messages in signal-cli memory, loss if signal-cli restarts first, exit codes). New tools
    `get_history`, `get_metrics`, `get_chat_activity`; `get_state` carries container facts.
  - Asked **automatically** (`incidents.wants_ai`, the console `auto_ask`, max 20/h, once per cause, 3 per incident)
    when it can add something; WARN-only known causes get "Ask anyway".
  - Container facts: `low/facts/container_facts.py` (`signal-container-facts`, Docker socket :ro, `group_add` =
    socket gid, 988 on dev) answers only `GET /facts` with fixed fields for project `signal-low`; signal-monitor
    `FACTS_URL`, message `t:"cf"`, **news only** (user, 2026-10-17: no unnecessary data through the diode): one
    container per change, sent 3 times; a start summary (`start`, `run`); heartbeat `cfh` fingerprint
    (`facts_fingerprint`, same code both sides) → the console `facts_ok`. Quiet dev: 12 packets / 2 min (was 16).
    Terms (user asked): container-facts = the service; facts = its GET answer (left host only); cf message = what
    signal-monitor sends through the diode; the console `monitor._containers` makes CONTAINER_* events, `_attach` puts facts on
    parts; rules CONTAINER_OOM/RESTARTING/CRASHED/STOPPED (above CLI_*/SUB_* in PRIORITY).
  - Flapping guard (`monitor._flapping/_settle`: >10 changes in 10 min = one event, "settled" after 10 quiet min);
    minute figures `mon_metrics` (48 h); incidents keep their most serious cause ("now:" for milder ones).
  - Verified with a real `docker stop signal-subscriber`: auto answer in 43 s with impact (0.01 msg/h), risk, an
    unusual finding the rules missed, and a question. Found then: the subscriber ignored SIGTERM (docker stop killed
    it after 10 s, exit 137). Fixed 2026-10-18 (`stop_now` raises KeyboardInterrupt): docker stop now exits 0 at once.
    Also fixed 2026-10-18: `stream_code()` used `aiohttp` (imported only inside `receive_signal_cli`), so any lost
    connection crashed the subscriber with a NameError (exit 1, Docker restarts). Late `cf` repeats of older news
    faked start/stop events (now dropped on both sides). AI instructions: where messages wait (signal-cli down =
    Signal's servers, no loss; subscriber down = signal-cli memory), never say who started/stopped something,
    patterns only from get_history spanning >24 h, compose service vs container names; get_incident adds
    `now_utc`/`open_for_minutes` (the model got durations wrong).
  - **Popups are in-page modals, never the browser's** (user, 2026-10-18): every confirm or notice uses the `#modal`
  dialog (`confirmBox`, `noticeBox`, `modalBox`); no `confirm()`, `alert()` or `prompt()`.
- Gotcha: a doubled line from a scripted edit broke the whole page script; check page JS with
    `node --check` (throwaway `node:22-alpine`) after big edits.
- **Step 2 plan, Q&A log** (user, 2026-10-17), as built: table in `relay.db`, written by `relay-assist`, 30 days. Per entry:
  who, when, kind (explain/ask), incident, model, time, question, tool calls with a one-line result each, answer,
  whether it agreed with the rules' cause, rating (helpful / not), errors. Never keys or message content.
  - `AI_LOG=summary|full|off` on `relay-assist` (default `summary` = prod; dev uncomments `full`: also the exact
    prompt and raw tool data). Each entry records the level it was saved at.
  - In `summary`, the full prompt is still **kept automatically** when: the answer is rated not helpful, the AI's
    cause disagrees with the rules', or the call errored / timed out / made an invalid tool call. Because ratings
    come later, every full prompt is held for 24 h, then dropped unless one of these applies.
- **Prod models (user, 2026-10-17), both OpenAI-compatible, on the right side:** Nemotron 3 Super on vLLM with tool
  calling, **no API key**; OpenAI GPT-5.4, **needs an API key**. So the assistant takes endpoint URL, model and an
  optional key as settings.
- **Dev:** NVIDIA build free API, `nvidia/nemotron-3-super-120b-a12b`, key `NVIDIA_API_KEY=` in `high/.env`.
  `tools/ai_probe.py` (stdlib) measures reliability and tool calling: Super 10/10, median 4.3 s; Ultra 2/5 (HTTP
  500s). OpenRouter's free Nemotron is the fallback. Free tiers may train on prompts: dev monitoring data only.

**Application monitoring: built 2026-10-18 (all 3 phases), live on dev, not committed.** User: "application failures
detection is higher priority than Docker"; triggered by a test where signal-cli lost Signal's servers while every tile
stayed UP. Doc: `docs/monitoring.md`, "Application problems" and "Signal servers (image r2)". Backups:
`backup/*.before-appmon`.
- Three layers per app (signal-cli, subscriber, receiver, the console): specific checks, a catch-all (every WARN/ERROR
  counted by fixed code, else `OTHER`; only codes/counts cross, never log text), worker liveness. Counts are
  cumulative (`p`); a counted problem keeps the part WARN/DOWN for 10 min (`Recent`, both sides; right-side windows
  saved with the part as `ups`). `active` = every code now, worst first. "1 more" events for repeats.
- Hop counting: signal-cli event IDs `<start ms>-<n>` are gapless (its `SseEventBuffer`), so the subscriber counts
  `EVENTS_MISSED` and `CLI_RESTART_LOSS` (window = lost touch → new run's start, over 20 s; the position file's mtime
  is "last in touch" after a subscriber restart, refreshed on a clean stop). Subscriber `flow` (`sess`, `m`, `m_age`)
  vs receiver `sess` → `CHAT_NOT_ARRIVING` (60 s) / `CHAT_LOST` (15 min). Receiver: `FORWARD_FAILED` (my_forward),
  `DELIVER_FAILED`, `RX_OVERFLOW` (/proc/net/snmp RcvbufErrors), watchdog thread → `RX_STALLED`.
- The console itself: guarded check steps (`CHECKS_FAILING`), `DB_ERROR` (store `Counted` wrapper; monitoring writes are
  `best_effort` so a full disk doesn't freeze the diagram), `INGEST_FAILED`, `AI_FAILED`, `LOOP_LAG`, disk.
  Page: `mon_alive` op every 10 s; >30 s without it → red "monitor last checked N s ago".
- **Image `signal-cli:0.14.9-r3`** (2026-10-19, low compose uses it; 0 CRITICAL/HIGH): r2 + the gateway counts only real refusals as `GATEWAY_REFUSED` (a broken-off handshake: logged "broke off", `gw.dropped`; found by tools/sim `docker.pause-signal-cli`).
- **Image `signal-cli:0.14.9-r2`** (only on dev, not exported or pushed:
  prod needs `docker save`/`load`). `image/launcher/SignalHealth.java`: logback listener in signal-cli's JVM; must
  wait for slf4j's INITIALIZATION_STATE (Main configures logging first); **signal-cli bridges Signal's library to slf4j
  only with --verbose** (Main → ManagerLogger.initLogger), so the launcher calls `SignalLogger.initLogger()` and filters
  `LibSignalService` off the console. Authenticated connection is named `normal` (vs `unidentified`). The gateway's
  `/api/v1/check` answers 200 + JSON (`sig`, `rx`, `jvm`, `gw`, `p`). New part `low/<host>/<inst>/signal-servers`;
  `SIGNAL_UNKNOWN` (WARN) when no connection event is seen, never a false DOWN. `.dockerignore` is an allowlist.
  Container facts match a subscriber's tile by container ID (`cid` = its host name; facts `id`; start summary
  `run: {name: [restarts, id]}`), so MONITOR_NAME need not equal the container name. The receiver keeps per-sender
  progress in `high/logs/receiver-progress.json` (`PROGRESS_FILE`; `sess_since`). `low/README.md` has the one-line
  `group_add` setter for a new host. Diagram 09 redrawn "as built" (Signal servers, container-facts, relay-assist).
  Verified live: block → WARN in 10 s, DOWN at 2 min, subscribers UP, reconnect 4 s after unblocking (39 s after a
  longer block: back-off). libsignal-net reports only `ChatServiceException: all connect attempts failed` (resets and
  drops alike) → `CONNECT_FAILED`; the full line goes to docker logs. **Hosts-file test: Signal's names → 192.0.2.1,
  signal-cli still connected in 1 s** (its own DNS/fallbacks): only a network-level block cuts it off.
- Incident #6 (receiver DOWN 00:59–01:04 on 18 Oct): the receiver really was stopped (its counters reset to 0 at
  01:04 = a new process); not a monitoring fault. Who stopped it is unknown (Docker's events were gone).
- Phase 3: container-facts adds mem/cpu/pids % of each container's own limits (one-shot stats); news only on a band
  change (85% mem/pids, CPU 90% for 5 min) or image change; part WARN `CONTAINER_*_HIGH`; `FACTS_UNAVAILABLE` on
  signal-monitor. Limits added: subscriber/receiver 256m, the console 512m.
- signal-monitor accepts `http://` targets (test rigs only). Subscriber `stream_code()` NameError fixed (it crashed on
  every lost connection). Text in states uses clock times, never "N s ago" (it changes on every check).
- Tested in a throwaway rig (fake signal-cli with fault controls + the real apps, `relayrig` project, removed after):
  every code; in-process checks 24/24; JVM test of SignalHealth with signal-cli's real logging setup; browser test
  with the console paused. 75 runbook causes.

Not assigned to a sprint:
- One-to-one chat filtering (group-only today).
- Prod Signal Desktop's clock is ~18.5 s fast (user told 2026-10-16 to turn on time sync); records no longer depend on it.

## Hard constraints
- **One-way, always.** Nothing is ever sent back to Signal or to the sender. No acks, no resend.
  - Exception outside our control: signal-cli itself sends delivery receipts (the second tick) for every message, by
    default, with no option to turn them off in 0.14.9 (its man page). It also confirms messages to the server and
    refreshes keys. No content goes back, and the gateway blocks `send`. The user was told on 2026-10-15.
- **CEF over UDP is the only thing that crosses the diode** (one route per chat room, one for monitoring). Uses `comlibv3.string_to_cef` → `parse_string_in_cef`
  (JSON after the header, not key=value). This format is already proven through the diode in prod; don't switch
  to `data_to_cef` key=value.
- **Header (REQUIRED by the diode, user 2026-10-18):** chat `CEF:0|CHAT|<ROOM>|{i}|{n}|SIGNAL-RELAY|APP|`, monitoring
  `CEF:0|CHAT|MONITOR|1|1|SIGNAL-RELAY-MON|MON|`. The diode routes on fields 1+2 only; the whole header is in
  capitals; room names `A-Z 0-9 - _`, `MONITOR` reserved, no `{ } | \` (comlibv3 fills `{i}/{n}` with `format()` and
  splits on `|` without escapes). `{i}/{n}` = part number / part count, Name `SIGNAL-RELAY` (receiver `TAG`), `APP`.
  The receiver reads field 2 itself (comlibv3's parser returns only the Name). History: `CEF:0|J6-DTT|CHAT|…` until
  2026-10-18 (no longer accepted), `CEF:0|CENTCOM|J6-DTT|…|SIGNAL|DEV|` before 2026-10-15.
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
low/   (project signal-low)
signal-cli (in-house image/, daemon --http 127.0.0.1:8080 + TLS gateway :8443)
  ═HTTPS mutual TLS, SSE /api/v1/events═▶ signal-subscriber  low/subscriber/subscriber_udp.py   routes.conf, names, split → CEF → UDP
  ─UDP, CEF:0|CHAT|<ROOM>|, to diode-in:chat-port ─ (prod: the data diode; dev: diode-sim on 6510) ─ by room ─
high/  (project signal-high)
  ▶ signal-receiver-<room>   high/receiver/cef_to_json.py   ROOMS, reassemble → Signal JSON → my_forward (FORWARD_TO) + deliver()
  ─HTTP POST :8091─▶ signal-console    high/console/console_web.py     the live diagram + group windows on :8090
```
- **subscriber_udp.py:** reads the event stream (it resumes with Last-Event-ID) into an `asyncio.Queue`, then `sender()` sends paced (`PACE`).
  - Header `CEF:0|CHAT|<ROOM>|{i}|{n}|SIGNAL-RELAY|APP|`; packet payload `{"s":session,"q":seq,"m":msgid,"i":i,"n":n,"d":"<json slice>"}`.
    One stream per room per run: its own session, `q`, `m` (no gaps from other rooms at a receiver).
  - **Groups and rooms: `low/routes.conf`** (since 2026-10-18; replaced `GROUP`/`groups`): `subscriber room groups`
    lines, the subscriber reads its `ROUTES_FOR` lines and refuses to start on any doubt (bad room, MONITOR, bad
    group ID, no lines, same group in the same room twice on any subscriber). A group in two rooms is sent twice.
  - Keeps a copy of each sent event in `logs/signal-udp-sent.jsonl`, with names added. Each line is wrapped as
    `{"s","m","room","event":{…}}`: unwrap `["event"]` before feeding it to `Chat` (unwrapped, nothing renders).
  - More subscribers: the commented `subscriber-2` block (own container_name, ROUTES_FOR, STATE_FILE, SENT_LOG; shared
    certificate), all reading the one signal-cli. Two concurrent streams tested 2026-10-12.
  - The console's page (since 2026-10-19): the diagram only; a floating window per group plus "All" (server side
    still `Rooms` → one `ChatView` per group, `HISTORY` 500, `MAX_ROOMS` 50, ops carry `room`); see the Words note.
- **cef_to_json.py:** one container per room (`ROOMS`); `Reassembler` reads the room from header field 2 (another room:
  dropped, ROOM_MISMATCH), handles duplicates `(s,q)`, gaps in `q`, a 30 s timeout, and lost notices.
  - `deliver()` POSTs to `CONSOLE_URL`/event (headers `X-Relay-Session`/`X-Relay-Room`), or prints if unset.
  - Lost notices go to `/notice`, counters to `/stats`.
  - `deliver()` feeds the console (dev and prod). The Gotham feed is `my_forward` (records), not `deliver()`.
  - **`my_forward(record)`** (2026-10-15): placeholder for the user's own forwarding code, called before
    `deliver()` with a **simple record** of every message the chat view shows (none for receipts/typing):
    `{"id","time","group","group_id","from","type","text","ref"}`, a fixed schema (user, 2026-10-15): every record
    has all eight keys, `ref` is `null` when there is nothing to point to. `text` is the terminal view's text after
    the name. `id` = `<sender uuid>:<sent timestamp>` (Signal's own message key); `ref` (replies, reactions,
    edits, deletes) = the `id` it points to, so the platform can link them even after a receiver restart.
    In the 82 real events, all 18 refs matched an earlier id.
    - Built by `chat_render.py`'s `Chat.record()` (a `Chat(color=False)` in the receiver; `render()` and
      `record()` share `_parts()`; render output verified unchanged on 82 real events and 13 synthetic ones).
      It lives in `high/shared/` (mounted at /shared in the receivers and the console).
    - For now it appends to `RECORD_LOG` (`high/logs/<room>-records.jsonl`, message content, gitignored).
    - Not called with `FORWARD_TO=off`. Returns normally = delivered; raises or returns False = failed (FORWARD_FAILED).
    - **`time` is Signal's server clock** (`serverReceivedTimestamp`, via `Chat.shown_ms()`), since 2026-10-16;
      the console's terminal and bubble times use it too. The sender's own timestamp put reactions before their
      message: prod's Signal Desktop (relay account, device 2) runs ~18.5 s fast. `id`/`ref` and all matching
      keep the sender timestamp (Signal's key). Trade-off: a message sent while offline shows when it reached
      the server. Tested on 175 real records: 3 reactions were out of order before, 0 after; nothing else changed.
    - Errors in it are logged and don't stop the console. The user writes the real version themselves: an API-post
      version (queue, retries, OAuth2) was built, then rolled back at their request.
    - **The user's prod version (2026-10-16), committed commented-out:** a `""" Prod only … """` block in `my_forward`
      sends `json.dumps({"records": [record]}, separators=(",", ":"))` with `mss_stream.mss_stream(payload_str)`, and a
      commented `#import mss_stream`. Their own module, not in the repo: when enabled it must be mounted next to
      `cef_to_json.py`. Two typos in it were fixed at their request (`separators=`, `log.info`).
    - `Chat` remembers the last `MAX_MSGS` = 5000 messages (oldest dropped; it grew forever before 2026-10-15).
    - Limits: quotes and old text are cut to 40 characters (as in the browser); after a restart, reactions/edits
      to older messages say "(earlier message)" in `text` (`ref` still links them). `group` comes from `groupInfo.groupName`, which all 82 real
      events in the sent log carry (0.14.9); the first 8 characters of the ID are only a fallback.
- **console_web.py + console_web.html + chat_render.py:** display and monitoring, with no UDP/CEF or receiver imports
  (the receiver imports `chat_render.py`, not the other way round).
  - Two views (chat bubbles, terminal) on the same stream. Times and date lines are in UTC.
  - Ingest port 8091 is internal to Docker only, never published; 8090 is open to the network, behind a local login.
  - 8090 is HTTPS (since 2026-10-11): `WEB_TLS_CERT`/`WEB_TLS_KEY` = `high/console/certs/ssl.lab.int.{crt,key}`.
    - The certificate is `*.lab.int`, from the Lab Internal CA, valid until 2028-07-13, TLS 1.2+.
    - Browse to a `*.lab.int` name. Browsers need the Lab Internal CA installed.
    - Plain HTTP on 8090 no longer answers. The page switches to `wss://` on its own.
  - The page loads nothing from the internet. It uses `textContent` only, because message text is untrusted.
- **tools/sim/** (2026-10-19): fault-simulation tests (dev, or prod before go-live): `tools/sim/run setup | list | test X | group a|b|c | all [--quick] | restore`. A runner container (stdlib, Docker Engine API over the socket, joins signal-high_default, reads the monitoring API with token `sim` in tools/sim/.token) acts only on signal-low/signal-high containers and its own (label `signal-sim=1`); network faults via `signal-sim-helper:1` in the target's own namespace (rules tagged `signal-sim`); hosts allowed in tools/sim/sim.conf; test subscribers send to 127.0.0.1:9. Split mode (prod, two hosts): the same command on both with `--at HH:MM` (UTC); role found from the projects on the host (`--side` to try it on one host); one slot per low-side fault (a `Fault` class: start/stop Docker only, during/after API only), low injects and holds, high checks, then runs the high-side tests; marker container `signal-sim-low-<plan>` seen via container-facts. Test subscribers are clones of the real one (image, env, ro mounts, network, user, limits). Token read from /sim/.token. Console: `signal-sim-*` boxes go 20 s after removal (`TEST_GONE_S`), their groups get no window. Not yet: hooks, rig. Instructions: tools/sim/README.md.
- **tools/signal-block** (2026-10-19, dev test tool): `start [drop|reject]` / `stop` / `status`: a helper container in signal-cli's own network namespace (`--network container:signal-cli`, NET_ADMIN) blocks its internet with iptables rules tagged `signal-block` (private and loopback addresses still pass); `stop` removes exactly those. Details and a timed test: `docs/monitoring.md`, Testing.
- **tools/dump.py:** the user's raw-event viewer (pretty-prints one group's events; `""` = all). Runs on the host, not in a
  container (user's choice, 2026-10-15), standard library only. Reads `/api/v1/events` through the TLS gateway with
  `low/certs/subscriber`: connects to the container's IP from `docker inspect signal-cli`, checks the certificate as
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
- **Containers run as the uid:gid set in `relay-user` (settings block)** (an anchor at the top of each compose file). `signalctl` takes the same
  value as `RELAY_USER`, which defaults to whoever runs it (`id -u`:`id -g`); its data folder defaults to `low/signal-data` (both since 2026-10-14). The compose default is 1000:1000.
  - **Dev:** 1000 (tpham).
  - **Prod:** the user's own account. That was their decision on 2026-10-13, after hearing the trade-off that anything
    running as them can read the Signal keys. On prod, uid 1000 belongs to someone else, so they set their own `id -u`:`id -g` in both compose files and in `RELAY_USER`.
  - A system account (`useradd --system`) was the alternative offered. Running as another uid works without a rebuild:
    the image's `USER 1000:1000` is overridden by compose (tested with 2400 on 2026-10-13).
  - These must be owned by that user, mode 700: `low/signal-data`, `low/certs`, `low/logs`, `high/logs`, `high/console/certs`.
    The scripts can be anyone's but must be world-readable. `certs/ca` is never mounted.
- Containers run `python:3.13-alp-u3`. Each app's folder is mounted read-only at /app, shared code (`comlibv3.py`,
  `chat_render.py`) from `shared/` at /shared (`PYTHONPATH`). **After editing a script: `docker compose restart <service>`**;
  after editing a compose file: `docker compose up -d` (recreates what changed). (Since 2026-10-18; before: single-file
  mounts that needed --force-recreate.)
- **Settings block (user, 2026-10-18):** everything that differs per host is in `x-settings` at the top of each compose
  file (YAML anchors: relay-user, signal-image, account, groups, signal-url, diode-in, docker-gid, message-log; high:
  relay-user, listen-ip, web-bind, web-cert/key, ai-url, ai-model, ai-log, retention-days). Secrets stay in high/.env.
- `low/signal-data` holds only the Signal account keys (`data/`, `attachments`, `avatars`, `jsonrpc2.yml`).
  Keep scripts and certificates out of it: signal-cli mounts it read-write.
- Logs use `logging`, written in plain English for a non-developer reader ("Sent message #3 (reply from …)").
  Keep new log lines in that style.
- Test without real Signal traffic:
  - From inside `signal-subscriber`: exec the script source up to `try:\n    asyncio.run` (cutting at plain
    `asyncio.run` leaves a dangling `try:`) and call `split`/`string_to_cef`/`send_events_over_udp` (or `Names`).
    Sending the parts to `DEST` exercises the whole right side: receiver, `my_forward` records and the console.
    Afterwards delete `high/logs/signal-records.jsonl` and recreate receiver and the console.
    **Since 2026-10-17 the console saves everything in `high/data/relay.db`**: test against a throwaway console (own
    container, spare ports, scratch `DB_PATH`), as done for the tabs and monitoring, not the live one.
  - Or POST events from `signal-receiver`.
    - The endpoint is `http://console:8091/event`.
    - Easiest: exec `cef_to_json.py` up to `main()`, then call `post("/event", {...})`.
  - Read the browser state over `wss://127.0.0.1:8090/ws` (certificate check off) from inside `signal-console` (aiohttp is in the image).
  - Recreating the console no longer clears test messages (they're in the database); use a throwaway console.
  - Browser tests: a throwaway `debian:trixie-slim` + chromium + python3-websocket image, driven over the DevTools
    protocol (`--network host`); log in through the form.
