#!/usr/bin/env python3
"""Show the Signal chat in a browser. Display only: events arrive as JSON from cef_to_json.py (signal-receiver).

Dev: the final destination. Prod: runs next to the Palantir Gotham feed (my_forward's records), for backend checks
and troubleshooting. It gets every rebuilt event, plus lost-message notices.
One tab per group (by group ID, created on its first message), plus "All": every group in one stream, each line tagged
with its group, and the lost-message notices (a lost message's group is unknown). Each tab keeps its own HISTORY.
Also the Monitor tab (monitor.py): the state of every part of the relay, from monitor-receiver's messages.
History (chat, monitoring, users, sessions) is kept in DB_PATH (SQLite, store.py) for RETENTION_DAYS and restored
at start. The page needs a login (local users: playback_users.py).
Two ports:
  WEB_PORT    (8090)  "/login", chat page "/" (playback_web.html), live updates "/ws", and the read-only
                      /api/monitor/state and /api/monitor/events (JSON, for tools). HTTPS when WEB_TLS_CERT and
                      WEB_TLS_KEY are set (PEM files); it then refuses to start without them.
  INGEST_PORT (8091)  POST /event (one Signal event), /notice ({"text"}), /stats (receiver counters),
                      /monitor (from monitor-receiver); keep this port private: anything posted here is shown
    python playback_web.py        # then:  PLAYBACK_URL=http://127.0.0.1:8091 python cef_to_json.py
"""
import asyncio, json, logging, os, re, ssl, time, zlib
from urllib.parse import urlsplit
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from aiohttp import web, WSMsgType
from chat_render import Chat, describe
import auth
from monitor import Monitor
from store import Store, chat_key, now_ms

WEB       = (os.environ.get("WEB_HOST", "127.0.0.1"), int(os.environ.get("WEB_PORT", 8090)))       # browsers
INGEST    = (os.environ.get("INGEST_HOST", "127.0.0.1"), int(os.environ.get("INGEST_PORT", 8091)))  # cef_to_json.py
TLS_CERT  = os.environ.get("WEB_TLS_CERT")   # e.g. /certs/ssl.lab.int.crt (with its CA chain, if any)
TLS_KEY   = os.environ.get("WEB_TLS_KEY")    # e.g. /certs/ssl.lab.int.key
PAGE      = Path(__file__).with_name("playback_web.html")
LOGIN     = Path(__file__).with_name("playback_login.html")
DB_PATH   = os.environ.get("DB_PATH", "relay.db")             # SQLite: history, monitoring, users (message content)
RETENTION = int(os.environ.get("RETENTION_DAYS", 30))         # days; anything older is deleted every hour
COOKIE    = "relay_session"
HISTORY   = 500                    # chat lines kept per tab, for browsers that connect later
MAX_ROOMS = int(os.environ.get("MAX_ROOMS", 50))   # group tabs at most; more groups still show in "All"
ALL       = "all"                  # the "All" tab's id (group IDs are base64, so they never clash)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("playback")


# ---------- chat state: renders events like a terminal (chat_render.Chat), as styled lines ----------
ANSI = re.compile(r"\x1b\[([0-9;]*)m")


def ansi_to_segments(line):
    """Split a terminal line into [{"t": text, "k": "dim bold c93"}] using its ANSI colour codes."""
    segs, style = [], set()
    pos = 0
    for m in ANSI.finditer(line):
        if m.start() > pos:
            segs.append({"t": line[pos:m.start()], "k": " ".join(sorted(style))})
        for code in (m.group(1) or "0").split(";"):
            if code == "0":
                style = set()
            elif code == "1":
                style.add("bold")
            elif code == "2":
                style.add("dim")
            elif code.isdigit():
                style = {k for k in style if not k.startswith("c")} | {f"c{code}"}
        pos = m.end()
    if pos < len(line):
        segs.append({"t": line[pos:], "k": " ".join(sorted(style))})
    return segs


class TermView:
    """Terminal-style view: chat_render.Chat lines, as styled segments."""
    def __init__(self, publish, show_group=False):
        self.publish = publish
        self.chat = Chat(show_group=show_group)   # "All" tags each line with its group; a group's tab doesn't
        self.chat.color = True         # always produce colour codes; they become CSS classes
        self.lines = deque(maxlen=HISTORY)
        self.day = None                # date of the last line shown; a date line is added when it changes

    def _add(self, line):
        segs = ansi_to_segments(line)
        self.lines.append(segs)
        self.publish({"op": "line", "segs": segs})

    def system(self, text):
        self._add(self.chat.dim(f"— {text} —"))

    def handle(self, event):
        line = self.chat.render(event)
        if line:
            ts = self.chat.shown_ms(event.get("envelope", {})) / 1000
            day = datetime.fromtimestamp(ts, timezone.utc).strftime("%A, %d %b %Y")   # UTC, like Chat's [HH:MM:SS]
            if day != self.day:
                self.day = day
                self._add(self.chat.dim(f"──── {day} (UTC) ────"))
            self._add(line)
        return bool(line)


class BubbleView:
    """Chat-app view: bubbles; edits, deletes and reactions update the message they belong to."""
    def __init__(self, publish, chat):
        self.publish = publish
        self.chat = chat               # shared with TermView: its learned names are used for mentions
        self.items = OrderedDict()     # id -> item (messages and system notes), oldest first
        self.by_key = {}               # (author_uuid, sent_ts) -> item id
        self.next_id = 0

    def _id(self):
        self.next_id += 1
        return self.next_id

    @staticmethod
    def _public(item):
        return {k: v for k, v in item.items() if k != "key"}

    def _keep(self, item):
        self.items[item["id"]] = item
        while len(self.items) > HISTORY:
            old = self.items.popitem(last=False)[1]
            self.by_key.pop(old.get("key"), None)
        self.publish({"op": "add", "item": self._public(item)})

    def _update(self, item, **changes):
        item.update(changes)
        self.publish({"op": "update", "item": self._public(item)})

    def _target(self, uuid, ts):
        iid = self.by_key.get((uuid, ts))
        return self.items.get(iid) if iid else None

    def system(self, text, ts=None):
        self._keep({"id": self._id(), "type": "system", "ts": ts or int(time.time() * 1000), "text": text})

    def handle(self, event):
        env = event.get("envelope", {})
        name = env.get("sourceName") or env.get("sourceNumber") or "unknown sender"
        uuid = env.get("sourceUuid") or name
        ts = env.get("timestamp", 0)            # the sender's: Signal's key for this message
        shown = self.chat.shown_ms(env)         # the server's: what the page shows
        edit = env.get("editMessage")
        dm = ((edit or {}).get("dataMessage") or env.get("dataMessage")
              or (env.get("syncMessage") or {}).get("sentMessage"))
        if not dm:
            return False
        gi = dm.get("groupInfo") or {}
        group = gi.get("groupName") or (gi.get("groupId") or "")[:8]
        own = bool(event.get("account")) and event.get("account") == env.get("sourceNumber")
        text = self.chat.with_mentions(dm.get("message"), dm.get("mentions"))

        if edit:
            target = self._target(uuid, edit.get("targetSentTimestamp"))
            if target:
                self._update(target, text=text, edited=True)
            else:
                self._message(env, dm, name, uuid, own, group, text,
                              key=(uuid, edit.get("targetSentTimestamp")), edited=True)
        elif dm.get("remoteDelete"):
            target = self._target(uuid, dm["remoteDelete"].get("timestamp"))
            if target:
                self._update(target, deleted=True, text=None, attachments=[], quote=None)
            else:
                self.system(f"{name} deleted an earlier message", shown)
        elif dm.get("reaction"):
            r = dm["reaction"]
            target = self._target(r.get("targetAuthorUuid"), r.get("targetSentTimestamp"))
            emoji = r.get("emoji", "")
            if target:
                reactions = {k: v for k, v in target.get("reactions", {}).items() if k != uuid}
                if not r.get("isRemove"):
                    reactions[uuid] = {"emoji": emoji, "name": name}
                self._update(target, reactions=reactions)
            else:
                verb = "removed a reaction from" if r.get("isRemove") else f"reacted {emoji} to"
                self.system(f"{name} {verb} an earlier message", shown)
        elif gi.get("type") in ("UPDATE", "QUIT") and not text and not dm.get("attachments"):
            self.system(f"{name} {'left the group' if gi['type'] == 'QUIT' else 'updated the group'}", shown)
        elif dm.get("isExpirationUpdate"):
            secs = dm.get("expiresInSeconds", 0)
            self.system(f"{name} set disappearing messages to {f'{secs}s' if secs else 'off'}", shown)
        else:
            self._message(env, dm, name, uuid, own, group, text, key=(uuid, ts))
        return True

    def _message(self, env, dm, name, uuid, own, group, text, key, edited=False):
        quote = None
        if dm.get("quote"):
            q = dm["quote"]
            target = self._target(q.get("authorUuid"), q.get("id"))
            quote = {"name": (target or {}).get("name") or self.chat.quote_author(q) or "earlier message",
                     "text": (target or {}).get("text") or q.get("text") or ""}
        atts = []
        for a in dm.get("attachments") or []:
            kind = (a.get("contentType") or "file").split(";")[0]
            size = f" {a['size'] / 1024:.0f} KB" if a.get("size") else ""
            atts.append(f"{a.get('filename') or kind}{size}")
        item = {"id": self._id(), "type": "msg", "key": key, "ts": self.chat.shown_ms(env), "group": group,
                "name": name, "color": zlib.crc32(uuid.encode()) % 12, "own": own, "text": text,
                "quote": quote, "attachments": atts, "sticker": bool(dm.get("sticker")),
                "edited": edited, "deleted": False, "reactions": {}}
        self.by_key[key] = item["id"]
        self._keep(item)


class ChatView:
    """One tab: feeds every event to both views; the browser shows whichever one the viewer picks."""
    def __init__(self, publish, room, name, show_group=False):
        self.room, self.name = room, name
        self.count, self.last = 0, None          # messages shown, and the server time (ms) of the latest
        tagged = lambda op: publish({**op, "room": room})
        self.term = TermView(tagged, show_group)
        self.bubbles = BubbleView(tagged, self.term.chat)   # term.handle runs first, so names are learned

    def info(self):
        return {"id": self.room, "name": self.name, "count": self.count, "last": self.last}

    def system(self, text, ts=None):
        self.term.system(text)
        self.bubbles.system(text[:1].upper() + text[1:] + ".", ts)

    def snapshot(self):
        return {**self.info(), "lines": list(self.term.lines),
                "items": [BubbleView._public(i) for i in self.bubbles.items.values()]}

    def handle(self, event):
        """Render one Signal event. True if it is shown in the terminal view (a chat message)."""
        shown = self.term.handle(event)
        self.bubbles.handle(event)
        if shown:
            self.count += 1
            self.last = self.term.chat.shown_ms(event.get("envelope", {}))
        return shown


def group_of(event):
    """(group ID, group name) of a Signal event, or (None, None)."""
    env = event.get("envelope", {})
    edit = env.get("editMessage")
    dm = ((edit or {}).get("dataMessage") or env.get("dataMessage")
          or (env.get("syncMessage") or {}).get("sentMessage") or {})
    gi = dm.get("groupInfo") or {}
    return gi.get("groupId"), gi.get("groupName")


class Rooms:
    """The "All" tab plus one tab per group, keyed by group ID (names can change, and two groups can share one)."""
    def __init__(self, publish):
        self.publish = publish
        self.all = ChatView(publish, ALL, "All", show_group=True)
        self.groups = {}                 # group ID -> ChatView, in the order first seen
        self.full_warned = False

    def _room(self, gid, gname):
        room = self.groups.get(gid)
        if room:
            if gname and gname != room.name:
                log.info("Group %s… is now called \"%s\" (was \"%s\"). Renamed its tab.", gid[:8], gname, room.name)
                room.name = gname
            return room
        if len(self.groups) >= MAX_ROOMS:
            if not self.full_warned:
                log.warning("There are already %d group tabs (MAX_ROOMS), so group %s… is shown in \"All\" only.",
                            MAX_ROOMS, gid[:8])
                self.full_warned = True
            return None
        room = self.groups[gid] = ChatView(self.publish, gid, gname or gid[:8])
        log.info("New group \"%s\" (%s…): added a tab for it.", room.name, gid[:8])
        self.publish({"op": "room", "room": room.info()})        # before its first line, so the page has the tab
        return room

    def handle(self, event):
        """Render one Signal event in "All" and in its group's tab. A plain-English description, or None if not shown."""
        gid, gname = group_of(event)
        shown = self.all.handle(event)
        if not shown:
            return None                  # receipts, typing, …: no tab is created for them
        room = self._room(gid, gname) if gid else None
        if room:
            room.handle(event)
            self.publish({"op": "room", "room": room.info()})
        self.publish({"op": "room", "room": self.all.info()})
        return describe(event)

    def system(self, text, ts=None):
        """A notice from the receiver (e.g. a lost message): "All" only, because its group is unknown."""
        self.all.system(text, ts)

    def snapshot(self):
        return {"op": "snapshot", "rooms": [self.all.snapshot()] + [r.snapshot() for r in self.groups.values()]}


# ---------- web: page + live websocket ----------
def tls_context():
    """HTTPS for the chat page, or None for plain HTTP. TLS 1.2 or newer."""
    if not (TLS_CERT or TLS_KEY):
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(TLS_CERT, TLS_KEY)       # fails (and stops the program) if either file is missing or wrong
    return ctx


async def main():
    tls = tls_context()
    store = Store(DB_PATH, RETENTION)
    browsers = {}                      # websocket -> its session's token hash (rechecked every minute)
    stats = store.get("stats", {})     # latest counters from cef_to_json.py, for the status bar

    def publish(op):
        msg = json.dumps(op)
        for ws in list(browsers):
            asyncio.ensure_future(ws.send_str(msg))

    view = Rooms(publish)
    monitor = Monitor(store, publish)

    # ---- history: rebuild the tabs from the database ----
    events = notices = 0
    for at, kind, body in store.chat_since(store.cutoff()):
        if kind == "event":
            view.handle(body); events += 1
        elif kind == "notice":
            view.system(body.get("text", ""), at); notices += 1
    log.info("Restored %d message(s) and %d notice(s) from the last %d days (%s), in %d group tab(s).",
             events, notices, RETENTION, DB_PATH, len(view.groups))
    users = store.users()
    if not users:
        log.warning("There are no users yet, so nobody can log in. Add one: "
                    "docker exec -it signal-playback python playback_users.py add NAME")

    # ---- login ----
    lockout = auth.Lockout()

    def session(request):
        """(user, token hash) of a valid session cookie, or (None, None)."""
        token = request.cookies.get(COOKIE)
        if not token:
            return None, None
        h = auth.token_hash(token)
        user = store.session_user(h)
        return (user, h) if user else (None, None)

    def same_origin(request):
        """Browsers send Origin on POSTs and websockets; it must be this site (no other page can drive it)."""
        origin = request.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == request.host

    @web.middleware
    async def guard(request, handler):
        if request.path != "/login":
            user, _ = session(request)
            if not user:
                if request.path == "/":
                    raise web.HTTPFound("/login")
                return web.json_response({"error": "log in first"}, status=401)
            request["user"] = user
        if request.method == "POST" and not same_origin(request):
            return web.json_response({"error": "wrong origin"}, status=403)
        resp = await handler(request)
        if not isinstance(resp, web.WebSocketResponse):
            resp.headers.update({
                "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                           "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                                           "img-src 'self' data:; frame-ancestors 'none'; form-action 'self'",
                "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",   # no-referrer would send "Origin: null"
                "Cache-Control": "no-store"})
        return resp

    async def login_page(request):
        return web.FileResponse(LOGIN)

    async def login(request):
        form = await request.post()
        name, pw = str(form.get("user", ""))[:64], str(form.get("password", ""))[:1024]
        key = (request.remote, name)
        if lockout.locked(key):
            log.warning("Refused a login as %r from %s: locked after too many wrong passwords.", name, request.remote)
            raise web.HTTPFound("/login?e=locked")
        user = store.user(name)
        if not auth.check_password(pw, user["hash"] if user else auth.DUMMY) or not user:
            locked = lockout.failed(key)
            log.warning("Wrong user name or password for %r from %s.%s", name, request.remote,
                        f" Locked for {auth.LOCK_SECONDS // 60} minutes." if locked else "")
            raise web.HTTPFound("/login?e=locked" if locked else "/login?e=1")
        lockout.ok(key)
        token, h = auth.new_token()
        store.add_session(h, name, now_ms() + auth.SESSION_HOURS * 3600_000)
        store.logged_in(name)
        log.info("%s logged in from %s.", name, request.remote)
        resp = web.HTTPFound("/")
        resp.set_cookie(COOKIE, token, max_age=auth.SESSION_HOURS * 3600, httponly=True, secure=bool(tls),
                        samesite="Strict", path="/")
        raise resp

    async def logout(request):
        _, h = session(request)
        if h:
            store.end_session(h)
        log.info("%s logged out.", request["user"])
        resp = web.HTTPFound("/login?e=out")
        resp.del_cookie(COOKIE, path="/")
        raise resp

    # ---- browsers ----
    async def page(request):
        return web.FileResponse(PAGE)

    async def ws_handler(request):
        if not same_origin(request):
            return web.json_response({"error": "wrong origin"}, status=403)
        _, h = session(request)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        browsers[ws] = h
        log.info("%s opened the page from %s (%d open). Sent it %d group tab(s) and the last %d lines of \"All\".",
                 request["user"], request.remote, len(browsers), len(view.groups), len(view.all.term.lines))
        await ws.send_str(json.dumps({**view.snapshot(), "user": request["user"], "mon": monitor.snapshot()}))
        if stats:
            await ws.send_str(json.dumps({"op": "stats", "stats": stats}))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            browsers.pop(ws, None)
            log.info("A browser closed the page (%d open).", len(browsers))
        return ws

    async def api_state(request):
        return web.json_response(monitor.state())

    async def api_events(request):
        try:
            since = int(request.query.get("since", 0))
            limit = max(1, min(int(request.query.get("limit", 200)), 1000))
        except ValueError:
            return web.json_response({"error": "since and limit are numbers (since: ms since 1970)"}, status=400)
        return web.json_response({"events": store.mon_events(since, limit, request.query.get("part"))})

    # ---- input from cef_to_json.py and monitor-receiver ----
    async def read_json(request):
        try:
            return await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("Received something from %s that is not JSON, so it was ignored.", request.remote)
            return None

    async def event_in(request):
        event = await read_json(request)
        if not isinstance(event, dict):
            return web.json_response({"ok": False, "error": "expected one JSON object"}, status=400)
        key = chat_key(event)
        if store.has_chat(key):
            log.info("Got a %s that is already shown (a second copy, e.g. via another account), so it was skipped.",
                     describe(event))
            return web.json_response({"ok": True, "duplicate": True})
        if view.handle(event):
            store.add_chat("event", event, key)
            log.info("Got a %s (via the receiver). Shown in the browser and saved.", describe(event))
        else:
            log.info("Got an event (via the receiver) that is not a chat message, so it was not shown.")
        return web.json_response({"ok": True})

    async def notice_in(request):
        body = await read_json(request)
        text = (body or {}).get("text") if isinstance(body, dict) else None
        if not text:
            return web.json_response({"ok": False, "error": "expected {\"text\": ...}"}, status=400)
        log.warning("The receiver reports: %s. Shown in the browser.", text)
        view.system(text)
        store.add_chat("notice", {"text": text})
        return web.json_response({"ok": True})

    async def stats_in(request):
        body = await read_json(request)
        if isinstance(body, dict):
            stats.clear(); stats.update(body)
            store.put("stats", stats)
            publish({"op": "stats", "stats": stats})
            monitor.touch("right/receiver", stats, side="right", comp="receiver", name="receiver")
        return web.json_response({"ok": True})

    async def monitor_in(request):
        body = await read_json(request)
        why = monitor.handle(body) if isinstance(body, dict) else "expected one JSON object"
        if why:
            log.warning("Ignored a monitoring message from %s: %s.", request.remote, why)
            return web.json_response({"ok": False, "error": why}, status=400)
        return web.json_response({"ok": True})

    # ---- every few seconds: health checks, sessions; every hour: cleanup ----
    async def housekeeping():
        last_clean = last_sessions = 0.0
        while True:
            await asyncio.sleep(3)
            try:
                monitor.tick()
                now = time.monotonic()
                if now - last_sessions >= 60:                    # logged out, expired or removed: close the page
                    last_sessions = now
                    for ws, h in list(browsers.items()):
                        if not (h and store.session_user(h)):
                            await ws.close(code=4001, message=b"session ended")
                if now - last_clean >= 3600:
                    last_clean = now
                    done = store.cleanup()
                    if any(done.values()):
                        log.info("Deleted what is older than %d days: %s.", RETENTION,
                                 ", ".join(f"{n} from {t}" for t, n in done.items() if n))
            except Exception:
                log.exception("Housekeeping failed (it carries on):")

    browser_app = web.Application(middlewares=[guard])
    browser_app.add_routes([web.get("/login", login_page), web.post("/login", login), web.post("/logout", logout),
                            web.get("/", page), web.get("/ws", ws_handler),
                            web.get("/api/monitor/state", api_state), web.get("/api/monitor/events", api_events)])
    ingest_app = web.Application(client_max_size=4 * 1024 * 1024)
    ingest_app.add_routes([web.post("/event", event_in), web.post("/notice", notice_in), web.post("/stats", stats_in),
                           web.post("/monitor", monitor_in)])
    runners = [web.AppRunner(browser_app), web.AppRunner(ingest_app)]
    for runner in runners:
        await runner.setup()
    await web.TCPSite(runners[0], *WEB, ssl_context=tls).start()
    await web.TCPSite(runners[1], *INGEST).start()
    if tls:
        log.info("Chat page is at https://%s:%d/ (encrypted, certificate %s).", WEB[0], WEB[1], TLS_CERT)
    else:
        log.info("Chat page is at http://%s:%d/ (NOT encrypted).", WEB[0], WEB[1])
    log.info("Waiting for messages from the receiver on http://%s:%d/event", INGEST[0], INGEST[1])
    task = asyncio.create_task(housekeeping())
    try:
        await asyncio.Event().wait()           # serve until stopped
    finally:
        log.info("Stopping.")
        task.cancel()
        for runner in runners:
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
