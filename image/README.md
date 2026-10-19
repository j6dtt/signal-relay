# In-house signal-cli image

The official [signal-cli](https://github.com/AsamK/signal-cli) release, with Signal's own libsignal, on Google's
distroless base: glibc only, with no shell and no package manager. It replaces `bbernhard/signal-cli-rest-api`.
There's no third-party code and no web control interface.

Why we replaced it: [public-image-assessment.md](public-image-assessment.md) (the public image's review) and
[in-house-proposal.md](in-house-proposal.md) (the plan this image was built from).

| | Public image | This image (`signal-cli:0.14.9`, now `0.14.9-r2`) |
|---|---|---|
| Size | 1.31 GB | 193 MB |
| Outside code with key access | REST wrapper (one maintainer) | none |
| Downloads verified at build | no | GPG signatures + SHA-256 + libsignal hash |
| Trivy CRITICAL / HIGH (11 Oct 2026) | — | **0 / 0** (23 medium, 8 low: all glibc/gcc/zlib, no fix released) |

## How it works: from Signal to the subscriber
```
 Phone / other group members
        │  end-to-end encrypted
        ▼
 Signal servers (chat.signal.org)
        ▲
        │ ① outbound HTTPS/websocket, port 443 (signal-cli connects out; nothing connects in)
┌───────┴──────────── container signal-cli ─────────────────────┐
│  signal-cli (Java)                                            │
│   ② decrypts each message with libsignal,                     │
│      using the account keys in /data (low/signal-data)       │
│   ③ turns it into a JSON event and keeps it in memory         │
│      HTTP service on 127.0.0.1:8080 (not reachable outside)   │
│            │                                                  │
│  TLS gateway :8443 (launcher/TlsGateway.java)                 │
│   ④ mutual TLS 1.3, read-only allowlist                       │
└────────────┼──────────────────────────────────────────────────┘
             ▼
 signal-subscriber
   ⑤ GET /api/v1/events: a stream that stays open
```
1. **Connecting to Signal.** The container is a device on the Signal account: the main device for a number
   registered here, or a linked device next to a phone. At start-up it opens a lasting, encrypted connection out to
   Signal's servers on port 443, signing in with the credentials in `/data`. Signal keeps a separate queue of
   messages for each device on the account and pushes them down that connection. `--receive-mode on-start` makes it
   start receiving right away, without waiting for a client.
2. **Decrypting.** Signal's servers only ever hold encrypted messages. signal-cli decrypts them in the container
   with **libsignal**, Signal's own encryption library (the build checks it's byte-identical to Signal's release).
   The keys and session state are in `/data`. That's why the folder is mode 700, and why two signal-cli instances
   must never share it: both would update the same session state.
3. **Making events.** Each decrypted message, reaction, edit or receipt becomes one JSON event (`envelope`,
   `dataMessage`, `syncMessage`, `groupInfo`, …). The daemon (`daemon --http 127.0.0.1:8080`) serves them three ways:
   - `/api/v1/events`: a live stream (Server-Sent Events). Each event has an ID.
   - `/api/v1/rpc`: JSON-RPC commands, e.g. `listContacts`, which the subscriber uses for names.
   - `/api/v1/check`: a health check.
4. **TLS gateway.** The launcher opens port 8443 in front of 8080. It accepts only the subscriber's client
   certificate, over TLS 1.3, and passes through only the event stream, the health check and `listContacts`.
   Everything else, including `send`, is refused (see "TLS gateway" below).
5. **Streaming.** The subscriber opens `/api/v1/events` and leaves the connection open, and signal-cli writes each
   new event to it as it arrives.
   - The subscriber saves the last event ID it handled in `low/logs/signal-last-event-id`. After a reconnect it
     sends that ID back (`Last-Event-ID`) and gets only the events it missed.
   - If signal-cli itself has restarted, it doesn't know that ID, so it resends all it has kept (up to 1000).

**The launcher's other job:** `launcher/MessageLogLauncher.java` starts signal-cli itself and controls what
`docker logs` shows for each message (see "Message log" below).

**About the one-way rule:** it applies to the data diode, not to this connection. Like any Signal device,
signal-cli talks both ways with Signal's servers: it confirms each message it receives, so the server can remove it
from its queue, and it refreshes its keys from time to time. The subscriber can't make it send anything.

## What the build checks (it stops if any check fails)
1. **signal-cli tarball:**
   - It has a good GPG signature from AsamK's key. The fingerprint is pinned as `SIGNAL_CLI_SIGNER`, and the key
     itself is in `keys/`.
   - Its SHA-256 matches `SIGNAL_CLI_SHA256`, the digest GitHub publishes for the release asset.
2. **libsignal:** `libsignal_jni_amd64.so` is byte-identical to Signal's official libsignal-client release
   (`LIBSIGNAL_SO_SHA256`, from build-artifacts.signal.org). The macOS and Windows copies are removed.
3. **Wire runtime patch:** signal-cli 0.14.9 bundles `wire-runtime-jvm-6.0.0-alpha02` (CVE-2026-45799, HIGH: a
   crafted protobuf can crash message decoding). The build swaps in 6.3.0 from Maven Central:
   - It has a good GPG signature from Block's release key (`WIRE_SIGNER`, the same key that signs the Okio jar
     signal-cli already ships) and a pinned SHA-256.
   - Its dependencies (Okio 3.17.0, Kotlin ≥ 2.0.21) are already satisfied.
   - If a new signal-cli no longer has the old jar, the build stops so the swap can be reviewed or removed.
4. **Start-up without a shell:** the official launcher's class path and Java options are written to
   `/opt/signal-cli/java.args`, and the image starts `java @/opt/signal-cli/java.args`. The build stops if the
   launcher's options change or a jar is missing.
5. **Java runtime:** built with `jlink` from Debian's signed OpenJDK 25, with only the modules signal-cli uses.
6. **qrencode:** Debian's binary plus `libqrencode` and `libpng`, for link QR codes.
7. **Base images, both pinned by digest:**
   - Build stage: `debian:trixie-slim`.
   - Runtime: `gcr.io/distroless/cc-debian13`, which provides glibc, libstdc++, zlib and CA certificates.

**Why not Alpine:** Signal publishes libsignal only for glibc. On Alpine it fails to load, even with `gcompat`
(`Error relocating … gnu_get_libc_version`). Using Alpine would mean compiling libsignal ourselves, which loses the
byte-identical check.

## Build and scan
```
docker build -t signal-cli:0.14.9-r2 /apps/signal-relay/image
trivy image --scanners vuln --severity CRITICAL,HIGH signal-cli:0.14.9-r2   # 0 CRITICAL/HIGH on 2026-10-18
```

## Admin: link, register, PIN (`signalctl`)
`signalctl` runs on the host. Each command starts a short-lived, locked-down container that runs one program:
`java`, or `qrencode` for the link QR. The image needs no shell. By default it uses the relay's
account, `low/signal-data` (created with mode 700 if missing), and runs as you (`id -u`:`id -g`).
```
./signalctl link [DEVICE_NAME]                           # link to an existing account: shows a QR to scan in the phone
./signalctl register +15551234567 [--voice] [--captcha TOKEN]   # register a new number
./signalctl verify +15551234567 CODE [--pin PIN]         # finish registration
./signalctl set-pin +15551234567 PIN                     # turn on registration lock
./signalctl accounts                                     # accounts in the data folder
./signalctl devices +15551234567                         # devices linked to the account
./signalctl cli ARGS...                                  # any other signal-cli command
```
- **Registering a new number:** Signal usually asks for a captcha. Open
  https://signalcaptchas.org/registration/generate.html, solve it, copy the "Open Signal" link and pass it as
  `--captcha 'signalcaptcha://…'`. A number registered here makes this server its **main device**. Linking makes the
  server an extra device on an account whose main device is a phone.
- **The account must not be in use elsewhere:** stop any daemon container using the same data folder before
  commands on its account. signal-cli locks the account.
- **Settings:** `SIGNAL_IMAGE` (default: the `image:` of the signal service in `low/docker-compose.yml`), `SIGNAL_DATA` (default `low/signal-data`, the live
  relay account) and `RELAY_USER` (default your own `id -u`:`id -g`; must own the data folder). Set them only to use
  another image, folder or user. Use the live account only with signal-cli stopped.
- **No shell for troubleshooting:** `docker exec … sh` won't work. Use `docker logs`, `docker cp` or
  `./signalctl cli …`.

## Message log (`MESSAGE_LOG`)
signal-cli normally prints every received message to its log, **including the text**. The image starts signal-cli
through its own launcher (`launcher/MessageLogLauncher.java`, compiled in the build, about 400 lines, using only
Java and the Jackson library signal-cli bundles). The launcher controls that log:

| `MESSAGE_LOG` | `docker logs` shows |
|---|---|
| `redacted` (default) | One plain-English line per event: kind, sender (number shortened, e.g. `+1941…805`), device, group, time. Message content is hidden; only its length or count is shown. |
| `full` | signal-cli's normal output, **including message text**. Troubleshooting only. |
| `off` | Nothing. |

```
2026-10-11 03:24:10Z  Reply from “Zahnzaha Wrenn” +1813…936 (device 1) in group “Signal API” (zCV6AA…): text [hidden, 17 chars], quoting “Tho Pham”'s message from 03:23:35Z [hidden, 18 chars]
```
- **Only `daemon` and `receive` are affected.** All other commands (link, register, …) run unchanged.
- **How it works:** for `redacted`, signal-cli runs with `-o json`, so each event is a single line and a
  multi-line message can't slip past the filter.
- **What's hidden:**
  - Text: messages, edits, stories, quotes, captions, polls, contact cards.
  - Link previews, file names and payments.
  - Any field the launcher doesn't recognise, so anything new in a future signal-cli is hidden by default.
- **Names of people referred to** (quoted, reacted to, mentioned) appear once that person has sent a message since
  start. Before that, they're shown as a shortened number or ID.
- **Self-test (no account needed):** `docker run --rm -i signal-cli:0.14.9 --summarize-stdin < events.jsonl`
  prints the summary of each JSON line. Use it to check the redaction against sample events after an update.

## Licences in the image (r2)
`/usr/share/doc/signal-cli-image/` holds a `NOTICE` (what is in the image, with each project's source link) and the
licence texts of signal-cli (GPL-3.0), libsignal (AGPL-3.0), the swapped-in Wire runtime (Apache-2.0), and the
Debian copyright files of qrencode and libpng. The other Java libraries carry theirs inside their jars; the Java
runtime's are in `/opt/java/legal`. The texts come from the pinned Debian base, not from downloads. Read them with
`docker cp signal-cli:/usr/share/doc/signal-cli-image - | tar t` (no shell in the image).
Still open (the owner's decision): a licence for this repository itself (the launcher runs inside signal-cli's
GPL-3.0 process), and publishing the signal-cli and libsignal sources next to the image when it is shared.

## Health check (`/api/v1/check`, image r2 and later)
`signal-cli:0.14.9-r2` (2026-10-18) adds `launcher/SignalHealth.java`: signal-cli's own view of its health, for the
relay's monitoring (`docs/monitoring.md`, "Signal servers"). Through the gateway, `/api/v1/check` still asks
signal-cli's own check first; when that is 200, the answer gets a JSON body:
```
{"v":1,"boot":…,"sig":{"st":"CONNECTED","since":…,"ka":…,"rtt":30,"rc":0,…},
 "rx":{"loop":…,"env":…,"caught":…},"jvm":{"heap":4.8,"gc":0},"gw":{"conns":2},"p":{"DECRYPT_FAILED":1,…}}
```
- `sig`: the link to Signal's servers: state (`CONNECTED`, `CONNECTING`, `RECONNECTING`, `AUTH_FAILED` = unlinked or
  deregistered, `EXPIRED` = this version refused), since when, last keep-alive answered (asked every 30 s), round
  trip, drops, last failure (in practice `CONNECT_FAILED`, Signal's library's "all connect attempts failed", or
  `DEREGISTERED` / `EXPIRED`; the full reason goes to `docker logs` as "Signal servers: connection failed …").
- `rx`: the receive loop's last run, envelopes received, when it last caught up. `jvm`: heap in use after GC (%), GC
  ms in the last minute. `gw`: open gateway connections. `p`: warnings and errors by fixed code (refused clients,
  blocked calls, decrypt failures, database errors, anything else as `OTHER`).
- **How:** the launcher listens to signal-cli's log events inside its own Java process (logback's public API) and
  keeps only codes, times and counts, never log text. signal-cli connects Signal's network library to its logging
  only with `--verbose`; the launcher connects it itself (`SignalLogger.initLogger()`) and keeps those lines off the
  console. `docker logs` looks as before, plus a few lines like `Signal servers: connected.` /
  `Signal servers: lost the connection, reconnecting...`.
- **It matches signal-cli's log texts**, so after a signal-cli update check that `docker logs signal-cli | grep
  "Signal servers"` says connected (step 3 below). If nothing matches, the Monitor tab shows `SIGNAL_UNKNOWN`, never a
  false DOWN; the fix is in `SignalHealth.java`.
- The build's `.dockerignore` lets only named files in: a new launcher file must be added there too.

## TLS gateway (`TLS_LISTEN`), for a subscriber on another network
signal-cli's HTTP service has no encryption and no login. With `TLS_LISTEN` set (e.g. `0.0.0.0:8443`), the
launcher (`launcher/TlsGateway.java`, JDK only) puts a mutual-TLS front end before it:
```
subscriber ══TLS 1.3, client certificate══▶ gateway :8443 ──inside the container──▶ signal-cli 127.0.0.1:8080
```
- **TLS 1.3 only. Client certificate required:** signed by our CA, with a name (CN) in `TLS_CLIENT_NAMES`
  (default `signal-subscriber`). Anything else is refused during the handshake.
- **Read-only allowlist, enforcing the one-way rule:** `GET /api/v1/events`, `GET /api/v1/check`, and
  `POST /api/v1/rpc` only for methods in `RPC_ALLOW` (default `listContacts`). Everything else, including `send`,
  gets 403, even with a valid certificate.
- **Per client (r2):** `TLS_CLIENT_ALLOW` limits a certificate to some of `events`, `check`, `rpc`, e.g.
  `signal-monitor=check;dump=events` (`;` between clients). A client not listed may use all three, as before. The relay
  sets `signal-monitor=check`: its certificate can't open the message stream (tested: 403). A wrong word stops the
  gateway at start, so a typo can't silently allow too much.
- **Request handling:**
  - Requests are rebuilt from a short header list.
  - Only one request is allowed per connection, and no chunked bodies, so a second request can't be smuggled in.
  - Bodies are limited to 64 KB, and at most 32 connections can be open.
- **It never falls back to plain:**
  - If the certificates can't be loaded, or `--http` isn't `127.0.0.1:PORT`, the container stops with an error.
- **Settings:** `TLS_LISTEN`, `TLS_UPSTREAM` (default: the `--http` address), `TLS_DIR` (default `/certs`: `server.crt`,
  `server.key`, `ca.crt`), `TLS_CLIENT_NAMES`, `RPC_ALLOW`.
- **Logs:** go to `docker logs`, and never include message contents. For example: "Opened the message stream for 172.24.0.3
  (signal-subscriber)", "Blocked an RPC call …: method send is not allowed".
- **Tested (11 Oct 2026):**
  - Refused: no certificate, a certificate from another CA, the server's own certificate used as a client,
    TLS 1.2, `send`, batches, chunked bodies, other paths, and a smuggled second request.
  - Plain port 8080 isn't reachable from other containers.

### Certificates (`tlscerts`)
`tlscerts` runs on the host, using openssl. It creates our own self-signed CA, which signs one server certificate
(signal-cli's gateway, `signal/`) and one client certificate (the subscriber, `subscriber/`). All of them use ECDSA P-256
and are valid for 5 years (`TLS_DAYS=1826`).
```
./tlscerts init NAME...     # CA + both certificates; NAME = the signal-cli host's name(s)/IP(s); "signal" is always added
./tlscerts server NAME...   # new server certificate (e.g. new host name), same CA: subscriber unchanged
./tlscerts client [NAME]    # new client certificate, same CA; a NAME makes a separate client (add it to TLS_CLIENT_NAMES)
./tlscerts show             # names and expiry dates
```
Output: the CA goes to `/apps/signal-relay/certs/ca`, and the others to `/apps/signal-relay/low/certs`. Copy each folder only to the host that needs it:
- `signal/` goes to the signal-cli host, and `subscriber/` to the subscriber host.
- Keep `ca/`, which holds the CA key, offline. It's only needed to issue new certificates.
- On every host, the keys must be owned by the user the containers run as (`relay-user` (settings block) in the compose files).

**Renewal:** all three certificates expire together, 5 years after `init` (see `show`). Before then, run `init`
again with new `TLS_CA_DIR` and `TLS_CERTS` folders, copy both sides over, and restart both containers.

**Full guide, with examples:** [`tlscerts.md`](tlscerts.md) (settings, names, recipes, checks, troubleshooting).

## Running the relay service
The relay reads incoming messages from signal-cli's built-in HTTP service:
`/api/v1/events` is a live event stream, `/api/v1/rpc` takes JSON-RPC commands and `/api/v1/check` is a health check.
```
docker run --init … signal-cli:0.14.9 -a +15551234567 daemon --http 0.0.0.0:8080 --receive-mode on-start \
           --ignore-stories --ignore-avatars --ignore-stickers
```
- **No login:** signal-cli says so at start-up. **Never publish the port.** Keep it on `127.0.0.1` and publish only the
  TLS gateway (above), or put the container on a private Docker network shared only with the subscriber.
- **Run it locked down:** `--init --user 1000:1000 --read-only --tmpfs /tmp:rw,exec,size=400m --cap-drop ALL
  --security-opt no-new-privileges`, plus memory and process limits.
  - `--init` lets stop signals reach Java.
  - libsignal unpacks itself (~200 MB) into `/tmp` at every start and loads from there. That needs `exec`: Docker's
    default `noexec` tmpfs fails with *failed to map segment from shared object*. The rest of the filesystem stays
    read-only.

## Updating
Signal stops serving clients that fall too far behind, so **run `./check-update` about once a month**, on the dev
host (it needs the internet). New CVEs are also published against packages that haven't changed, which it catches too.

### 1. Check (`./check-update`, about 20 seconds)
```
signal-cli      pinned 0.14.9, latest 0.14.9 (released 2026-10-07)
Wire runtime    pinned 6.3.0 (replacing 6.0.0-alpha02), latest 7.1.0
BUILD_BASE      debian:trixie-slim: unchanged
RUNTIME_BASE    gcr.io/distroless/cc-debian13: unchanged
Trivy           signal-cli:0.14.9: 0 CRITICAL/HIGH

Up to date. Nothing to do.
```
- **What it checks:** the latest signal-cli release on GitHub, the latest Wire runtime, whether either base image has a new
  digest, and a Trivy scan of the image the relay runs (`image:` in `low/docker-compose.yml`).
- **Exit status:** 0 up to date, 1 something to do (listed under "To do"), 2 a check failed. Add `--no-scan` to skip Trivy.
- **Nothing to do?** Stop here.

### 2. Prepare and build (`./check-update --prepare --apply --build`)
First read the release notes (the link is in the To do list). Then:
```
./check-update --prepare --apply --build   # or --prepare alone to only print the values
git diff Dockerfile
```
It downloads the release (about 125 MB) into a temporary folder and stops at the first failed check:
- **Signature:** must be from the pinned key (`SIGNAL_CLI_SIGNER`, `keys/asamk-signal-cli.asc`), and the SHA-256 must
  match GitHub's digest.
- **libsignal:** the bundled `libsignal_jni_amd64.so` must be byte-identical to Signal's own release of that version.
- **Start-up options:** the official launcher's JVM options must be what Dockerfile step 4 expects.
- **Wire:** it says whether the CVE-2026-45799 swap is still needed. If the release bundles 6.3.0 or later, the To do list
  says to remove Dockerfile step 3, the `WIRE_*` args and the Wire `sed` in step 4. Do that by hand.

`--apply` writes `SIGNAL_CLI_VERSION` (both copies), `SIGNAL_CLI_SHA256`, `LIBSIGNAL_SO_SHA256` (and the comment naming
the libsignal version), `WIRE_OLD` if it changed, and new `BUILD_BASE` / `RUNTIME_BASE` digests.

`--build` then builds and scans the image:
- **Tag:** `signal-cli:VERSION`. If that tag already exists (a rebuild for new base images), it uses
  `signal-cli:VERSION-r2`, `-r3` and so on, so the old image stays for rollback.
- **Not built** if a To do item needs a manual Dockerfile change (Wire swap removal, start-up options). Make the change,
  then build by hand (step 3). It also doesn't build when nothing in the Dockerfile changed. Self-test: `./check-update --prepare 0.14.9` must print the
values already pinned, all "(unchanged)".

**If the signature check fails because the signing key changed:** don't replace `keys/` with whatever the new release
points to. Confirm the new key through the project itself (the README, several releases, the maintainer) first.

**Only a base image changed:** `--prepare --apply --build` handles that too. It re-verifies the current release,
writes the new digests and builds `signal-cli:VERSION-r2`.

### 3. Test on dev
Building by hand, when `--build` didn't:
```
docker build -t signal-cli:NEW /apps/signal-relay/image
trivy image --scanners vuln --severity CRITICAL,HIGH signal-cli:NEW
```
Then in `low/docker-compose.yml`'s settings block set `signal-image: &signal-image signal-cli:NEW`, and from `low/`:
```
docker compose up -d
docker logs -f signal-cli                       # starts, opens the message stream for the subscriber
docker logs signal-cli 2>&1 | grep "Signal servers"   # "connected." within a few seconds (health check, r2)
```
Send a message in the group. It must show up in the console (`https://<host>.lab.int:8090`), and the Monitor tab's
"Signal servers" tile must be UP with a recent keep-alive.
`signalctl` and `check-update` read `signal-image` from the compose file, so there's nothing else to change.

### 4. Deploy to prod
```
docker save signal-cli:NEW | gzip > signal-cli-NEW.tar.gz        # on dev
docker load -i signal-cli-NEW.tar.gz                             # on prod
```
On prod, set the same `signal-image` in `low/docker-compose.yml`, then `docker compose up -d` in
`low/`, and check `docker logs signal-cli` and the console as on dev.

**Rollback:** put the old tag back in `image:` and recreate the service. Keep the previous image on prod until the new
one has run cleanly for a few days.
- signal-cli may upgrade its account database when a new version first starts, and an older version may then refuse it.
  So **back up `low/signal-data` (with signal-cli stopped) just before deploying**.
- If the old version won't start on the upgraded data, restore that backup. Do it soon: the older the backup, the more
  conversations need to re-establish their encryption sessions after it.
