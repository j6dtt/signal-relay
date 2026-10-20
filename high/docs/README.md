# Signal Chat Relay: all documents

Every document is in this folder. The console's Analyst reads them too, and the runbook (what each problem the
page shows means and what to check): ask in its **How-to** tab. To add a
document, put a `.md` file here; it is searchable at the next question (its `##` headings become the sections the
Analyst can point to).

## Start here

| Document | For |
|---|---|
| [architecture-and-risks.md](architecture-and-risks.md) | what the relay is, the rules it never breaks, decisions and why, the risks we assessed |
| [prod-deploy.md](prod-deploy.md) | the checklist for a first install on prod |
| [adding-subscribers-receivers.md](adding-subscribers-receivers.md) | adding or removing a subscriber or a receiver, with nothing else restarting |
| [operations.md](operations.md) | day to day: what to check, where every file lives, what grows, backups and restores, dates to watch |
| [updating.md](updating.md) | bringing new code, settings or a new image to prod, and rolling back |
| [settings.md](settings.md) | every setting on both sides and in `.env`: what it is, dev's value, what prod needs |
| [data-handling.md](data-handling.md) | where message content is, how long, who can see it, what goes back to Signal, deleting |

## Each side

| Document | For |
|---|---|
| [low-side.md](low-side.md) | the low side (`low/`): deploying, one or two hosts, certificates, `routes.conf`, signal-monitor |
| [high-side.md](high-side.md) | the high side (`high/`): deploying, settings, the page, users, how the receiver rebuilds messages, Kubernetes |
| [register-account.md](register-account.md) | registering a new Signal number for the relay |

## signal-cli and its image

| Document | For |
|---|---|
| [image.md](image.md) | building, scanning and updating the in-house signal-cli image; its TLS gateway |
| [tlscerts.md](tlscerts.md) | the certificates between signal-cli and its clients (`image/tlscerts`) |
| [signal-cli-reference.md](signal-cli-reference.md) ([PDF](signal-cli-reference.pdf)) | the technical reference for engineers and security reviewers |
| [public-image-assessment.md](public-image-assessment.md) | why not the public `signal-cli-rest-api` image |
| [in-house-proposal.md](in-house-proposal.md) | the proposal for the in-house relay |
| [Signal-Chat-Relay-Container-Review.pdf](Signal-Chat-Relay-Container-Review.pdf) | both of the above as one document for leadership |

## Monitoring and the Analyst

| Document | For |
|---|---|
| [monitoring.md](monitoring.md) | everything about monitoring: the page, states, incidents, the Analyst, settings |
| [monitoring-redesign.md](monitoring-redesign.md) | why monitoring works the way it does (boxes vs connections, incidents per problem) |
| [sim-tests.md](sim-tests.md) | the fault-simulation tests: how to run them, what each does |
| [simulation-proposal.md](simulation-proposal.md) | the original proposal for those tests |

## Designs

| Document | For |
|---|---|
| [room-routing-design.md](room-routing-design.md) | one receiver per Gotham room, routed by the diode on the header |

## Diagrams

In [diagrams/](diagrams/): each as an editable `.drawio`, a `.pdf` and a `.png`.

| Diagram | Shows |
|---|---|
| 01-end-to-end-relay | the whole relay, Signal to the destination |
| 02-signal-cli-container | what runs inside the signal-cli container |
| 03-signal-cli-layers | signal-cli's layers, from our launcher to libsignal |
| 04-receiving-a-message | one message, step by step |
| 05-who-we-trust | what each part trusts, and the certificates |
| 06-receiver-rebuild | how the receiver rebuilds a message from its parts |
| 07-register-a-number | registering a number |
| 08-build-and-update | building and updating the image |
| 09-monitoring-design | the monitoring, as built |

`render/` renders `signal-cli-reference.md` to its PDF (`./render/render.sh`).
