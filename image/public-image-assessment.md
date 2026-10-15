# Assessment of the public image `bbernhard/signal-cli-rest-api`

*11 October 2026. Subject: the public open-source image `bbernhard/signal-cli-rest-api`, release 0.101.*
*Part 1 of the "Signal Chat Relay Container Review" (`docs/Signal-Chat-Relay-Container-Review.pdf`). Part 2, the
proposal for the in-house image, became this folder.*

Whatever runs this image holds the Signal account's encryption keys: it can read the account's chats and act as the
account. It needs the same scrutiny as any system that holds credentials. We examined release 0.101, the published
build recipe, the source code and the project's own deployment examples.

## Bottom line
The public image contains no evidence of malicious code, and its Signal encryption and protocol components match the
official releases. But whatever runs it holds the Signal account's keys, and the image falls short of what a system
holding credentials needs:
- Its **control interface has no login**, and the project's own examples make it reachable from the network. Anyone
  who reaches it can read the account's messages, send as the account, or take the account over.
- It runs **code from a single outside developer** with full access to the account keys, and we can't prove the
  shipped program matches the published source.
- Its **downloads aren't verified** when it's built, and its advertised **"latest" version changes silently**, so a
  faulty or tampered release could arrive unnoticed.
- Its link to the rest of the relay is **unencrypted**, which rules out running the parts on separate networks.

| Verdict | |
|---|---|
| **Authentic** | The Signal encryption library and the Signal protocol software match the official releases exactly. |
| **Well behaved in testing** | Running in a test environment, it connected only to Signal's own servers. |
| **Open by design** | Its control interface has no login, and the project's examples make it reachable from the network. |
| **Loosely controlled** | Downloads aren't verified when it's built, the advertised version changes silently, and part of the code comes from one maintainer. |

## What's inside, and what we verified
| Component | Published by | What it does | Verified |
|---|---|---|---|
| Encryption library (libsignal) | Signal Messenger | All encryption and key handling | **Identical** to Signal's official release, checked on the copy the image actually loads when it runs |
| Signal protocol software (signal-cli) | signal-cli project | Connects to Signal, sends and receives | **Identical**: 77 of 79 libraries byte-for-byte; the other 2 differ only by repackaging and the extra library below |
| Web control interface (REST API) | One outside developer | Wraps signal-cli in a web API; ~14,000 lines | *Source reviewed* for network calls: only local ones found. We can't prove the shipped program was built from that source. |
| Extra copy of the encryption library | Same outside developer | Alternative build for other CPU types | *Present, not used* on standard servers. Confirmed it is not loaded. |
| Operating system and process supervisor | Debian, s6-overlay | Base system | Widely used; not reviewed |

## Findings
| Severity | Finding | Why it matters | Mitigation |
|---|---|---|---|
| **High** | The control interface has no login of any kind, and the project's examples publish it to the network. The documentation gives no warning about this. | Anyone who can reach it can link their own phone to the account and read every message from then on, send messages as the account, or delete the account. | Never expose the interface beyond its own host. The in-house image has no such interface. |
| **Medium** | The project's examples use the moving "latest" version rather than a fixed one. | A routine update silently installs whatever was last published, including a faulty or compromised release. | Pin an exact, reviewed version. The in-house image only changes when we change it. |
| **Medium** | The image's build recipe downloads its components without checking fingerprints or signatures. | If a download source were tampered with, the change would flow into the published image unnoticed. | The in-house image checks every download against a recorded fingerprint. |
| **Medium** | A single outside maintainer's code runs with full access to the account keys. | Users depend on one person's security practices, and can't prove the shipped program matches the reviewed source. | The in-house image doesn't include this code. |
| **Low** | The image carries far more than a one-way relay needs: sending, account management, device linking, add-on scripts that can make web requests, and outgoing notifications. | Every unused feature is a way the account could be misused if something else went wrong. | The in-house image contains only what the relay uses. |
| **Info** | An unused extra copy of the encryption library, built by the maintainer, is in the image. | No effect on standard servers; it's unneeded code from a non-official source. | Not included in the in-house image. |

On the positive side, the image already runs as a regular user rather than an administrator, and its recipe supports
a read-only system. No software, open or closed source, can be proven free of malicious code. Assurance comes from
three things together: verifying that what runs matches trusted releases, limiting what it could do if it misbehaved,
and watching for unexpected behaviour.

## Evidence
- The encryption library the image loads at run time matches Signal's official libsignal 0.102.1 release
  (SHA-256 `00ecda2a…6662d`).
- signal-cli 0.14.8 libraries compared one by one with the official GitHub release: 77 of 79 byte-identical; one
  repackaged with identical contents; one carrying the unused extra library.
- Source of the web interface reviewed at the exact release examined (0.101), including every outbound network call
  and its full list of control endpoints. No login check exists anywhere in the code.
- The project's README and example deployment file were reviewed: both use the "latest" version and publish the
  control interface, with no security warning.
- The upstream build recipe was reviewed for download verification and version pinning.
- In a test environment, the running image connected only to Signal's own servers.
- The built-in message service, with a live event stream, was confirmed present in signal-cli 0.14.8.
