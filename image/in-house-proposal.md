# Proposal: an in-house Signal Chat Relay

*11 October 2026. Part 2 of the "Signal Chat Relay Container Review" (`docs/Signal-Chat-Relay-Container-Review.pdf`).
Part 1, the assessment of the public image, is `public-image-assessment.md`. This proposal was approved and built as
this folder.*

The official Signal protocol software already includes its own built-in service for streaming incoming messages. We
confirmed this in its recent releases. That makes the outside developer's web interface unnecessary for a one-way
relay. We'd build a small image of our own that contains only the official Signal components, verified on every
build, and nothing else.

**In short:** build an in-house Signal Chat Relay from verified official Signal releases only. It has no outside
developer's code and no control interface, it checks every component on every build, and it connects to the relay
over an encrypted, read-only link that only our own systems can use. Estimated 4–6 working days of one engineer to
build, test and deploy; no licence cost.

## Who we'd have to trust
```
Public image                                    In-house Signal Chat Relay
┌───────────────────────────────────────────┐   ┌───────────────────────────────────────────────────┐
│ Container image       outside developer   │   │ Container image       us: build file + gateway    │
│ Web control interface outside developer   │   │ Signal protocol sw.   signal-cli project, verified│
│ Signal protocol sw.   signal-cli project  │   │ Encryption library    Signal Messenger, verified  │
│ Encryption library    Signal Messenger    │   └───────────────────────────────────────────────────┘
└───────────────────────────────────────────┘
```
- **Outside, single maintainer:** the public image's container image and web control interface.
- **Official release, fingerprint-checked:** the Signal protocol software (signal-cli) and the encryption library (libsignal).
- **Built and controlled by us:** the in-house container image.

## How it fits the relay
```
Signal servers
     │  encrypted, outbound only
     ▼
[ Signal container ]   in-house Signal Chat Relay · official Signal software only
     │  encrypted link (TLS 1.3, both sides prove identity) · same host or across networks
     ▼
[ Subscriber ] → CEF over UDP → data diode → [ Receiver ] → local chat playback (POC), external platform later on
```
Each part has one job. The Signal container is the only part that holds the account's keys or talks to Signal. The
subscriber reads incoming messages from it over the encrypted link, keeps only the chat being relayed, and converts
each message to CEF. From the subscriber onward, only CEF over UDP travels, in one direction, so nothing downstream
ever reaches back to the Signal container or the account.

## Security controls built in
Every control below applies to the Signal container alone. Nothing changes on the shared host's firewall or for the
other applications running there.
- **Verified inputs:** every component is checked against its official published fingerprint on every build. A
  mismatch stops the build.
- **Pinned versions:** nothing changes unless we change it, and each change is reviewed.
- **No exposed control interface:** only the subscriber, holding its own certificate, can connect. The container
  offers no web pages, no login screens and no device-linking over the network.
- **Encrypted, authenticated link:** the connection to the subscriber is encrypted and both ends prove who they are
  (details below).
- **Locked-down container:** runs as a regular user, with a read-only system, no extra privileges, and memory and
  process limits.
- **Linked-device check:** a regular check of the devices linked to the account, with an alert on any new one.
- **Vulnerability scan** of the image on every build, for known published flaws.

## Encrypted link between the Signal container and the subscriber
The Signal container and the subscriber can run on the same host or on separate hosts in different networks. Because
message text travels over this link, it is always encrypted, in both layouts, and the image adds a small secure
gateway in front of the Signal software's built-in message service. The gateway uses only standard, built-in Java
components, so no outside code is involved.
- **Encryption:** TLS 1.3 only, the current standard; older versions are refused.
- **Both sides prove their identity:** the subscriber must present its own certificate, and it accepts only our
  Signal container's certificate. Connections without a valid certificate are refused before any data is exchanged.
- **Our own certificates:** issued by a private certificate authority we control, not a public one. Each host
  receives only its own key. The authority's signing key is kept offline and used only to issue or renew certificates.
- **Read-only by design:** the gateway allows only reading incoming messages and looking up display names. Sending
  messages and every other account action are blocked, even for a connection with a valid certificate. This enforces
  the relay's one-way rule in the Signal container itself.
- **Never falls back to unencrypted:** if the certificates are missing or invalid, the Signal container refuses to
  start. The unencrypted built-in service stays inside the container and is never reachable from the network.
- **Simple to place:** moving between one host and two is a configuration change, not a code change. The network
  port is a single setting (443 by default).
- **No lost messages on reconnect or move:** the subscriber records how far it has read, and the Signal container
  resends anything missed.

When the two run on separate hosts, the network must allow the subscriber's host to connect to the Signal container's
host on the chosen port. This is the only network requirement, and it is a standard firewall request between the two
hosts, not a change to the shared host.

## Plan and effort
| Phase | Work | Estimate |
|---|---|---|
| **Build** | Build the in-house Signal Chat Relay: build file with fingerprint checks, the secure gateway and certificates, vulnerability scan, written build and update procedure. | 2–3 days |
| **Test** | Confirm it connects to Signal, receives and streams messages correctly, and handles reconnects without losing messages. Confirm the gateway refuses connections without a valid certificate and blocks sending. Confirm admin tasks (linking a device, registering a number) work. | 1–2 days |
| **Deploy** | Issue the certificates, install the image with its locked-down settings, and connect the subscriber over the encrypted link. | ½–1 day |

Estimates assume one engineer and include testing. Total about 4–6 working days.

## Trade-offs and risks
| Trade-off | Impact | How we handle it |
|---|---|---|
| We own updates | Signal stops serving clients that fall too far behind, so the Signal software must be kept current. With the public image, the outside developer does this. | A written monthly update and verification routine, delivered with the image. Each update is a version number and fingerprint change. |
| No web admin screens | Occasional admin tasks, such as linking a device, are done from the server's command line. | For a one-way relay this is a benefit: there are fewer ways to act on the account. |
| The built-in message service has no login | Anything that can reach it could use it. | It stays inside the container. The only way in is the secure gateway, which requires the subscriber's certificate and allows reading only. |
| Small change to the subscriber | New connection code, plus a new way to look up display names. | Covered in testing. |
| Certificates to look after | The link's certificates must be renewed before they expire. | A script issues and renews them; renewal is a short, scheduled task. |
| Open-source licences | The Signal components are GPL and AGPL licensed. | Internal use is permitted. Obligations apply only if a modified version is given outside the organisation; we don't modify them. |

## Options considered
| | Use the public image | Customise the public image | Build an in-house Signal Chat Relay (recommended) |
|---|---|---|---|
| What it means | Deploy it as published, with careful settings | Take a private copy of the outside code, add a login and remove unneeded features | Our own small image with only the official Signal components |
| Outside code with key access | Yes | Yes, but ours to review | None |
| Control interface | Yes, no login | Yes, with a login | None |
| Link to the subscriber | Unencrypted, no login | Depends on the changes made | Encrypted; certificate required; read-only |
| Code we must maintain | None | ~14,000 lines, merged with every upstream change | A short build file and a small gateway, plus small subscriber changes |
| Effort | — | 2–3 weeks | 4–6 days |
