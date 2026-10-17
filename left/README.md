# left/: the Signal side of the data diode

Runs signal-cli (the in-house image) and the subscriber. The subscriber sends each message of the watched group as
CEF over UDP to the diode's input. Nothing comes back. The other side is `../right`.

```
signal-cli ══mutual TLS 1.3══▶ subscriber ──CEF over UDP──▶ diode input (UDP_DEST:6510)   chat
    ▲ health check                 │ status every 10 s
    └──────────── signal-monitor ◀─┘ ──CEF over UDP──▶ diode input (UDP_DEST:6511)   monitoring
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
| `monitor/` | `signal_monitor.py` | signal-cli host |
| `certs/signal-monitor/` | signal-monitor's TLS certificate (`../image/tlscerts client signal-monitor`) | signal-cli host |

## One host or two
signal-cli and the subscriber may or may not be on the same network. Switch by editing `docker-compose.yml`:

| Setup | In `docker-compose.yml` |
|---|---|
| **One host** (as shipped) | Both services. No `ports:` on `signal`. `SIGNAL_URL=https://signal:8443`. |
| **signal-cli host** | Comment out the `subscriber` service. Uncomment `ports:` on `signal` (e.g. `"443:8443"`). |
| **Subscriber host** | Comment out the `signal` service. Set `SIGNAL_URL=https://<signal-cli host>:<port>`. |

- **The host name in `SIGNAL_URL`** must be one signal-cli's certificate covers. `../image/tlscerts show` lists them.
  If the subscriber logs `IP address mismatch` or `Hostname mismatch`, the name isn't covered: see
  [Replacing signal-cli's certificate](#replacing-signal-clis-certificate).
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

## Replacing signal-cli's certificate
Do this when the subscriber reaches signal-cli by a name or IP address that the certificate doesn't cover, or when the
certificate is close to expiry. It replaces only the server certificate. The CA and the subscriber's certificate
stay as they are, so the subscriber host needs no change.

All `tlscerts` commands, settings and checks: [`../image/tlscerts.md`](../image/tlscerts.md).

Three machines are involved:
- **CA host:** the one with `certs/ca/` (the dev box).
- **signal-cli host.**
- **Subscriber host.**

Below, `SIGNAL_HOST` is the address in the subscriber's `SIGNAL_URL`.

**1. Make the new certificate (CA host).** This writes to a separate folder, so the CA host's own `left/certs` isn't
touched:
```
cd /apps/signal-relay/image
TLS_CERTS=$HOME/new-certs ./tlscerts server SIGNAL_HOST
```
- **List every name** the subscriber may use. See [Names on the certificate](#names-on-the-certificate) below.
- **Check the output's `Server certificate for:` line.** It must show `SIGNAL_HOST`.

The new files are in `~/new-certs/signal/`.

**2. Copy the new pair to the signal-cli host (CA host).** `ca.crt` doesn't change.
```
scp ~/new-certs/signal/server.crt ~/new-certs/signal/server.key you@SIGNAL_HOST:/tmp/
```

**3. Swap it in (signal-cli host).**
```
cd /apps/signal-relay/left/certs/signal
mkdir -p ~/cert-backup && cp -p server.crt server.key ~/cert-backup/     # the old pair, for rollback
mv /tmp/server.crt /tmp/server.key .
chmod 644 server.crt ca.crt && chmod 600 server.key
chown $(id -u):$(id -g) server.crt server.key        # the user in x-relay-user
```
Check that the key and the certificate belong together. The two lines must be identical:
```
openssl x509 -in server.crt -noout -pubkey | sha256sum
openssl pkey -in server.key -pubout | sha256sum
```

**4. Restart signal-cli (signal-cli host).**
```
cd /apps/signal-relay/left
docker compose up -d --force-recreate signal
docker logs --tail 20 signal-cli
```

**5. Confirm (subscriber host).** The subscriber retries every few seconds. `docker logs --tail 5 signal-subscriber`
should show "Connected to signal-cli". If it still fails, test the TLS connection directly, using the
subscriber's own certificate:
```
cd /apps/signal-relay/left/certs/subscriber
openssl s_client -connect SIGNAL_HOST:PORT -verify_ip SIGNAL_HOST -CAfile ca.crt \
  -cert client.crt -key client.key </dev/null 2>&1 | grep -E "Verify return|verify error"
```
Use `-verify_hostname` in place of `-verify_ip` for a DNS name. `Verify return code: 0 (ok)` means the certificate is
right.

**6. Clean up.** Run `rm -rf ~/new-certs` on the CA host. Once everything works, delete `~/cert-backup` on the
signal-cli host.

**To roll back:** copy the pair from `~/cert-backup` back into `certs/signal/` and repeat step 4.

**Tip:** a stable DNS name for the signal-cli host in the certificate and in `SIGNAL_URL` means an IP change needs only
a DNS update, not a new certificate.

### Names on the certificate
`tlscerts server` takes any number of names, separated by spaces. Each one becomes an entry the subscriber can use in
`SIGNAL_URL`:
```
./tlscerts server ubt2 sigprod.example.net 10.1.2.3 fd00::5 '*.example.net'
#   -> DNS:signal, DNS:ubt2, DNS:sigprod.example.net, IP:10.1.2.3, IP:fd00::5, DNS:*.example.net
```

| You give | It becomes | Notes |
|---|---|---|
| A host name (`ubt2`, `sigprod.example.net`) | `DNS:` | The subscriber must resolve it to the signal-cli host. |
| An IPv4 address (`10.1.2.3`) | `IP:` | Needed when `SIGNAL_URL` uses the address itself. A name that resolves to it doesn't cover it. |
| An IPv6 address (`fd00::5`) | `IP:` | Write it in `SIGNAL_URL` in brackets: `https://[fd00::5]:443`. |
| A wildcard (`'*.example.net'`) | `DNS:` | Quote it so the shell doesn't expand it. It covers `a.example.net`, but not `example.net` itself or `a.b.example.net`. |
| `signal` | (always added) | The container name, for one host. You don't need to list it. |

- **Give names only, with no `https://`, port or path.** The port isn't part of the certificate.
- **To add a name and keep the old ones,** run `./tlscerts show` and repeat every name it lists (except `signal`),
  plus the new one. `server` replaces the whole list. For example, if `show` lists
  `DNS:signal, DNS:ubt2, IP Address:172.16.0.46`:
  ```
  ./tlscerts server ubt2 172.16.0.46 sigprod.example.net
  ```
- **To remove a name,** run `server` again without it.
- **Only the signal-cli host needs the new certificate.** It comes from the same CA, so subscribers need no change.
  Then follow steps 2–6 above. If the CA host is also the signal-cli host and you didn't set `TLS_CERTS`, step 1
  already wrote it into `left/certs/signal`. Back up the old pair first, then run step 4.

## Monitoring (`signal-monitor`)
One `signal-monitor` per left host checks signal-cli (with its own certificate, CN `signal-monitor`, which must be in
`TLS_CLIENT_NAMES`), hears each subscriber's status (`MONITOR_ADDR`, `RELAY_ID`, `MONITOR_NAME`), checks disk and
certificate expiry, and reports it all across the diode on UDP 6511 to playback's Monitor tab in `../right`. It never
sees message content and never sends anything to Signal. Settings, split setup, a second instance and known limits:
**[`../docs/monitoring.md`](../docs/monitoring.md)**.

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
