# Registering a new number for the relay (service account)

This runbook registers a phone number, such as a landline, as the relay's own Signal account. signal-cli on the left-side
host becomes the account's **main device**. No phone runs Signal for this number.

```
landline ── voice call with a 6-digit code ──▶ you ──▶ signalctl verify ──▶ low/signal-data (the account)
```

Do every step **on the prod signal-cli host**, in `/apps/signal-relay`. The account data is the account, so don't
register on another host and copy it over.

## 1. Before you start
- **The number must take Signal's call directly.** No extension, menu or operator. An automated voice reads out a
  6-digit code. Voicemail works if you can listen to it within a few minutes.
- **A browser** to solve a captcha. Any computer will do.
- **A Signal PIN** of at least 4 digits for registration lock (step 5). Store it in your password manager. Without it, nobody
  can register the number again for 7 days after the account goes inactive.
- **The user the containers run as.** This is `relay-user` (settings block) in `low/docker-compose.yml`; in prod it's your own account.
  `signalctl` must use the same one:
  ```
  export RELAY_USER=$(id -u):$(id -g)
  ```
- **The image** `signal-cli:0.14.9`, loaded with `docker image load -i image/signal-cli-0.14.9.tar` or pulled as
  `th0pham/signal-cli:0.14.9` and tagged `signal-cli:0.14.9`.

## 2. Prepare an empty account folder
signal-cli must not be running on this folder:
```
cd /apps/signal-relay/low && docker compose stop signal
ls -A signal-data            # should show only .gitkeep; if an old account is there, move it away first
chmod 700 signal-data
export SIGNAL_DATA=/apps/signal-relay/low/signal-data
export N=+1XXXXXXXXXX        # the new number, with country code
cd /apps/signal-relay/image
```

## 3. Request the code by voice call
1. **Get a captcha.** Signal almost always wants one:
   - Open https://signalcaptchas.org/registration/generate.html and solve it.
   - Right-click **Open Signal** and copy the link. It starts with `signalcaptcha://`.
   - Use it within a minute or two. Each captcha works only once.
2. **Ask for the call:**
   ```
   ./signalctl register $N --voice --captcha 'signalcaptcha://…'
   ```
   - **If the phone rings:** go to step 4.
   - **If you get an error instead:** Signal sometimes allows a call only after an SMS attempt. The error mentions SMS or a
     wait. In that case:
     1. Request an SMS (a landline won't receive it). Use a new captcha:
        `./signalctl register $N --captcha 'signalcaptcha://…'`.
     2. Wait 60 seconds.
     3. Ask for the call again, with another new captcha:
        `./signalctl register $N --voice --captcha 'signalcaptcha://…'`.
   - **"Rate limited":** you asked too often. Wait an hour or so, then start again.

## 4. Enter the code
```
./signalctl verify $N 123456
```
If the number already had registration lock (it was registered before, with a PIN), add `--pin 'PIN'`.

## 5. Turn on registration lock
Without it, anyone who can answer the landline could take the account.
```
./signalctl set-pin $N 'PIN'
```
To keep the PIN out of your shell history, put a space before the command. Most shells skip lines that start with a space.

## 6. Set the name the group sees
```
./signalctl cli -a $N updateProfile --given-name "Signal Relay"
```

## 7. Join the group
1. On a phone that's in the group: **Group settings → Group link**. Turn the link on and copy it.
2. Join:
   ```
   ./signalctl cli -a $N joinGroup --uri 'https://signal.group/#…'
   ```
   Adding the number from a phone also works. But because the number isn't in anyone's contacts, it can arrive as a pending
   invitation instead.
3. Get the group's ID for the subscriber:
   ```
   ./signalctl cli -a $N listGroups
   ```
   It shows each group's ID and name. Turn the group link off again if you don't need it.

## 8. Point the relay at the new account
- **The number:** in `low/.env` (gitignored, mode 600), `SIGNAL_ACCOUNT=+<the new number>`. The settings block's
  `account` reads it from there, so the number never goes into the repository.
- **The groups:** the group ID from step 7, in `low/routes.conf` (which groups go to which room).
- **On the subscriber host**, delete the subscriber's saved position: `rm low/logs/signal-last-event-id`.
  It belongs to the old account.

Then start it:
```
cd /apps/signal-relay/low && docker compose up -d
docker logs signal-cli          # "Starting daemon in single-account mode for +1…"
docker logs signal-subscriber   # "Connected to signal-cli …"
```
Send a test message in the group. It should appear in `docker logs signal-cli` and on the right side.

## 9. Back up the account
If the account data is lost, the only way back is to register again. That creates a new identity: group members see a
changed safety number, and the account must rejoin the group.

Back it up now, and again after any change to the account. Stop signal-cli first and encrypt the copy:
```
cd /apps/signal-relay/low && docker compose stop signal
tar -C signal-data -cf - . | gpg --symmetric --cipher-algo AES256 -o ~/signal-account-$(date -u +%Y%m%d).tar.gpg
docker compose start signal
```
Keep the file and its passphrase off this host. The backup holds the account's private keys.

## Rules for a service account
- **Never register the number anywhere else,** including dev or a phone. A new registration logs out this one.
- **Keep signal-cli running.** The main device keeps the account active. Signal deletes accounts that don't connect for
  months, and registration lock ends after 7 days inactive.
- **Never run two signal-cli instances on `signal-data`.** Stop the `signal` service before any `signalctl` command on
  this account, because the account can only be open once.
- **A later sender** (see `CLAUDE.md`) becomes a *linked device* of this account. The sender's signal-cli runs
  `link` and shows a `sgnl://` link, and this account approves it:
  `./signalctl cli -a $N addDevice --uri 'sgnl://…'`. No phone is needed.

## If something goes wrong
| Problem | What to do |
|---|---|
| The captcha is refused | Captchas expire quickly and work only once. Get a new one and run the command again straight away. |
| No call comes | Check the number has no menu or extension in front. Wait a few minutes, then request again with a new captcha. |
| `verify` says the code is wrong or expired | Request a new call (step 3). |
| `verify` asks for a PIN | The number had registration lock from an earlier registration. Use that PIN, or wait 7 days. |
| `Permission denied` on `/data` | `signal-data` isn't owned by `RELAY_USER`. Run `sudo chown -R $(id -u):$(id -g) signal-data`. |
| "Config file is in use by another instance" | signal-cli is still running. Run `docker compose stop signal` in `low/`. |
