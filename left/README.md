# left/: the Signal side of the data diode

Runs signal-cli (the in-house image) and the subscriber. The subscriber sends each message of the watched group as
CEF over UDP to the diode's input. Nothing comes back. The other side is `../right`.

```
signal-cli ══mutual TLS 1.3══▶ subscriber ──CEF over UDP──▶ diode input (UDP_DEST:6510)
```

## Deploying
Copy this whole folder to the left host, then run `docker compose up -d` in it. It needs the image
`signal-cli:0.14.9` (build it from `../image`, then `docker save` / `docker load`) and `python:3.13-alp-u3`.

In a fresh clone of the repo, these folders exist but are empty (only a `.gitkeep`): keys, certificates and
logs are never committed. Fill them, then set the owner and mode:
`sudo chown -R <user>: signal-data certs logs && sudo chmod 700 signal-data certs certs/* logs`.
`<user>` is the account the containers run as (`x-relay-user` at the top of `docker-compose.yml`; in prod, the operator's own).

| Folder | Holds | Needed on |
|---|---|---|
| `signal-data/` | the Signal account keys | signal-cli host only. Keep it mode 700, owned by the user in `x-relay-user`. |
| `certs/signal/` | signal-cli's TLS certificate | signal-cli host only |
| `subscriber/` | `subscriber_udp.py`, `comlibv3.py` | subscriber host only |
| `certs/subscriber/` | the subscriber's TLS certificate | subscriber host only |
| `logs/` | `signal-last-event-id` (where the subscriber stopped), `signal-udp-sent.jsonl` | subscriber host only |

## One host or two
signal-cli and the subscriber may or may not be on the same network. Switch by editing `docker-compose.yml`:

| Setup | In `docker-compose.yml` |
|---|---|
| **One host** (as shipped) | Both services. No `ports:` on `signal`. `SIGNAL_URL=https://signal:8443`. |
| **signal-cli host** | Comment out the `subscriber` service. Uncomment `ports:` on `signal` (e.g. `"443:8443"`). |
| **Subscriber host** | Comment out the `signal` service. Set `SIGNAL_URL=https://<signal-cli host>:<port>`. |

- **The host name in `SIGNAL_URL`** must be one signal-cli's certificate covers: `signal`, `ubt2` and `172.16.0.46` today.
  `../image/tlscerts show` lists them, and `../image/tlscerts server NAME...` adds one.
- **The port in `SIGNAL_URL`** must match the published `ports:` port. Host port 8443 is taken by another app on the dev host.
- **`UDP_DEST`** is the diode's input address. On the dev host it is `172.16.0.46`, where `../right` listens.

**Moving the subscriber to another host:**
1. `docker compose stop subscriber`.
2. Copy `logs/signal-last-event-id`, `subscriber/` and `certs/subscriber/` to the new host. The ID lets it resume
   without gaps.
3. Edit both copies of `docker-compose.yml` as in the table, then `docker compose up -d --remove-orphans` on each host.
4. Check that `docker logs signal-subscriber` says "Connected to signal-cli".

**Never run signal-cli on two hosts, or twice on the same `signal-data`.** Two instances on the same account
compete for messages and corrupt its sessions.

## New number (service account)
To register a new number, such as a landline, as the relay's own account, follow `../docs/register-account.md`.

## More groups
Each subscriber relays one group, set by `GROUP` in `docker-compose.yml`. For another group, uncomment the
`subscriber-2` block and set its `GROUP`. All subscribers read from the same signal-cli.
- Each one needs its own `container_name`, `GROUP`, `STATE_FILE` and `SENT_LOG`. They can share the certificate.
- A subscriber with no `GROUP` refuses to start, so it can never relay every chat by accident.
- **To find a group's ID:**
  - Stop signal-cli (`docker compose stop signal`), because the account can only be open once.
  - Run `SIGNAL_DATA=/apps/signal-relay/left/signal-data ../image/signalctl cli -a +19415499805 listGroups`. It lists each group's ID and name.
  - Then `docker compose start signal`. Messages sent in the meantime wait on Signal's servers.
  - signal-cli's own log shows only a shortened ID.

## After editing a script
Run `docker compose up -d --force-recreate subscriber`. A plain restart can keep the old file.
