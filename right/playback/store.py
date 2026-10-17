"""Playback's database: one SQLite file (DB_PATH) for chat history, monitoring, users and sessions.

Only playback writes to it (one process), so SQLite's single writer is never a problem. Everything older than
RETENTION_DAYS is deleted by cleanup(), which playback runs every hour. All SQL lives here: moving to another
database later (e.g. Postgres, for k8s replicas) means replacing this file only.
"""
import json, sqlite3, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (          -- Signal events shown in playback, and the receiver's notices
    id     INTEGER PRIMARY KEY,
    at     INTEGER NOT NULL,                 -- when playback got it (ms)
    kind   TEXT NOT NULL,                    -- "event" or "notice"
    dedupe TEXT UNIQUE,                      -- group|sender|sent time: the same message via two accounts is kept once
    body   TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chat_at ON chat(at);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY, hash TEXT NOT NULL, created INTEGER NOT NULL, last_login INTEGER);
CREATE TABLE IF NOT EXISTS sessions (       -- token is stored hashed: a copy of the database can't log anyone in
    token TEXT PRIMARY KEY, user TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS mon_events (     -- the monitoring timeline: what happened, newest last
    id INTEGER PRIMARY KEY, at INTEGER NOT NULL, part TEXT NOT NULL, code TEXT NOT NULL, sev TEXT NOT NULL,
    text TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS mon_events_at ON mon_events(at);
CREATE TABLE IF NOT EXISTS mon_parts (      -- the latest known state of each monitored part
    key TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mon_link (       -- diode health per left host, in 5-minute buckets
    host TEXT NOT NULL, bucket INTEGER NOT NULL, received INTEGER NOT NULL, expected INTEGER NOT NULL,
    delay_sum INTEGER NOT NULL, delay_n INTEGER NOT NULL, PRIMARY KEY (host, bucket));
"""
BUCKET_MS = 5 * 60 * 1000


def now_ms():
    return int(time.time() * 1000)


def chat_key(event):
    """group|sender|sent time of a Signal event, or None. The same message relayed by two accounts has the same key."""
    env = event.get("envelope") or {}
    edit = env.get("editMessage")
    dm = ((edit or {}).get("dataMessage") or env.get("dataMessage")
          or (env.get("syncMessage") or {}).get("sentMessage") or {})
    gid = (dm.get("groupInfo") or {}).get("groupId")
    who = env.get("sourceUuid") or env.get("sourceNumber")
    ts = env.get("timestamp")
    return f"{gid}|{who}|{ts}" if gid and who and ts else None


class Store:
    def __init__(self, path, retention_days=30):
        self.retention_ms = retention_days * 86400 * 1000
        self.db = sqlite3.connect(path, isolation_level=None)       # autocommit; each write is one statement
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    def cutoff(self):
        return now_ms() - self.retention_ms

    # ---- chat ----
    def add_chat(self, kind, body, key=None, at=None):
        """Store one event or notice. False if an event with the same key is already stored (a copy)."""
        try:
            self.db.execute("INSERT INTO chat (at, kind, dedupe, body) VALUES (?, ?, ?, ?)",
                            (at or now_ms(), kind, key, json.dumps(body, separators=(",", ":"))))
            return True
        except sqlite3.IntegrityError:
            return False

    def has_chat(self, key):
        return key is not None and self.db.execute("SELECT 1 FROM chat WHERE dedupe = ?", (key,)).fetchone() is not None

    def chat_since(self, since_ms):
        """(at, kind, body) in the order they arrived."""
        for at, kind, body in self.db.execute("SELECT at, kind, body FROM chat WHERE at >= ? ORDER BY id", (since_ms,)):
            yield at, kind, json.loads(body)

    # ---- key/value ----
    def get(self, k, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k = ?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, k, v):
        self.db.execute("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                        (k, json.dumps(v, separators=(",", ":"))))

    # ---- users and sessions ----
    def user(self, name):
        row = self.db.execute("SELECT name, hash, created, last_login FROM users WHERE name = ?", (name,)).fetchone()
        return dict(zip(("name", "hash", "created", "last_login"), row)) if row else None

    def users(self):
        return [dict(zip(("name", "created", "last_login"), r))
                for r in self.db.execute("SELECT name, created, last_login FROM users ORDER BY name")]

    def set_user(self, name, pw_hash):
        self.db.execute("INSERT INTO users (name, hash, created) VALUES (?, ?, ?) "
                        "ON CONFLICT(name) DO UPDATE SET hash = excluded.hash", (name, pw_hash, now_ms()))
        self.db.execute("DELETE FROM sessions WHERE user = ?", (name,))     # a new password logs out old sessions

    def remove_user(self, name):
        self.db.execute("DELETE FROM sessions WHERE user = ?", (name,))
        return self.db.execute("DELETE FROM users WHERE name = ?", (name,)).rowcount > 0

    def logged_in(self, name):
        self.db.execute("UPDATE users SET last_login = ? WHERE name = ?", (now_ms(), name))

    def add_session(self, token_hash, user, expires):
        self.db.execute("INSERT INTO sessions (token, user, created, expires) VALUES (?, ?, ?, ?)",
                        (token_hash, user, now_ms(), expires))

    def session_user(self, token_hash):
        row = self.db.execute("SELECT s.user FROM sessions s JOIN users u ON u.name = s.user "
                              "WHERE s.token = ? AND s.expires > ?", (token_hash, now_ms())).fetchone()
        return row[0] if row else None

    def end_session(self, token_hash):
        self.db.execute("DELETE FROM sessions WHERE token = ?", (token_hash,))

    # ---- monitoring ----
    def add_mon_event(self, ev):
        cur = self.db.execute("INSERT INTO mon_events (at, part, code, sev, text, data) VALUES (?, ?, ?, ?, ?, ?)",
                              (ev["at"], ev["part"], ev["code"], ev["sev"], ev["text"],
                               json.dumps(ev.get("data") or {}, separators=(",", ":"))))
        return cur.lastrowid

    def mon_events(self, since_ms=0, limit=200, part=None):
        """Newest last."""
        q, args = "SELECT id, at, part, code, sev, text, data FROM mon_events WHERE at >= ?", [since_ms]
        if part:
            q += " AND part = ?"; args.append(part)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        rows = self.db.execute(q, args).fetchall()
        return [dict(zip(("id", "at", "part", "code", "sev", "text"), r[:6]), data=json.loads(r[6] or "{}"))
                for r in reversed(rows)]

    def put_part(self, key, body):
        self.db.execute("INSERT INTO mon_parts (key, body) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET body = excluded.body",
                        (key, json.dumps(body, separators=(",", ":"))))

    def parts(self):
        return {k: json.loads(b) for k, b in self.db.execute("SELECT key, body FROM mon_parts")}

    def remove_part(self, key):
        self.db.execute("DELETE FROM mon_parts WHERE key = ?", (key,))

    def add_link(self, host, at, received, expected, delay_ms=None):
        b = at - at % BUCKET_MS
        self.db.execute("INSERT INTO mon_link (host, bucket, received, expected, delay_sum, delay_n) VALUES (?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(host, bucket) DO UPDATE SET received = received + excluded.received, "
                        "expected = expected + excluded.expected, delay_sum = delay_sum + excluded.delay_sum, "
                        "delay_n = delay_n + excluded.delay_n",
                        (host, b, received, expected, delay_ms or 0, 1 if delay_ms is not None else 0))

    def link(self, host, since_ms):
        return [dict(zip(("bucket", "received", "expected", "delay_sum", "delay_n"), r)) for r in
                self.db.execute("SELECT bucket, received, expected, delay_sum, delay_n FROM mon_link "
                                "WHERE host = ? AND bucket >= ? ORDER BY bucket", (host, since_ms - since_ms % BUCKET_MS))]

    # ---- retention ----
    def cleanup(self):
        """Delete everything older than the retention period. Returns {table: rows deleted}."""
        c, done = self.cutoff(), {}
        for table, col in (("chat", "at"), ("mon_events", "at"), ("mon_link", "bucket")):
            done[table] = self.db.execute(f"DELETE FROM {table} WHERE {col} < ?", (c,)).rowcount
        done["sessions"] = self.db.execute("DELETE FROM sessions WHERE expires < ?", (now_ms(),)).rowcount
        return done
