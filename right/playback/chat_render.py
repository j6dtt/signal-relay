#!/usr/bin/env python3
"""Render Signal events as terminal chat lines, e.g. "[02:40:01] Tho Pham: ↪ Alice "Hello" Hi Alice".

Used by playback_web.py for its terminal-style web view. Chat remembers recent messages so reactions,
edits, deletes and quotes can show the text they refer to. Colours are ANSI codes when color=True.
"""
import sys, zlib
from datetime import datetime, timezone

COLORS = [31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96]
DIM, RESET = "\033[2m", "\033[0m"


def describe(event):
    """Short plain-English description of a Signal event, e.g. 'reaction from Tho Pham'."""
    env = event.get("envelope", {})
    who = env.get("sourceName") or env.get("sourceNumber") or "unknown sender"
    edit = env.get("editMessage")
    dm = ((edit or {}).get("dataMessage") or env.get("dataMessage")
          or (env.get("syncMessage") or {}).get("sentMessage") or {})
    if edit:                                   kind = "edited message"
    elif dm.get("remoteDelete"):               kind = "deleted message"
    elif dm.get("reaction"):                   kind = "reaction"
    elif dm.get("sticker"):                    kind = "sticker"
    elif dm.get("attachments"):                kind = f"{len(dm['attachments'])} attachment(s)"
    elif dm.get("quote"):                      kind = "reply"
    elif (dm.get("groupInfo") or {}).get("type") in ("UPDATE", "QUIT"): kind = "group change"
    else:                                      kind = "text message"
    return f"{kind} from {who}"


class Chat:
    def __init__(self, group=None, color=True, show_group=False, group_names=None):
        self.group = group
        self.color = color and sys.stdout.isatty()
        self.show_group = show_group
        self.group_names = group_names or {}
        self.msgs = {}  # (author_uuid, timestamp) -> {"name", "text"}
        self.names = {}  # uuid / number -> display name, learned from senders (mentions only carry ids)

    # ---------- helpers ----------
    def c(self, code, s):
        return f"\033[{code}m{s}{RESET}" if self.color else s

    def dim(self, s):
        return f"{DIM}{s}{RESET}" if self.color else s

    def who(self, name, uuid):
        name = name or "unknown"
        return self.c(f"1;{COLORS[zlib.crc32((uuid or name).encode()) % len(COLORS)]}", name)

    @staticmethod
    def snip(text, n=40):
        text = (text or "").replace("\n", " ")
        return text if len(text) <= n else text[: n - 1] + "…"

    def ref(self, uuid, ts):
        m = self.msgs.get((uuid, ts))
        if not m:
            return self.dim("(earlier message)")
        return f'{m["name"]} {self.dim(chr(34) + self.snip(m["text"]) + chr(34))}'

    def quote_author(self, q):
        """Quoted author's name: authorName from subscriber_udp.py, else learned from senders, else the number."""
        return (q.get("authorName") or self.names.get(q.get("authorUuid"))
                or self.names.get(q.get("authorNumber")) or q.get("authorNumber") or "someone")

    def quote_ref(self, q):
        """Who and what a reply quotes; uses the quote's own text if the original was not seen here."""
        m = self.msgs.get((q.get("authorUuid"), q.get("id")))
        if m:
            return self.ref(q.get("authorUuid"), q.get("id"))
        name, text = self.quote_author(q), q.get("text")
        if not name and not text:
            return self.dim("(earlier message)")
        return f'{name or "someone"} {self.dim(chr(34) + self.snip(text) + chr(34)) if text else self.dim("(attachment)")}'

    def learn(self, env):
        """Remember the sender's display name, so mentions of them can show it."""
        name = env.get("sourceName")
        if name:
            for key in (env.get("sourceUuid"), env.get("sourceNumber")):
                if key:
                    self.names[key] = name

    def mention_name(self, m):
        # subscriber_udp.py puts the display name in "name"; plain signal-cli puts the number or uuid there
        given = m.get("name")
        if given and given not in (m.get("uuid"), m.get("number")):
            return given
        return (self.names.get(m.get("uuid")) or self.names.get(m.get("number"))   # learned from senders
                or m.get("number") or "someone")

    def with_mentions(self, text, mentions):
        if not text or not mentions:
            return text
        chars = list(text)
        for m in sorted(mentions, key=lambda m: m.get("start", 0), reverse=True):
            s, ln = m.get("start", 0), m.get("length", 1)
            chars[s : s + ln] = list("@" + self.mention_name(m))
        return "".join(chars)

    def attachments(self, atts):
        out = []
        for a in atts or []:
            kind = (a.get("contentType") or "file").split(";")[0]
            size = a.get("size")
            size = f" {size/1024:.0f}KB" if size else ""
            name = f" {a['filename']}" if a.get("filename") else ""
            out.append(self.dim(f"[{kind}{name}{size}]"))
        return " ".join(out)

    def stamp(self, env, gid):
        ts = datetime.fromtimestamp(env.get("timestamp", 0) / 1000, timezone.utc).strftime("%H:%M:%S")  # UTC
        g = ""
        if self.show_group and gid:
            g = " " + self.dim("#" + self.snip(self.group_names.get(gid, gid[:8]), 20))
        return self.dim(f"[{ts}]") + g

    # ---------- main renderer ----------
    def render(self, event):
        env = event.get("envelope", {})
        name, uuid = env.get("sourceName") or env.get("sourceNumber"), env.get("sourceUuid")
        edit = env.get("editMessage")
        dm = (edit or {}).get("dataMessage") or env.get("dataMessage") \
            or (env.get("syncMessage") or {}).get("sentMessage")
        if not dm:
            return None  # receipts, typing, calls, etc.
        gi = dm.get("groupInfo") or {}
        gid = gi.get("groupId")
        if not gid or (self.group and gid != self.group):
            return None
        if gi.get("groupName"):
            self.group_names[gid] = gi["groupName"]
        self.learn(env)

        head = f"{self.stamp(env, gid)} {self.who(name, uuid)}"
        text = self.with_mentions(dm.get("message"), dm.get("mentions"))

        if edit:
            key = (uuid, edit.get("targetSentTimestamp"))
            old = self.msgs.get(key, {}).get("text")
            self.msgs[key] = {"name": name, "text": text}
            was = f"{self.dim(chr(34) + self.snip(old) + chr(34))} → " if old else ""
            return f"{head} {self.dim('(edited)')} {was}{text}"

        if dm.get("remoteDelete"):
            m = self.msgs.get((uuid, dm["remoteDelete"].get("timestamp")))
            what = self.dim(chr(34) + self.snip(m["text"]) + chr(34)) if m else self.dim("an earlier message")
            return f"{head} {self.dim('deleted')} {what}"

        if dm.get("reaction"):
            r = dm["reaction"]
            target = self.ref(r.get("targetAuthorUuid"), r.get("targetSentTimestamp"))
            verb = "removed reaction" if r.get("isRemove") else "reacted"
            return f"{head} {self.dim(verb)} {r.get('emoji', '')} {self.dim('to')} {target}"

        if gi.get("type") in ("UPDATE", "QUIT") and not text and not dm.get("attachments"):
            what = "left the group" if gi["type"] == "QUIT" else "updated the group"
            return f"{self.stamp(env, gid)} {self.dim('— ' + (name or 'someone') + ' ' + what + ' —')}"

        if dm.get("isExpirationUpdate"):
            secs = dm.get("expiresInSeconds", 0)
            return f"{head} {self.dim('set disappearing messages to ' + (f'{secs}s' if secs else 'off'))}"

        body = []
        if dm.get("quote"):
            q = dm["quote"]
            body.append(self.dim("↪") + " " + self.quote_ref(q))
        if dm.get("sticker"):
            body.append(self.dim("[sticker]"))
        if dm.get("attachments"):
            body.append(self.attachments(dm["attachments"]))
        if text:
            body.append(text)
        if not body:
            body.append(self.dim("(empty or unsupported message)"))

        self.msgs[(uuid, env.get("timestamp"))] = {"name": name, "text": text or "[attachment]"}
        return f"{head}: " + " ".join(body)
