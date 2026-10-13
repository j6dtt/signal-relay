# right/: the receiving side of the data diode

Runs the receiver and playback. The receiver listens at the diode's output, rebuilds each Signal message from its
CEF/UDP packets and delivers it. Playback is a temporary stand-in for Palantir: it shows the chat in a browser.
Nothing here can reach the left side (`../left`).

```
diode output ──UDP 6510──▶ receiver ──HTTP :8091 (this project's network only)──▶ playback ──HTTPS :8090──▶ browser
```

## Deploying
Copy this whole folder to the right host, then run `docker compose up -d` in it. It needs `python:3.13-alp-u3`.

In a fresh clone `playback/certs/` is empty (only a `.gitkeep`). Put the lab.int certificate and key there, then
`sudo chown -R <user>: playback/certs && sudo chmod 700 playback/certs && sudo chmod 600 playback/certs/*.key`.
`<user>` is the account the containers run as (`x-relay-user` at the top of `docker-compose.yml`).

| Folder | Holds |
|---|---|
| `receiver/` | `cef_to_json.py`, `comlibv3.py` |
| `playback/` | `playback_web.py`, `playback_web.html`, `chat_render.py` |
| `playback/certs/` | the `*.lab.int` certificate and key for the chat page. Keep it mode 700, owned by the user in `x-relay-user`. |

## Settings (in `docker-compose.yml`)
- **Receiver `ports:`** `"172.16.0.46:6510:6510/udp"`: where the diode output delivers UDP. On the dev host
  this is the host's LAN address. Anyone on that network could send packets to it.
- **Playback `ports:`** `"0.0.0.0:8090:8090"`: the chat page, over HTTPS. It has no login.
  - Browse to a `*.lab.int` name. Browsers need the Lab Internal CA installed.
- **Port 8091** is where the receiver posts to playback. It is never published.

When Palantir delivery is built (in the receiver's `deliver()`), the `playback` service is removed.

## After editing a script
Run `docker compose up -d --force-recreate <receiver|playback>`. A plain restart can keep the old file.
Recreating them also clears test messages and counters.
