# In-house signal-cli image

The official [signal-cli](https://github.com/AsamK/signal-cli) release, with Signal's own libsignal, on Google's
distroless base: glibc only, with no shell and no package manager. It replaces `bbernhard/signal-cli-rest-api`.
There's no third-party code and no web control interface.

| | Public image | This image (`signal-cli:0.14.9`) |
|---|---|---|
| Size | 1.31 GB | 193 MB |
| Outside code with key access | REST wrapper (one maintainer) | none |
| Downloads verified at build | no | GPG signatures + SHA-256 + libsignal hash |
| Trivy CRITICAL / HIGH (11 Oct 2026) | — | **0 / 0** (23 medium, 8 low: all glibc/gcc/zlib, no fix released) |

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
docker build -t signal-cli:0.14.9 /apps/signal-relay/image
trivy image --scanners vuln --severity CRITICAL,HIGH signal-cli:0.14.9
```

## Admin: link, register, PIN (`signalctl`)
`signalctl` runs on the host. Each command starts a short-lived, locked-down container that runs one program:
`java`, or `qrencode` for the link QR. The image needs no shell. The default account data folder is
`/apps/signal-relay/signal-cli-data`, created with mode 700.
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
- **Settings:** `SIGNAL_IMAGE` (default `signal-cli:0.14.9`) and `SIGNAL_DATA` change the image and the data folder.
  The live relay account is in `/apps/signal-relay/left/signal-data`. Use `SIGNAL_DATA=…/left/signal-data` for it, and only
  with signal-cli stopped.
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
./tlscerts client           # new client certificate, same CA: signal-cli unchanged
./tlscerts show             # names and expiry dates
```
Output: the CA goes to `/apps/signal-relay/certs/ca`, and the others to `/apps/signal-relay/left/certs`. Copy each folder only to the host that needs it:
- `signal/` goes to the signal-cli host, and `subscriber/` to the subscriber host.
- Keep `ca/`, which holds the CA key, offline. It's only needed to issue new certificates.
- On every host, the keys must be owned by the user the containers run as (`x-relay-user` in the compose files).

**Renewal:** all three certificates expire together, 5 years after `init` (see `show`). Before then, run `init`
again with new `TLS_CA_DIR` and `TLS_CERTS` folders, copy both sides over, and restart both containers.

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
Signal stops serving clients that fall too far behind, so check for a new signal-cli release about monthly.
Re-scan the current image too: new CVEs are published against unchanged packages.
1. Read the release notes at https://github.com/AsamK/signal-cli/releases.
   - Set `SIGNAL_CLI_VERSION`, and set `SIGNAL_CLI_SHA256` from the asset digest on the release page.
2. **libsignal:** if the bundled version changed (`lib/libsignal-client-X.jar`):
   - Download `https://build-artifacts.signal.org/libraries/maven/org/signal/libsignal-client/X/libsignal-client-X.jar`.
   - Set `LIBSIGNAL_SO_SHA256` to the sha256 of its `libsignal_jni_amd64.so`.
3. **Wire:** if the new release bundles wire-runtime 6.3.0 or later, delete the Wire swap (step 3 in the
   Dockerfile and the `WIRE_*` args). If it still bundles `WIRE_OLD`, keep the swap. If it bundles another
   vulnerable version, update `WIRE_OLD`.
4. **Signing keys:** check the release is still signed with `SIGNAL_CLI_SIGNER`. If a key ever changes, verify the
   new key through the project itself before trusting it.
5. **Base images:** refresh the digests (`docker pull`, then `docker image inspect … --format '{{index .RepoDigests 0}}'`).
6. Rebuild, run the Trivy scan, test with `./signalctl accounts` and the relay, then deploy.
