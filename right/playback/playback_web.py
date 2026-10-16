#!/usr/bin/env python3
"""Show the Signal chat in a browser. Display only: events arrive as JSON from cef_to_json.py (signal-receiver).

Stand-in for Palantir: it receives exactly what Palantir will receive, plus lost-message notices.
Two ports:
  WEB_PORT    (8090)  chat page "/" (playback_web.html) and live updates "/ws" for browsers. HTTPS when
                      WEB_TLS_CERT and WEB_TLS_KEY are set (PEM files); it then refuses to start without them.
  INGEST_PORT (8091)  POST /event (one Signal event), /notice ({"text"}), /stats (receiver counters);
                      keep this port private: anything posted here appears in the chat
    python playback_web.py        # then:  PLAYBACK_URL=http://127.0.0.1:8091 python cef_to_json.py
"""
import asyncio, json, logging, os, re, ssl, time, zlib
from collections import OrderedDict, deque
from datetime import datetime, timezone
from pathlib import Path
from aiohttp import web, WSMsgType
from chat_render import Chat, describe

WEB       = (os.environ.get("WEB_HOST", "127.0.0.1"), int(os.environ.get("WEB_PORT", 8090)))       # browsers
INGEST    = (os.environ.get("INGEST_HOST", "127.0.0.1"), int(os.environ.get("INGEST_PORT", 8091)))  # cef_to_json.py
TLS_CERT  = os.environ.get("WEB_TLS_CERT")   # e.g. /certs/ssl.lab.int.crt (with its CA chain, if any)
TLS_KEY   = os.environ.get("WEB_TLS_KEY")    # e.g. /certs/ssl.lab.int.key
PAGE      = Path(__file__).with_name("playback_web.html")
HISTORY   = 500                    # chat lines kept for browsers that connect later

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
    def __init__(self, publish):
        self.publish = publish
        self.chat = Chat(show_group=False)    # group name is in the page title instead
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
    """Feeds every event to both views; the browser shows whichever one the viewer picks."""
    def __init__(self, publish):
        self.term = TermView(publish)
        self.bubbles = BubbleView(publish, self.term.chat)   # term.handle runs first, so names are learned

    def system(self, text):
        self.term.system(text)
        self.bubbles.system(text[:1].upper() + text[1:] + ".")

    def snapshot(self):
        return {"op": "snapshot", "lines": list(self.term.lines),
                "items": [BubbleView._public(i) for i in self.bubbles.items.values()]}

    def handle(self, event):
        """Render one Signal event. Returns a plain-English description for the log, or None if not shown."""
        shown = self.term.handle(event)
        self.bubbles.handle(event)
        return describe(event) if shown else None


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
    browsers = set()
    stats = {}                         # latest counters from cef_to_json.py, for the status bar

    def publish(op):
        msg = json.dumps(op)
        for ws in list(browsers):
            asyncio.ensure_future(ws.send_str(msg))

    view = ChatView(publish)

    # ---- browsers ----
    async def page(request):
        return web.FileResponse(PAGE)

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        browsers.add(ws)
        log.info("A browser connected from %s (%d open). Sent it the last %d chat lines.",
                 request.remote, len(browsers), len(view.term.lines))
        await ws.send_str(json.dumps(view.snapshot()))
        if stats:
            await ws.send_str(json.dumps({"op": "stats", "stats": stats}))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            browsers.discard(ws)
            log.info("A browser disconnected (%d open).", len(browsers))
        return ws

    # ---- input from cef_to_json.py ----
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
        if view.handle(event):
            log.info("Got a %s (via the receiver). Shown in the browser.", describe(event))
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
        return web.json_response({"ok": True})

    async def stats_in(request):
        body = await read_json(request)
        if isinstance(body, dict):
            stats.clear(); stats.update(body)
            publish({"op": "stats", "stats": stats})
        return web.json_response({"ok": True})

    browser_app = web.Application()
    browser_app.add_routes([web.get("/", page), web.get("/ws", ws_handler)])
    ingest_app = web.Application(client_max_size=4 * 1024 * 1024)
    ingest_app.add_routes([web.post("/event", event_in), web.post("/notice", notice_in), web.post("/stats", stats_in)])
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
    try:
        await asyncio.Event().wait()           # serve until stopped
    finally:
        log.info("Stopping.")
        for runner in runners:
            await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
