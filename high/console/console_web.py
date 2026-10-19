#!/usr/bin/env python3
"""Show the Signal chat in a browser. Display only: events arrive as JSON from cef_to_json.py (signal-receiver).

Dev: the final destination. Prod: runs next to the Palantir Gotham feed (my_forward's records), for backend checks
and troubleshooting. It gets every rebuilt event, plus lost-message notices.
One tab per group (by group ID, created on its first message), plus "All": every group in one stream, each line tagged
with its group, and the lost-message notices (a lost message's group is unknown). Each tab keeps its own HISTORY.
Also the Monitor tab (monitor.py): the state of every part of the relay, from monitor-receiver's messages.
History (chat, monitoring, users, sessions) is kept in DB_PATH (SQLite, store.py) for RETENTION_DAYS and restored
at start. The page needs a login (local users: console_users.py).
Two ports:
  WEB_PORT    (8090)  "/login", chat page "/" (console_web.html), live updates "/ws", and the read-only
                      /api/monitor/state and /api/monitor/events (JSON, for tools). HTTPS when WEB_TLS_CERT and
                      WEB_TLS_KEY are set (PEM files); it then refuses to start without them.
  INGEST_PORT (8091)  POST /event (one Signal event), /notice ({"text"}), /stats (a receiver's counters),
                      /monitor (from monitor-receiver); keep this port private: anything posted here is shown
    python console_web.py        # then:  CONSOLE_URL=http://127.0.0.1:8091 python cef_to_json.py
"""
import asyncio, json, logging, os, re, signal, socket, ssl, time, zlib
from urllib.parse import urlsplit
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from aiohttp import web, WSMsgType, ClientSession, ClientTimeout, ClientError
from chat_render import Chat, describe
import auth
from monitor import Monitor, NAME
from store import Store, chat_key, now_ms

WEB       = (os.environ.get("WEB_HOST", "127.0.0.1"), int(os.environ.get("WEB_PORT", 8090)))       # browsers
INGEST    = (os.environ.get("INGEST_HOST", "127.0.0.1"), int(os.environ.get("INGEST_PORT", 8091)))  # cef_to_json.py
TLS_CERT  = os.environ.get("WEB_TLS_CERT")   # e.g. /certs/ssl.lab.int.crt (with its CA chain, if any)
TLS_KEY   = os.environ.get("WEB_TLS_KEY")    # e.g. /certs/ssl.lab.int.key
# Behind a reverse proxy that ends TLS (k8s ingress-nginx): take the browser's address and scheme from the proxy's
# headers (X-Real-IP / X-Forwarded-For, X-Forwarded-Proto), so the login lockout and the logs see the real address and
# the session cookie stays Secure. Only with the page port reachable through the proxy alone: the headers can be faked.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "") in ("1", "true", "yes")
PAGE      = Path(__file__).with_name("console_web.html")
LOGIN     = Path(__file__).with_name("console_login.html")
RUNBOOK   = Path(__file__).with_name("runbook.json")              # likely causes: what they mean, what to check
ASSIST_URL = os.environ.get("ASSIST_URL", "").rstrip("/")         # relay-assist (AI explanations); empty = off
ASSIST_WAIT = int(os.environ.get("ASSIST_WAIT", 360))              # s; longer than relay-assist's AI_TOTAL_S (300)


def host_name():
    """This host's name for the Monitor tab: HOST_NAME, else the Docker host's own (/etc/hostname at /host/hostname)."""
    try:
        return os.environ.get("HOST_NAME") or open("/host/hostname").read().strip()
    except OSError:
        return ""


HOST_NAME = host_name()
AUTO_PER_HOUR = 20                 # automatic AI questions at most (free tiers, a shared prod model)
WRAP_ALWAYS = {"EVENTS_MISSED", "CLI_RESTART_LOSS", "CHAT_NOT_ARRIVING", "MESSAGE_LOST", "SEND_FAILED",   # messages may be lost:
               "FORWARD_FAILED", "DELIVER_FAILED", "DEVICE_UNLINKED", "CLIENT_EXPIRED"}       # always worth a wrap-up
ASK_TURNS = 4                      # a question carries at most this many earlier questions and answers
PROMPT_HOLD_MS = 24 * 3600_000     # AI_LOG=summary: full prompts are dropped after this, unless kept (see store.py)
DB_PATH   = os.environ.get("DB_PATH", "relay.db")             # SQLite: history, monitoring, users (message content)
RETENTION = int(os.environ.get("RETENTION_DAYS", 30))         # days; anything older is deleted every hour
COOKIE    = "relay_session"
HISTORY   = 500                    # chat lines kept per tab, for browsers that connect later
MAX_ROOMS = int(os.environ.get("MAX_ROOMS", 50))   # group tabs at most; more groups still show in "All"
# on: message text is shown and kept (relay.db); off: never stored or shown, only counted per group (delivery figures)
CHAT_VIEW = os.environ.get("CHAT_VIEW", "on").strip().lower() != "off"
# full: whole messages in the group windows; short: only the first SHORT_WORDS words of each (the page never gets more)
CHAT_TEXT = "short" if os.environ.get("CHAT_TEXT", "full").strip().lower() == "short" else "full"
SHORT_WORDS = 5
ALL       = "all"                  # the "All" tab's id (group IDs are base64, so they never clash)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
log = logging.getLogger("console")


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
        self.chat = Chat(show_group=show_group,   # "All" tags each line with its group; a group's window doesn't
                         words=SHORT_WORDS if CHAT_TEXT == "short" else None)
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
                     "text": self.chat.cut((target or {}).get("text") or q.get("text") or "")}
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

    def count(code):
        """One more of the console's own problems (shown on its tile, see monitor.CONSOLE_COUNTED)."""
        monitor.health["p"][code] = monitor.health["p"].get(code, 0) + 1
    browsers = {}                      # websocket -> its session's token hash (rechecked every minute)
    rx_stats = store.get("rx_stats", {})   # receiver name -> its latest counters (cef_to_json.py), for the status bar
    rx_names = {}                          # receiver address -> (its name, when looked up)

    def stats_total():
        """The status bar's figures: every receiver on the Monitor tab, added up."""
        live = [v for n, v in rx_stats.items() if f"high/receiver/{n}" in monitor.parts]
        total = {k: sum(v[k] for v in live if isinstance(v.get(k), int))
                 for k in ("messages", "packets", "lost", "missing_packets", "bad", "duplicates")}
        return {**total, "receivers": len(live)}

    async def rx_name(request, body):
        """Which receiver sent its counters: RECEIVER_NAME if it set one, else its container name from Docker's DNS
        (the address looked up backwards, as signal-monitor does for subscribers)."""
        given = body.get("name")
        if isinstance(given, str) and NAME.match(given):
            return given
        ip, now = request.remote or "", time.time()
        hit = rx_names.get(ip)
        if hit and now - hit[1] < 60:
            return hit[0]
        try:
            host, _ = await asyncio.get_running_loop().getnameinfo((ip, 0), socket.NI_NAMEREQD)
            name = host.split(".")[0]
        except OSError:
            name = ""
        if not NAME.match(name):
            name = "receiver-" + re.sub(r"[^0-9A-Za-z]", "-", ip)
            if not hit:
                log.warning("Could not look up the name of the receiver at %s, so it shows as %s. Set RECEIVER_NAME "
                            "on it to name it.", ip, name)
        rx_names[ip] = (name, now)
        return name

    def publish(op):
        msg = json.dumps(op)
        for ws in list(browsers):
            # a page that is closing can't take it: nothing to do (its websocket handler cleans up)
            asyncio.ensure_future(ws.send_str(msg)).add_done_callback(lambda f: f.cancelled() or f.exception())

    view = Rooms(publish)
    try:
        runbook = {k: v for k, v in json.loads(RUNBOOK.read_text()).items() if not k.startswith("_")}
    except (OSError, ValueError) as e:
        log.error("Could not read the runbook %s (%s): incidents will show cause codes without explanations.", RUNBOOK, e)
        runbook = {}
    monitor = Monitor(store, publish, runbook)
    monitor.disk_path = os.path.dirname(os.path.abspath(DB_PATH))

    # ---- history: rebuild the tabs from the database ----
    events = notices = 0
    for at, kind, body in (store.chat_since(store.cutoff()) if CHAT_VIEW else []):
        if kind == "event":
            view.handle(body); events += 1
            monitor.arrived("", *group_of(body), "")      # group names only (for the group windows), no counts
        elif kind == "notice":
            view.system(body.get("text", ""), at); notices += 1
    log.info("Restored %d message(s) and %d notice(s) from the last %d days (%s), in %d group tab(s).",
             events, notices, RETENTION, DB_PATH, len(view.groups))
    users = store.users()
    if not users:
        log.warning("There are no users yet, so nobody can log in. Add one: "
                    "docker exec -it signal-console python console_users.py add NAME")

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

    def token_user(request):
        """A tool's API token (Authorization: Bearer ...): read-only access to /api/monitor/ only."""
        h = request.headers.get("Authorization", "")
        if not h.startswith("Bearer ") or request.method != "GET" or not request.path.startswith("/api/monitor/"):
            return None
        name = store.token_name(auth.token_hash(h[7:].strip()))
        return f"token:{name}" if name else None

    def via_proxy(request):
        """The request as the browser made it (TRUST_PROXY): its address and http/https, from the proxy's headers."""
        fwd = request.headers.get("X-Forwarded-For", "")
        remote = request.headers.get("X-Real-IP") or (fwd.split(",")[-1].strip() if fwd else None) or request.remote
        scheme = request.headers.get("X-Forwarded-Proto", request.scheme).split(",")[0].strip().lower()
        return request.clone(remote=remote[:64], scheme=scheme if scheme in ("http", "https") else request.scheme)

    @web.middleware
    async def guard(request, handler):
        if TRUST_PROXY:
            request = via_proxy(request)
        if request.path != "/login":
            user = token_user(request)
            if not user and request.headers.get("Authorization"):
                log.warning("Refused an API request from %s: unknown token, or not allowed for tokens (%s %s).",
                            request.remote, request.method, request.path)
                return web.json_response({"error": "unknown token, or not allowed with a token"}, status=401)
            if not user:
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
        resp.set_cookie(COOKIE, token, max_age=auth.SESSION_HOURS * 3600, httponly=True, secure=bool(tls) or request.scheme == "https",
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
        await ws.send_str(json.dumps({**view.snapshot(), "user": request["user"], "mon": monitor.snapshot(),
                                      "assist": bool(ASSIST_URL), "host": HOST_NAME, "chat_view": CHAT_VIEW,
                                      "chat_text": CHAT_TEXT, "short_words": SHORT_WORDS}))
        if rx_stats:
            await ws.send_str(json.dumps({"op": "stats", "stats": stats_total()}))
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
            before = int(request.query["before"]) if "before" in request.query else None
        except ValueError:
            return web.json_response({"error": "since, limit and before are numbers (since: ms since 1970; before: an event id)"}, status=400)
        return web.json_response({"events": store.mon_events(since, limit, request.query.get("part"), before)})

    TL_BUCKETS = 144                                     # the timeline's columns (24 h: 10 minutes each)
    TL_ORDER = ["up", "warn", "stale", "unknown", "down"]

    async def api_timeline(request):
        """Each part's state over the last hours (default 24, kept 48), in TL_BUCKETS columns: the worst state
        seen in each, or null where nothing was recorded (the console itself was down)."""
        try:
            hours = num(request, "hours", 24, 1, 48)
        except ValueError:
            return web.json_response({"error": "hours is a number (1-48)"}, status=400)
        end = now_ms(); start = end - hours * 3600_000; step = (end - start) / TL_BUCKETS
        lanes = {}
        for minute, key, st in store.metrics_all(start):
            i = min(TL_BUCKETS - 1, int((minute - start) // step))
            row = lanes.setdefault(key, [None] * TL_BUCKETS)
            if st in TL_ORDER and (row[i] is None or TL_ORDER.index(st) > TL_ORDER.index(row[i])):
                row[i] = st
        return web.json_response({"start": start, "end": end, "step_ms": int(step), "lanes": lanes})

    async def api_incidents(request):
        try:
            since = int(request.query.get("since", 0))
            limit = max(1, min(int(request.query.get("limit", 50)), 500))
        except ValueError:
            return web.json_response({"error": "since and limit are numbers (since: ms since 1970)"}, status=400)
        open_only = request.query.get("state") == "open"
        found = [{k: v for k, v in i.items() if k != "_saved"} for i in store.incidents(open_only, since, limit)]
        return web.json_response({"incidents": found})

    async def api_incident(request):
        try:
            inc = monitor.incidents.detail(int(request.match_info["id"]), lambda since, n: store.mon_events(since, n))
        except ValueError:
            inc = None
        return web.json_response(inc) if inc else web.json_response({"error": "no such incident"}, status=404)

    async def api_runbook(request):
        return web.json_response(runbook)

    def num(request, name, default, lo, hi):
        return max(lo, min(int(request.query.get(name, default)), hi))

    async def api_history(request):
        """Past incidents by cause and/or part (how often, when, how long): for the AI's get_history."""
        try:
            days = num(request, "days", 30, 1, RETENTION)
        except ValueError:
            return web.json_response({"error": "days is a number"}, status=400)
        return web.json_response(monitor.incidents.history(request.query.get("code") or None,
                                                           request.query.get("part") or None, days))

    async def api_metrics(request):
        """One part's state and figures, once a minute (kept 48 h): the run-up to a problem."""
        try:
            minutes = num(request, "minutes", 60, 1, 48 * 60)
        except ValueError:
            return web.json_response({"error": "minutes is a number"}, status=400)
        part = request.query.get("part", "")
        rows = store.metrics(part, now_ms() - minutes * 60_000)
        step = max(1, len(rows) // 120)                  # at most ~120 points
        return web.json_response({"part": part, "every_minutes": step, "rows": rows[::step]})

    async def api_chat_activity(request):
        """Messages per group per hour (counts and group names only, never content): how busy each group
        normally is, so an outage's impact can be estimated."""
        try:
            hours = num(request, "hours", 24, 1, 168)
        except ValueError:
            return web.json_response({"error": "hours is a number"}, status=400)
        now = now_ms()
        groups = {}
        for at, kind, body in store.chat_since(now - 7 * 86400_000):
            if kind != "event":
                continue
            env = body.get("envelope") or {}
            dm = ((env.get("editMessage") or {}).get("dataMessage") or env.get("dataMessage")
                  or (env.get("syncMessage") or {}).get("sentMessage") or {})
            gi = dm.get("groupInfo") or {}
            if not gi.get("groupId"):
                continue
            g = groups.setdefault(gi["groupId"], {"name": gi.get("groupName") or gi["groupId"][:8], "week": 0,
                                                  "last": 0, "per_hour": [0] * hours})
            g["week"] += 1
            g["last"] = max(g["last"], at)
            ago = int((now - at) // 3600_000)
            if ago < hours:
                g["per_hour"][hours - 1 - ago] += 1
        out = [{"group": g["name"], "last_message_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(g["last"] / 1000)),
                "per_hour_oldest_first": g["per_hour"], "avg_per_hour_7d": round(g["week"] / 168, 2)}
               for g in sorted(groups.values(), key=lambda g: -g["week"])]
        return web.json_response({"hours": hours, "now_utc": time.strftime("%Y-%m-%d %H:%M", time.gmtime(now / 1000)),
                                  "groups": out})

    # ---- AI assistant (relay-assist): explain an incident, rate an answer ----
    asking = {}                        # incident id -> task: a second click waits for the same answer

    async def ask_assist(incident, user, path="/explain", extra=None):
        # every open page shows a waiting animation until the answer (or error) is in, whoever asked
        wait = {"op": "ai_wait", "incident": incident, "kind": {"/ask": "ask", "/wrapup": "wrapup"}.get(path, "explain")}
        publish(wait)
        try:
            return await _ask_assist(incident, user, path, extra)
        finally:
            publish({**wait, "op": "ai_done"})

    async def _ask_assist(incident, user, path, extra):
        try:
            async with ClientSession(timeout=ClientTimeout(total=ASSIST_WAIT)) as http:
                async with http.post(f"{ASSIST_URL}{path}", json={"incident": incident, "user": user, **(extra or {})}) as r:
                    entry = await r.json()
                    if r.status != 200:
                        count("AI_FAILED")
                        return {"error": entry.get("error") or f"relay-assist answered HTTP {r.status}"}
        except (ClientError, asyncio.TimeoutError, ValueError) as e:
            count("AI_FAILED")
            return {"error": f"could not reach relay-assist ({str(e) or type(e).__name__})"}
        if entry.get("error"):
            count("AI_FAILED")
        level = entry.get("log", "summary")
        if level == "off":
            entry.pop("prompt", None)
            return {**entry, "id": None, "detail": "none"}
        keep = level == "full" or bool(entry.get("error")) or entry.get("verdict") == "disagree"
        entry["id"] = store.add_ai(entry, entry.get("prompt"), None if level == "full" else now_ms() + PROMPT_HOLD_MS, keep)
        entry.pop("prompt", None)
        entry["detail"], entry["kept"] = "full", keep
        log.info("Saved AI %s #%d%s (%s).", {"ask": "answer to a question", "wrapup": "wrap-up"}.get(entry.get("kind"), "answer"),
                 entry["id"], f" for incident #{incident}" if incident else "", "full prompt kept" if keep else "full prompt held 24 h")
        publish({"op": "ai", "entry": entry})
        return entry

    auto_asked = deque(maxlen=50)      # times of automatic questions: at most AUTO_PER_HOUR

    def ask(incident, user):
        task = asking.get(incident)
        if not task:
            task = asking[incident] = asyncio.ensure_future(ask_assist(incident, user))
            task.add_done_callback(lambda _: asking.pop(incident, None))
        return task

    wrapping = set()                   # incidents whose wrap-up is being written

    def wrap(incident, user):
        if incident in wrapping:
            return None
        wrapping.add(incident)
        task = asyncio.ensure_future(ask_assist(incident, user, "/wrapup"))
        task.add_done_callback(lambda _: wrapping.discard(incident))
        return task

    def auto_wrap(now):
        """When an incident closes: its wrap-up, by itself, if it is worth one (the AI was asked while it was open,
        it lasted 2 min or more, or messages may have been lost), within the same hourly limit."""
        for inc in store.incidents(since_ms=now - 1800_000, limit=20):
            if not inc.get("closed") or inc.get("wrapped") or inc["id"] in wrapping or now - inc["closed"] > 1800_000:
                continue
            worth = inc.get("asked") or inc["closed"] - inc["opened"] >= 120_000 or inc["cause"]["code"] in WRAP_ALWAYS
            if not worth:
                continue
            if len([t for t in auto_asked if now - t < 3600_000]) >= AUTO_PER_HOUR:
                return
            auto_asked.append(now)
            inc["wrapped"] = True
            store.put_incident(inc)
            log.info("Writing the wrap-up of incident #%d by itself (it closed).", inc["id"])
            wrap(inc["id"], "auto")

    def auto_ask():
        """Ask the AI by itself when an incident qualifies (incidents.wants_ai), so the answer is ready when
        someone opens it; and write the wrap-up when one closes."""
        if not ASSIST_URL:
            return
        now = now_ms()
        auto_wrap(now)
        for inc in list(monitor.incidents.open.values()):
            code = monitor.incidents.wants_ai(inc, monitor.parts, now)
            if not code or inc["id"] in asking:
                continue
            if len([t for t in auto_asked if now - t < 3600_000]) >= AUTO_PER_HOUR:
                return
            auto_asked.append(now)
            monitor.incidents.mark_asked(inc["id"], code)
            log.info("Asking the AI about incident #%d by itself (likely cause %s).", inc["id"], code)
            ask(inc["id"], "auto")

    async def assist_explain(request):
        if not ASSIST_URL:
            return web.json_response({"error": "the Analyst is not set up (ASSIST_URL)"}, status=503)
        try:
            incident = int((await request.json())["incident"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return web.json_response({"error": "expected {\"incident\": id}"}, status=400)
        if not monitor.incidents.detail(incident, lambda since, n: []):
            return web.json_response({"error": "no such incident"}, status=404)
        task = ask(incident, request["user"])
        return web.json_response(await asyncio.shield(task))     # finishes and is saved even if the browser leaves

    async def assist_ask(request):
        """A free question, about one incident or (incident null) the relay as a whole."""
        if not ASSIST_URL:
            return web.json_response({"error": "the Analyst is not set up (ASSIST_URL)"}, status=503)
        try:
            body = await request.json()
            question = str(body["question"]).strip()[:500]
            incident = int(body["incident"]) if body.get("incident") is not None else None
            prev = [int(i) for i in (body.get("prev") or [])][-ASK_TURNS:]
            if not question:
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return web.json_response({"error": "expected {\"question\": text, \"incident\": id or null, "
                                               "\"prev\": [earlier answer ids]}"}, status=400)
        if incident is not None and not monitor.incidents.detail(incident, lambda since, n: []):
            return web.json_response({"error": "no such incident"}, status=404)
        # multi-turn: the earlier questions shown in the same box, read back from the log (never taken from the browser)
        history = [{"id": e["id"], "q": e.get("question", ""), "reply": e.get("reply") or {"answer": e.get("answer", "")}}
                   for e in store.ai_asks(prev, incident) if not e.get("error")]
        task = asyncio.ensure_future(ask_assist(incident, request["user"], "/ask", {"question": question, "history": history}))
        return web.json_response(await asyncio.shield(task))

    async def assist_wrapup(request):
        """Write (or write again) a closed incident's wrap-up."""
        if not ASSIST_URL:
            return web.json_response({"error": "the Analyst is not set up (ASSIST_URL)"}, status=503)
        try:
            incident = int((await request.json())["incident"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return web.json_response({"error": "expected {\"incident\": id}"}, status=400)
        inc = store.incident(incident)
        if not inc or not inc.get("closed"):
            return web.json_response({"error": "no such closed incident"}, status=404)
        task = wrap(incident, request["user"])
        if task is None:
            return web.json_response({"error": "its wrap-up is being written already"}, status=409)
        return web.json_response(await asyncio.shield(task))

    async def assist_clear(request):
        """Clear in an Ask box: delete its questions and answers, for everyone. {"incident": id} = that incident's
        Ask box (its explain cards and wrap-ups stay); null = Ask Analyst (the relay as a whole)."""
        try:
            body = await request.json()
            incident = int(body["incident"]) if body.get("incident") is not None else None
        except (ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
            return web.json_response({"error": "expected {\"incident\": id or null}"}, status=400)
        n = store.clear_asks(incident)
        log.info("%s cleared %s: deleted %d question(s) and answer(s).", request["user"],
                 f"the questions of incident #{incident}" if incident is not None else "Ask Analyst", n)
        publish({"op": "ai_clear", "incident": incident})
        return web.json_response({"ok": True, "deleted": n})

    async def monitor_forget(request):
        """Remove this box: {"key": part key}. Only for a box not heard from for over an hour (monitor.removable);
        its history stays, and it comes back by itself if it reports again."""
        try:
            key = (await request.json())["key"]
            if not isinstance(key, str) or len(key) > 200:
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return web.json_response({"error": "expected {\"key\": part key}"}, status=400)
        why = monitor.remove_by_hand(key, request["user"])
        if why:
            return web.json_response({"error": why}, status=409)
        for name in [n for n in rx_stats if f"high/receiver/{n}" not in monitor.parts]:
            del rx_stats[name]                             # a removed receiver leaves the status bar's total too
        store.put("rx_stats", rx_stats)
        publish({"op": "stats", "stats": stats_total()})
        publish({"op": "mon", **monitor.state()})
        return web.json_response({"ok": True})

    async def assist_rate(request):
        try:
            body = await request.json()
            id_, rating = int(body["id"]), body["rating"]
            if rating not in ("helpful", "not_helpful"):
                raise ValueError
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return web.json_response({"error": "expected {\"id\": n, \"rating\": \"helpful\"|\"not_helpful\"}"}, status=400)
        if not store.rate_ai(id_, rating):
            return web.json_response({"error": "no such answer"}, status=404)
        log.info("%s rated AI answer #%d %s.", request["user"], id_, rating.replace("_", " "))
        return web.json_response({"ok": True})

    async def assist_log(request):
        try:
            incident = int(request.query["incident"]) if "incident" in request.query else None
            limit = max(1, min(int(request.query.get("limit", 20)), 200))
        except ValueError:
            return web.json_response({"error": "incident and limit are numbers"}, status=400)
        return web.json_response({"answers": store.ai_entries(incident, limit, general=request.query.get("general") == "1")})

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
        gid, gname = group_of(event)
        session, room = (request.headers.get(h, "")[:32] for h in ("X-Relay-Session", "X-Relay-Room"))
        monitor.arrived(session, gid, gname, room)        # counted per route before the duplicate check
        if not CHAT_VIEW:
            log.info("Got a %s via %s (counted; CHAT_VIEW is off, so it was not shown or kept).", describe(event), room or "?")
            return web.json_response({"ok": True})
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
        # "a message for SELFTEST was lost": the room by its real name (its Signal groups), as everywhere on the page
        text = re.sub(r"\bfor ([A-Z0-9_-]{1,32}) was lost", lambda m: f"for {monitor.room_title(m.group(1))} was lost", text)
        log.warning("The receiver reports: %s. Shown in the browser.", text)
        view.system(text)
        store.add_chat("notice", {"text": text})
        return web.json_response({"ok": True})

    async def stats_in(request):
        body = await read_json(request)
        if isinstance(body, dict):
            name = await rx_name(request, body)
            rx_stats[name] = body
            store.put("rx_stats", rx_stats)
            monitor.receiver(body, name)
            publish({"op": "stats", "stats": stats_total()})
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
            t0 = time.monotonic()
            await asyncio.sleep(3)
            lag = time.monotonic() - t0 - 3                      # how long the event loop was blocked (slow disk?)
            if lag > 2:
                monitor.health["lag"], monitor.health["lag_at"] = lag, time.time()
                log.warning("The console was blocked for %.1f s: pages and intake had to wait.", lag)
            monitor.health["p"]["DB_ERROR"] = store.db.errors
            try:
                monitor.tick()
                auto_ask()
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
                            web.get("/api/monitor/state", api_state), web.get("/api/monitor/events", api_events),
                            web.get("/api/monitor/incidents", api_incidents),
                            web.get("/api/monitor/incidents/{id}", api_incident),
                            web.get("/api/monitor/runbook", api_runbook),
                            web.get("/api/monitor/history", api_history), web.get("/api/monitor/metrics", api_metrics),
                            web.get("/api/monitor/chat-activity", api_chat_activity), web.get("/api/monitor/timeline", api_timeline),
                            web.post("/api/assist/explain", assist_explain), web.post("/api/assist/rate", assist_rate),
                            web.post("/api/assist/ask", assist_ask), web.post("/api/assist/wrapup", assist_wrapup),
                            web.get("/api/assist/log", assist_log),
                            web.post("/api/assist/clear", assist_clear), web.post("/api/monitor/forget", monitor_forget)])
    @web.middleware
    async def ingest_guard(request, handler):
        """Port 8091 (this project's network only): POSTs from the receivers as before; the read-only monitoring API
        for relay-assist, with a token."""
        if request.method == "GET":
            user = token_user(request)
            if not user:
                log.warning("Refused a read on the internal port from %s: no valid token (%s).", request.remote, request.path)
                return web.json_response({"error": "a valid token is needed"}, status=401)
            request["user"] = user
        try:
            resp = await handler(request)
        except web.HTTPException:
            raise
        except Exception:
            count("INGEST_FAILED")
            log.exception("Could not take in %s %s from %s:", request.method, request.path, request.remote)
            raise
        if resp.status >= 500:
            count("INGEST_FAILED")
        return resp

    ingest_app = web.Application(client_max_size=4 * 1024 * 1024, middlewares=[ingest_guard])
    ingest_app.add_routes([web.post("/event", event_in), web.post("/notice", notice_in), web.post("/stats", stats_in),
                           web.post("/monitor", monitor_in),
                           web.get("/api/monitor/state", api_state), web.get("/api/monitor/events", api_events),
                           web.get("/api/monitor/incidents", api_incidents),
                           web.get("/api/monitor/incidents/{id}", api_incident),
                           web.get("/api/monitor/runbook", api_runbook),
                           web.get("/api/monitor/history", api_history), web.get("/api/monitor/metrics", api_metrics),
                           web.get("/api/monitor/chat-activity", api_chat_activity)])
    runners = [web.AppRunner(browser_app, shutdown_timeout=5), web.AppRunner(ingest_app, shutdown_timeout=5)]
    for runner in runners:
        await runner.setup()
    await web.TCPSite(runners[0], *WEB, ssl_context=tls).start()
    await web.TCPSite(runners[1], *INGEST).start()
    if tls:
        log.info("Chat page is at https://%s:%d/ (encrypted, certificate %s).", WEB[0], WEB[1], TLS_CERT)
    elif TRUST_PROXY:
        log.info("Chat page is at http://%s:%d/, behind a proxy that ends TLS (TRUST_PROXY).", WEB[0], WEB[1])
    else:
        log.info("Chat page is at http://%s:%d/ (NOT encrypted).", WEB[0], WEB[1])
    log.info("Waiting for messages from the receiver on http://%s:%d/event", INGEST[0], INGEST[1])
    task = asyncio.create_task(housekeeping())
    try:
        stop = asyncio.Event()                 # serve until stopped: SIGTERM (docker stop, k8s) or Ctrl-C
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        await stop.wait()
    finally:
        log.info("Stopping.")
        task.cancel()
        for ws in list(browsers):              # open pages reconnect by themselves; don't wait for them
            await ws.close(code=1001, message=b"the console is restarting")
        for runner in runners:
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
