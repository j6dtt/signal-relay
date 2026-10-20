# `tlscerts`: certificates for the signal-cli ↔ subscriber link

`tlscerts` makes the certificates for the one encrypted link in the relay: the subscriber reading signal-cli's event
stream through the TLS gateway (mutual TLS 1.3). It runs on the host with `openssl`. It needs no container and no
internet.

```
            our own CA (certs/ca/, kept offline)
              │ signs                    │ signs
              ▼                          ▼
   server certificate             client certificate
   low/certs/signal/             low/certs/subscriber/
   "I am signal-cli, reachable    "I am signal-subscriber"
    as these names"
              │                          │
   signal-cli's TLS gateway ◀══ TLS 1.3 ══ subscriber
   checks: client cert from our CA,       checks: server cert from our CA,
           CN in gateway/clients.conf             a name matching SIGNAL_URL
```

Each side trusts only this CA. No public CA is involved, and nothing else can use the link.

Contents:
- [Commands](#commands)
- [Settings](#settings)
- [What it writes](#what-it-writes)
- [What is in each certificate](#what-is-in-each-certificate)
- [Names on the server certificate](#names-on-the-server-certificate)
- [Recipes](#recipes)
- [Checking certificates](#checking-certificates)
- [Troubleshooting](#troubleshooting)
- [Things to know](#things-to-know)

## Commands
Run it from `image/` (or by full path). Run with no command to print the built-in help.

| Command | Does | Who needs the result |
|---|---|---|
| `init NAME...` | Creates the CA, then a server and a client certificate | Both hosts |
| `server NAME...` | A new server certificate from the same CA | signal-cli host only |
| `client [NAME]` | A new client certificate from the same CA | That client's host (+ its line in `low/gateway/clients.conf`: added for you) |
| `show` | Prints each certificate's name, expiry and server names | (read only) |

### `init NAME...`: first set-up
Creates the CA, the server certificate and the client certificate in one go. `NAME` is every host name or IP address
the subscriber may use to reach signal-cli. `signal` is always added, so with one host you can give no names at all.
```
$ ./tlscerts init sighost 10.1.2.3
Created the CA in /apps/signal-relay/certs/ca (valid 1826 days).
Server certificate for: DNS:signal, DNS:sighost, IP:10.1.2.3
Client certificate for: signal-subscriber
certs/ca/ca.crt:
  subject=O = signal-relay, CN = signal-relay CA
  notAfter=Oct 17 14:00:56 2031 GMT
low/certs/signal/server.crt:
  subject=O = signal-relay, CN = signal-cli gateway
  notAfter=Oct 17 14:00:56 2031 GMT
  X509v3 Subject Alternative Name:
      DNS:signal, DNS:sighost, IP Address:10.1.2.3
low/certs/subscriber/client.crt:
  subject=O = signal-relay, CN = signal-subscriber
  notAfter=Oct 17 14:00:56 2031 GMT
```
- It refuses to run if the CA already exists, so it never replaces a CA by accident:
  ```
  tlscerts: /apps/signal-relay/certs/ca already exists. Use 'server' or 'client' to reissue, or move it away first.
  ```
- A new CA means **both** hosts need new files: the old certificates are no longer trusted.

### `server NAME...`: new server certificate
Use it to add or change a name (a new host, a new IP), or to replace the key. Same CA, so the subscriber needs no
change.
```
$ ./tlscerts server sighost 10.1.2.3 sigprod.example.net
Server certificate for: DNS:signal, DNS:sighost, IP:10.1.2.3, DNS:sigprod.example.net
...(show output)...
Restart signal-cli to use it, in low/: docker compose restart signal
```
- **It replaces the whole list of names.** To keep the old names, list them again (see
  [Add a name](#add-a-name-keep-the-old-ones)).
- It needs the CA key. Without it:
  ```
  tlscerts: no CA in /apps/signal-relay/certs/ca (run: ./tlscerts init NAME...)
  ```

### `client [NAME]`: new client certificate
The name becomes the certificate's CN. The gateway lets a client in only if that CN is a line in
`low/gateway/clients.conf` (image r5: signal-cli reads it again when it changes, so no restart). `client` adds the
line for you on this host.

**No NAME:** replaces the default client, `signal-subscriber`, in `subscriber/`. Use it if that key may have leaked,
or the certificate is near expiry. signal-cli needs no change.
```
$ ./tlscerts client
Client certificate for: signal-subscriber (in low/certs/subscriber)
...(show output)...
Copy subscriber/ to the subscriber host, then in low/: docker compose restart subscriber
```

**With a NAME:** a separate client with its own key, in its own folder `NAME/`. Use one per subscriber host or tool,
so signal-cli's log shows who connected and you can lock one out on its own (see
[Several clients](#several-clients-one-name-each)).
```
$ ./tlscerts client subscriber-prod2
Client certificate for: subscriber-prod2 (in low/certs/subscriber-prod2)
...(show output)...
Added subscriber-prod2 to low/gateway/clients.conf (signal-cli lets it in at its next connection, no restart).
Copy subscriber-prod2/ to that client's host (mounted at /certs in its block).
If signal-cli runs on another host: add the line "subscriber-prod2" to low/gateway/clients.conf there too.
```
- Names: letters, digits, `.`, `_` and `-`, up to 64 characters. `signal`, `subscriber` and `ca` are refused (those
  folders are taken). One name per run.
- Running it again with the same NAME replaces that client's key and certificate.
- **An old certificate still works** until it expires: the gateway trusts anything the CA signed with an allowed
  name. To lock one out, remove its line from `low/gateway/clients.conf` (no restart), or, for the default name, make a new CA (see
  [Start over with a new CA](#start-over-with-a-new-ca)).

### `show`: what is there now
Read only. Prints the CA, the server certificate and every client certificate (those that exist), with expiry dates and the server's
names.
```
$ ./tlscerts show
certs/ca/ca.crt:
  subject=O = signal-relay, CN = signal-relay CA
  notAfter=Oct 17 14:00:56 2031 GMT
low/certs/signal/server.crt:
  subject=O = signal-relay, CN = signal-cli gateway
  notAfter=Oct 17 14:00:56 2031 GMT
  X509v3 Subject Alternative Name:
      DNS:signal, DNS:sighost, IP Address:10.1.2.3
low/certs/subscriber/client.crt:
  subject=O = signal-relay, CN = signal-subscriber
  notAfter=Oct 17 14:00:56 2031 GMT
```
On a host with only one folder (say, the signal-cli host), it shows only that one. `TLS_CERTS` changes where it
looks.

## Settings
Set them in front of the command, e.g. `TLS_CERTS=$HOME/new-certs ./tlscerts server sighost`.

| Variable | Default | Meaning |
|---|---|---|
| `TLS_CERTS` | `low/certs` | Where `signal/` and `subscriber/` are written (and read by `show`) |
| `TLS_CA_DIR` | `certs/ca` | Where the CA is created (`init`) or read from (`server`, `client`) |
| `TLS_DAYS` | `1826` (5 years) | How long new certificates are valid. `init` uses it for the CA too |

The default client name is `signal-subscriber` (`client` with no NAME). Every client name in use must be a line in
`low/gateway/clients.conf`, optionally followed by what it may use (`events,check,rpc`).

## What it writes
| Folder | Files | Goes to | Mounted at |
|---|---|---|---|
| `certs/ca/` | `ca.key`, `ca.crt` | **Nowhere.** Stays on the CA host | never |
| `low/certs/signal/` | `server.crt`, `server.key`, `ca.crt` | signal-cli host | `/certs` in `signal-cli` |
| `low/certs/subscriber/` | `client.crt`, `client.key`, `ca.crt` | subscriber host(s) | `/certs` in each subscriber |
| `low/certs/NAME/` | the same three | client NAME's host | `/certs` in that client |

- Folders are mode 700, keys 600, certificates 644 (`ca.crt` copies are 600, which is fine).
- The files are owned by whoever ran `tlscerts`. They must be owned by the user the containers run as
  (`relay-user` (settings block) in the compose files). If that's someone else:
  ```
  sudo chown -R UID:GID low/certs
  ```
  `certs/ca` stays with you.
- Subscribers on one host (`subscriber`, `subscriber-2`, …) can share one client certificate. Give each host or tool
  its own NAME when you want to tell them apart.

## What is in each certificate
All three use ECDSA P-256 keys and SHA-256 signatures.

| | CA | Server | Client |
|---|---|---|---|
| Subject | `O=signal-relay, CN=signal-relay CA` | `O=signal-relay, CN=signal-cli gateway` | `O=signal-relay, CN=signal-subscriber` |
| Can sign others | yes, one level only (`pathlen:0`) | no | no |
| Used for | signing | TLS server (`serverAuth`) | TLS client (`clientAuth`) |
| Names (SAN) | — | `signal` + your names | — |
| Valid | `TLS_DAYS` from `init` | `TLS_DAYS` from issue | `TLS_DAYS` from issue |

Because of the "used for" field, the server certificate can't be used as a client certificate (tested: the gateway
refuses it), and the other way round.

## Names on the server certificate
The subscriber checks that the host in its `SIGNAL_URL` is one of the server certificate's names. Each `NAME` you give
becomes one entry:
```
$ ./tlscerts server sighost sigprod.example.net 10.1.2.3 fd00::5 '*.example.net'
Server certificate for: DNS:signal, DNS:sighost, DNS:sigprod.example.net, IP:10.1.2.3, IP:fd00::5, DNS:*.example.net
```

| You give | It becomes | `SIGNAL_URL` it allows |
|---|---|---|
| (nothing) | `DNS:signal` | `https://signal:8443` (one host) |
| `sighost` | `DNS:sighost` | `https://sighost:443` |
| `sigprod.example.net` | `DNS:sigprod.example.net` | `https://sigprod.example.net:443` |
| `10.1.2.3` | `IP:10.1.2.3` | `https://10.1.2.3:443` |
| `fd00::5` | `IP:fd00::5` | `https://[fd00::5]:443` |
| `'*.example.net'` | `DNS:*.example.net` | `https://a.example.net:443`, not `example.net` or `a.b.example.net` |

- Anything made only of digits and dots is taken as an IPv4 address; anything with a `:` as IPv6. Everything else is
  a DNS name.
- **An IP in `SIGNAL_URL` needs the IP on the certificate.** A DNS name that resolves to that IP doesn't cover it,
  and the other way round.
- **Names only:** no `https://`, port or path. The port isn't part of a certificate. `tlscerts` refuses these, and
  invalid IPv4 addresses or host names, with a message.
- Quote wildcards so the shell doesn't expand them.
- Prefer a stable DNS name: then an IP change needs only a DNS update, not a new certificate.

## Recipes
Paths below assume the repo is at `/apps/signal-relay`.

### One host (dev, as shipped)
```
cd /apps/signal-relay/image
./tlscerts init
```
The certificate covers `signal`, and `SIGNAL_URL=https://signal:8443` works as shipped.

### Two hosts (signal-cli and the subscriber split)
On the CA host, with every name the subscriber may use:
```
cd /apps/signal-relay/image
./tlscerts init sighost sighost.example.net 10.1.2.3
scp -r ../low/certs/signal     you@sighost:/apps/signal-relay/low/certs/
scp -r ../low/certs/subscriber you@subhost:/apps/signal-relay/low/certs/
```
On the subscriber host, `SIGNAL_URL` uses one of those names, e.g. `https://sighost.example.net:443`.

### Add a name, keep the old ones
`server` replaces the whole list, so read the current list first and repeat it:
```
$ ./tlscerts show | grep -A1 "Alternative Name"
  X509v3 Subject Alternative Name:
      DNS:signal, DNS:sighost, IP Address:10.1.2.3
$ ./tlscerts server sighost 10.1.2.3 sigprod.example.net
Server certificate for: DNS:signal, DNS:sighost, IP:10.1.2.3, DNS:sigprod.example.net
```
`signal` is added again on its own, so you can leave it out. Then restart signal-cli:
```
cd ../low && docker compose restart signal
```

### Remove a name
Run `server` with the list minus that name. The old certificate is replaced, so the removed name stops working once
signal-cli restarts.

### Issue for another host without touching your own
`TLS_CERTS` sends the output to a separate folder, so this host's `low/certs` stays as it is:
```
TLS_CERTS=$HOME/new-certs ./tlscerts server sighost 10.1.2.3
scp ~/new-certs/signal/server.crt ~/new-certs/signal/server.key you@sighost:/tmp/
rm -rf ~/new-certs        # once copied
```
The step-by-step swap on the signal-cli host (backup, permissions, key check, restart, test, rollback) is in
[`low/README.md`: Replacing signal-cli's certificate](../low/README.md#replacing-signal-clis-certificate).

### Replace the subscriber's certificate
```
TLS_CERTS=$HOME/new-certs ./tlscerts client
scp ~/new-certs/subscriber/client.crt ~/new-certs/subscriber/client.key you@subhost:/tmp/
```
On the subscriber host: back up `low/certs/subscriber/client.*`, move the two new files in (key 600, owned by the
`relay-user` (settings block) user), then `docker compose restart subscriber` (and each `subscriber-2` …).

### Several clients, one name each
For example, a second subscriber host and `tools/dump.py`, each with its own certificate:
```
cd /apps/signal-relay/image
./tlscerts client subscriber-prod2
./tlscerts client dump-tool
```
1. **signal-cli host,** `low/gateway/clients.conf` (on this host `client` already added them):
   ```
   signal-subscriber
   subscriber-prod2
   dump-tool  events
   signal-monitor  check
   ```
   Nothing to restart: signal-cli reads the file again when it changes.
2. **Each client's host:** copy its folder into `low/certs/`, owned by the `relay-user` (settings block) user, and point the mount at
   it:
   ```
   - ./certs/subscriber-prod2:/certs:ro
   ```
   `tools/dump.py` always reads `low/certs/subscriber`. To run it as `dump-tool`, copy that client's three files
   into a `subscriber/` folder next to a copy of `tools/`, or use the default certificate.
3. **Check signal-cli's log.** Each connection shows its name:
   ```
   Opened the message stream for 10.1.2.7 (subscriber-prod2)
   ```

**To lock one client out,** delete its line from `low/gateway/clients.conf` (no restart, image r5). Its next connection
is refused (tested on dev):
```
Refused a connection from 172.24.0.1: its certificate is for "test-tool", not an allowed client.
```
The others carry on. This works for any name except one you can't drop, such as `signal-subscriber` while the main
subscriber still uses it; for that, use a new CA.

### A short-lived test certificate
```
TLS_DAYS=30 TLS_CERTS=/tmp/test-certs ./tlscerts server testhost
```

### Start over with a new CA
Needed when the CA key may be known to others, or when the CA nears its expiry (all three certificates from `init`
expire on the same day). The old CA keeps working until both hosts switch.
```
cd /apps/signal-relay/image
TLS_CA_DIR=$HOME/ca-2031 TLS_CERTS=$HOME/certs-2031 ./tlscerts init sighost 10.1.2.3
```
1. Copy `~/certs-2031/signal/` to the signal-cli host and `~/certs-2031/subscriber/` to every subscriber host, into
   `low/certs/` (back up the old folders first).
2. Restart signal-cli, then the subscribers. Between the two, the subscriber logs connection errors and retries; it
   resumes from its saved event ID, so nothing is lost while signal-cli is running.
3. Keep the new CA offline where you made it (use `TLS_CA_DIR=$HOME/ca-2031` for later `server`/`client` runs).
   Delete the old CA once both hosts work.

## Checking certificates
Run these in the folder that holds the files.

**Names and expiry:**
```
openssl x509 -in server.crt -noout -subject -enddate -ext subjectAltName
```

**Signed by our CA:**
```
$ openssl verify -CAfile ca.crt server.crt
server.crt: OK
```

**Key and certificate belong together.** The two lines must be identical:
```
openssl x509 -in server.crt -noout -pubkey | sha256sum
openssl pkey -in server.key -pubout | sha256sum
```

**A live test against signal-cli,** from the subscriber host, with the subscriber's own files (in
`low/certs/subscriber`). This is exactly what the subscriber does:
```
$ openssl s_client -connect sighost:443 -verify_hostname sighost -CAfile ca.crt \
    -cert client.crt -key client.key </dev/null 2>&1 | grep -E "Verify return|verify error"
Verify return code: 0 (ok)
```
For an IP, use `-verify_ip 10.1.2.3`. A name that isn't on the certificate shows:
```
verify error:num=64:IP address mismatch
Verify return code: 64 (IP address mismatch)
```

## Troubleshooting
**Subscriber log: `IP address mismatch` or `Hostname mismatch`.**
- The host in `SIGNAL_URL` isn't on the server certificate.
- Fix: issue a new server certificate that includes it (see [Add a name](#add-a-name-keep-the-old-ones)), or change
  `SIGNAL_URL` to a name `show` lists.

**Subscriber log: `certificate verify failed: unable to get local issuer certificate`.**
- The subscriber's `ca.crt` isn't the CA that signed the server certificate. Usually: a new CA on one side only.
- Fix: copy the matching `ca.crt`, or the whole folder from the same `init`.

**Subscriber log: `certificate has expired`.**
- Check `show`. Issue new ones; if the CA itself expired, start over with a new CA.

**signal-cli's log: the gateway refuses the client** (no certificate, wrong CA, or a name not in
`low/gateway/clients.conf`).
- Check the client certificate with `openssl verify -CAfile ca.crt client.crt` against the server's `ca.crt`.
- Check that its subject's CN is a line in `low/gateway/clients.conf` (exact, case-sensitive):
  `openssl x509 -in client.crt -noout -subject`.
- The log line names the CN it saw: `its certificate is for "X", not an allowed client`.

**signal-cli won't start after a swap, or the gateway can't read its key.**
- The files aren't owned by the `relay-user` (settings block) user, or `server.key` doesn't match `server.crt` (run the key check
  above).

**`tlscerts` refuses a name.** It checks every name before it touches any file, so nothing is changed:
```
tlscerts: 'sighost:8443': give the name only, without a port (e.g. sighost); the port isn't part of a certificate
tlscerts: 'https://sighost': give the name only, without https://, a port or a path (e.g. sighost)
tlscerts: '10.1.2.300' is not a valid IPv4 address
tlscerts: 'bad_name' is not a valid host name (letters, digits, - and . only; a wildcard is '*.example.net')
```
Run it again with the name it suggests.

## Things to know
- **The CA key is the master key.** Anyone with `certs/ca/ca.key` can make a client certificate the gateway accepts.
  Keep it offline, never mount it, never copy it to a deployed host. The dev CA in this repo is public: never use it
  for prod. Make prod's own with `TLS_CA_DIR` pointing outside the repo.
- **`server` and `client` replace files in place.** Back up the old pair first if signal-cli or the subscriber is
  running from that folder. A failed run (a bad name, an openssl error) leaves the old pair as it was.
- **A certificate can't outlive its CA.** A certificate issued in year 4 still says "valid 5 years", but it stops
  working when the CA expires. Plan a new CA before the CA's date in `show`.
- **There is no revocation list.** To retire a certificate before it expires, delete its line from
  `low/gateway/clients.conf` (so give each client its own name), or make a new CA.
- **A container reads the files when it starts.** After a swap, restart it: `docker compose restart …` (the
  certificate folders are mounted whole, so a restart sees the new files).
- **Check the `Server certificate for:` line after every run.** It lists exactly the names the subscriber can use.
