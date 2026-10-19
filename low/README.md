# low/: the low side (Signal side) of the data diode

Runs signal-cli (the in-house image) and the subscriber. The subscriber sends each message of the groups in
`routes.conf` as CEF over UDP to the diode's input, once per room the group goes to; the header carries the room, and
the diode routes each room to its receiver. Nothing comes back. The other side is `../high`.

```
signal-cli ══mutual TLS 1.3══▶ subscriber ──CEF:0|CHAT|<ROOM>|──▶ diode input (diode-in:chat-port)    chat
    ▲ health check                 │ status every 10 s
    └──────────── signal-monitor ◀─┘ ──CEF:0|CHAT|MONITOR|──▶ diode input (diode-in:monitor-port)  monitoring
```

## Deploying
Everything that differs per host is in the **settings block** (`x-settings`) at the top of `docker-compose.yml`:
relay-user, signal-image, account, signal-url, diode-in, chat-port, monitor-port, docker-gid, message-log. Change the
values there only, then `docker compose up -d`. Which groups go to which room: `routes.conf` (below).

Copy this whole folder to the low-side host, then run `docker compose up -d` in it. It needs the image
`signal-cli:0.14.9-r2` (build it from `../image`, then `docker save` / `docker load`; r2 adds the health check the
diagram's "Signal servers" box needs) and `python:3.13-alp-u3`.

In a fresh clone of the repo, these folders exist but are empty (only a `.gitkeep`): keys, certificates and
logs are never committed. Fill them, then set the owner and mode:
`sudo chown -R <user>: signal-data certs logs && sudo chmod 700 signal-data certs certs/* logs`.
`<user>` is the account the containers run as (`relay-user` in the settings block of `docker-compose.yml`; in prod, the operator's own).

container-facts reads Docker through its socket, so it needs that socket's group on this host (dev's is 988). Set it
once, in this folder:
```
sed -i "s/group_add: \[\"[0-9]*\"\]/group_add: [\"$(stat -c %g /var/run/docker.sock)\"]/" docker-compose.yml
```
If it is wrong, signal-monitor's box says "can't read container facts" (`FACTS_UNAVAILABLE`).

| Folder | Holds | Needed on |
|---|---|---|
| `signal-data/` | the Signal account keys | signal-cli host only. Keep it mode 700, owned by the user in `relay-user` (settings block). |
| `certs/signal/` | signal-cli's TLS certificate | signal-cli host only |
| `subscriber/` | `subscriber_udp.py`, `comlibv3.py` | subscriber host only |
| `certs/subscriber/` | the subscriber's TLS certificate | subscriber host only |
| `logs/` | `signal-last-event-id` (where the subscriber stopped), `signal-udp-sent.jsonl` | subscriber host only |
| `monitor/` | `signal_monitor.py` | signal-cli host |
| `certs/signal-monitor/` | signal-monitor's TLS certificate (`../image/tlscerts client signal-monitor`) | signal-cli host |
| `facts/` | `container_facts.py`: read-only view of this project's containers for signal-monitor (holds the Docker socket) | each low-side host |

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
- **`UDP_DEST`** is the diode's input address (`<diode-input-ip>`). Without a diode (dev) it is the high-side host's
  address, where `../high` listens.

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

**1. Make the new certificate (CA host).** This writes to a separate folder, so the CA host's own `low/certs` isn't
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
cd /apps/signal-relay/low/certs/signal
mkdir -p ~/cert-backup && cp -p server.crt server.key ~/cert-backup/     # the old pair, for rollback
mv /tmp/server.crt /tmp/server.key .
chmod 644 server.crt ca.crt && chmod 600 server.key
chown $(id -u):$(id -g) server.crt server.key        # the user in relay-user (settings block)
```
Check that the key and the certificate belong together. The two lines must be identical:
```
openssl x509 -in server.crt -noout -pubkey | sha256sum
openssl pkey -in server.key -pubout | sha256sum
```

**4. Restart signal-cli (signal-cli host).**
```
cd /apps/signal-relay/low
docker compose restart signal
docker logs --tail 20 signal-cli
```

**5. Confirm (subscriber host).** The subscriber retries every few seconds. `docker logs --tail 5 signal-subscriber`
should show "Connected to signal-cli". If it still fails, test the TLS connection directly, using the
subscriber's own certificate:
```
cd /apps/signal-relay/low/certs/subscriber
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
./tlscerts server sighost sigprod.example.net 10.1.2.3 fd00::5 '*.example.net'
#   -> DNS:signal, DNS:sighost, DNS:sigprod.example.net, IP:10.1.2.3, IP:fd00::5, DNS:*.example.net
```

| You give | It becomes | Notes |
|---|---|---|
| A host name (`sighost`, `sigprod.example.net`) | `DNS:` | The subscriber must resolve it to the signal-cli host. |
| An IPv4 address (`10.1.2.3`) | `IP:` | Needed when `SIGNAL_URL` uses the address itself. A name that resolves to it doesn't cover it. |
| An IPv6 address (`fd00::5`) | `IP:` | Write it in `SIGNAL_URL` in brackets: `https://[fd00::5]:443`. |
| A wildcard (`'*.example.net'`) | `DNS:` | Quote it so the shell doesn't expand it. It covers `a.example.net`, but not `example.net` itself or `a.b.example.net`. |
| `signal` | (always added) | The container name, for one host. You don't need to list it. |

- **Give names only, with no `https://`, port or path.** The port isn't part of the certificate.
- **To add a name and keep the old ones,** run `./tlscerts show` and repeat every name it lists (except `signal`),
  plus the new one. `server` replaces the whole list. For example, if `show` lists
  `DNS:signal, DNS:sighost, IP Address:10.1.2.3`:
  ```
  ./tlscerts server sighost 10.1.2.3 sigprod.example.net
  ```
- **To remove a name,** run `server` again without it.
- **Only the signal-cli host needs the new certificate.** It comes from the same CA, so subscribers need no change.
  Then follow steps 2–6 above. If the CA host is also the signal-cli host and you didn't set `TLS_CERTS`, step 1
  already wrote it into `low/certs/signal`. Back up the old pair first, then run step 4.

## Monitoring (`signal-monitor`)
One `signal-monitor` per low-side host checks signal-cli (with its own certificate, CN `signal-monitor`, which must be in
`TLS_CLIENT_NAMES`), hears each subscriber's status (`MONITOR_ADDR`; names come from the container names), checks disk and
certificate expiry, and reports it all across the diode (its own route, `CEF:0|CHAT|MONITOR|`) to the console's
diagram in `../high`. Each app
is also watched from the inside: problems counted by code, stuck workers, every hop's losses counted, and (image r2)
signal-cli's own link to Signal's servers. It never sees message content and never sends anything to Signal. Settings, split setup, a second instance and known limits:
**[`../docs/monitoring.md`](../docs/monitoring.md)**.

## New number (service account)
To register a new number, such as a landline, as the relay's own account, follow `../docs/register-account.md`.

## Groups and rooms (`routes.conf`)
`routes.conf` (this folder, the same file on every low-side host) is the only list of relayed groups: per subscriber,
each room and its groups. A room is a route through the diode to one receiver (in prod: one Gotham room).
```
# subscriber         room       groups
signal-subscriber    RELAY      zCV6AAXf4WH28L62kT/iMZ4m6Mrck6ZIwiJhHupA5Ck=
signal-subscriber    SELFTEST   3Rr5QB/jXDXJ2XBmwu85/PBXcCueNoPIp/I0MxB4ARA=
```
- **Columns:** the subscriber's `ROUTES_FOR`, the room's short name (A-Z 0-9 - _; `MONITOR` is reserved), its group
  IDs (comma-separated; a line starting with a space continues the list).
- **After editing:** `docker compose restart subscriber` (each subscriber that has lines there).
- **It refuses to start** on any mistake, with the line and why: a bad room name, a malformed group ID, no lines for
  it, or the same group in the same room twice (on any subscriber: Gotham would get every message twice). A group
  may go to several rooms; it is sent once per room.
- **Every room needs** a receiver on the high side with that name in its `ROOMS`, and a diode route for it. The page
  calls a room by its Signal group(s), never by the short name.
- **A second subscriber:** uncomment the `subscriber-2` block (its own `container_name`, `ROUTES_FOR`,
  `STATE_FILE` and `SENT_LOG`; it shares the certificate) and give it lines in `routes.conf`. All subscribers read
  from the same signal-cli. Never run the same `ROUTES_FOR` twice: every message would be sent twice
  (`SUBSCRIBER_TWICE` on the diagram).
- **To find a group's ID:**
  - Stop signal-cli (`docker compose stop signal`), because the account can only be open once.
  - Run `../image/signalctl cli -a <account> listGroups` (`<account>`: the number in the settings block). It lists each group's ID and name.
  - Then `docker compose start signal`. Messages sent in the meantime wait on Signal's servers.
  - signal-cli's own log shows only a shortened ID.

## After editing a script
Each app's folder is mounted read-only (shared code in `shared/`), so after editing a script a restart is
enough: `docker compose restart <service>`. After editing `docker-compose.yml` (its settings block included):
`docker compose up -d`, which recreates only what changed.
