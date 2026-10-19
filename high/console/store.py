"""The console's database: one SQLite file (DB_PATH) for chat history, monitoring, users and sessions.

Only the console writes to it (one process), so SQLite's single writer is never a problem. Everything older than
RETENTION_DAYS is deleted by cleanup(), which the console runs every hour. All SQL lives here: moving to another
database later (e.g. Postgres, for k8s replicas) means replacing this file only.
"""
import json, logging, sqlite3, time

log = logging.getLogger("console")

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (          -- Signal events shown in the console, and the receiver's notices
    id     INTEGER PRIMARY KEY,
    at     INTEGER NOT NULL,                 -- when the console got it (ms)
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
CREATE TABLE IF NOT EXISTS mon_incidents (  -- a period of trouble on one side/host, with its likely cause
    id INTEGER PRIMARY KEY, scope TEXT NOT NULL, opened INTEGER NOT NULL, closed INTEGER, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS mon_incidents_opened ON mon_incidents(opened);
CREATE TABLE IF NOT EXISTS api_tokens (     -- for tools (e.g. the AI assistant): read-only monitoring API; stored hashed
    name TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, created INTEGER NOT NULL, last_used INTEGER);
CREATE TABLE IF NOT EXISTS ai_log (         -- every question to the AI assistant and its answer (relay-assist)
    id INTEGER PRIMARY KEY, at INTEGER NOT NULL, user TEXT NOT NULL, incident INTEGER, body TEXT NOT NULL,
    rating TEXT,                             -- "helpful" / "not_helpful" / NULL
    prompt TEXT,                             -- the exact prompt and tool data; NULL once dropped (AI_LOG=summary)
    prompt_until INTEGER,                    -- drop the prompt after this (ms), unless keep_prompt
    keep_prompt INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ai_log_incident ON ai_log(incident);
CREATE TABLE IF NOT EXISTS mon_metrics (    -- each part's state and figures once a minute, kept METRICS_HOURS
    minute INTEGER NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (key, minute));
CREATE INDEX IF NOT EXISTS mon_metrics_minute ON mon_metrics(minute);
CREATE TABLE IF NOT EXISTS mon_link (       -- diode health per left host, in 5-minute buckets
    host TEXT NOT NULL, bucket INTEGER NOT NULL, received INTEGER NOT NULL, expected INTEGER NOT NULL,
    delay_sum INTEGER NOT NULL, delay_n INTEGER NOT NULL, PRIMARY KEY (host, bucket));
"""
BUCKET_MS = 5 * 60 * 1000
METRICS_HOURS = 48


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


class Counted:
    """The database connection, counting its errors (disk full, locked, damaged) for the console's own tile
    (DB_ERROR). A duplicate key is expected (IntegrityError) and not counted."""
    def __init__(self, db):
        self._db, self.errors = db, 0

    def _run(self, fn, *a):
        try:
            return fn(*a)
        except sqlite3.IntegrityError:
            raise
        except sqlite3.Error:
            self.errors += 1
            raise

    def execute(self, *a):
        return self._run(self._db.execute, *a)

    def executemany(self, *a):
        return self._run(self._db.executemany, *a)

    def executescript(self, *a):
        return self._run(self._db.executescript, *a)

    def __getattr__(self, name):
        return getattr(self._db, name)

    def __enter__(self):
        return self._db.__enter__()

    def __exit__(self, *a):
        return self._db.__exit__(*a)


def best_effort(fn):
    """A monitoring write that must not stop monitoring when the database can't take it (disk full, locked):
    the error is counted (DB_ERROR on the console's tile) and logged once a minute, and the caller carries on with
    what it has in memory. Chat writes are not wrapped: their failure must reach the receiver (DELIVER_FAILED)."""
    def run(self, *a, **k):
        try:
            return fn(self, *a, **k)
        except sqlite3.Error as e:
            if time.time() - getattr(self, "_warned", 0) > 60:
                self._warned = time.time()
                log.error("The database could not save monitoring data (%s); monitoring carries on in memory.", e)
            return None
    return run


class Store:
    def __init__(self, path, retention_days=30):
        self.retention_ms = retention_days * 86400 * 1000
        self.db = Counted(sqlite3.connect(path, isolation_level=None))   # autocommit; each write is one statement
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

    @best_effort
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
    @best_effort
    def add_mon_event(self, ev):
        cur = self.db.execute("INSERT INTO mon_events (at, part, code, sev, text, data) VALUES (?, ?, ?, ?, ?, ?)",
                              (ev["at"], ev["part"], ev["code"], ev["sev"], ev["text"],
                               json.dumps(ev.get("data") or {}, separators=(",", ":"))))
        return cur.lastrowid

    def mon_events(self, since_ms=0, limit=200, part=None, before_id=None):
        """Newest last. before_id: only older ones (the Log window's "Show older")."""
        q, args = "SELECT id, at, part, code, sev, text, data FROM mon_events WHERE at >= ?", [since_ms]
        if before_id:
            q += " AND id < ?"; args.append(before_id)
        if part:
            q += " AND part = ?"; args.append(part)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        rows = self.db.execute(q, args).fetchall()
        return [dict(zip(("id", "at", "part", "code", "sev", "text"), r[:6]), data=json.loads(r[6] or "{}"))
                for r in reversed(rows)]

    @best_effort
    def put_part(self, key, body):
        self.db.execute("INSERT INTO mon_parts (key, body) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET body = excluded.body",
                        (key, json.dumps(body, separators=(",", ":"))))

    def parts(self):
        return {k: json.loads(b) for k, b in self.db.execute("SELECT key, body FROM mon_parts")}

    def remove_part(self, key):
        self.db.execute("DELETE FROM mon_parts WHERE key = ?", (key,))

    @best_effort
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

    def add_incident(self, inc):
        cur = self.db.execute("INSERT INTO mon_incidents (scope, opened, closed, body) VALUES (?, ?, ?, ?)",
                              (inc["scope"], inc["opened"], inc.get("closed"), json.dumps(inc, separators=(",", ":"))))
        return cur.lastrowid

    @best_effort
    def put_incident(self, inc):
        self.db.execute("UPDATE mon_incidents SET closed = ?, body = ? WHERE id = ?",
                        (inc.get("closed"), json.dumps(inc, separators=(",", ":")), inc["id"]))

    def incidents(self, open_only=False, since_ms=0, limit=50):
        """Newest first."""
        q = "SELECT body FROM mon_incidents WHERE " + ("closed IS NULL" if open_only else "(closed IS NULL OR closed >= ?)")
        args = [] if open_only else [since_ms]
        rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        return [json.loads(r[0]) for r in rows]

    def incident(self, id_):
        row = self.db.execute("SELECT body FROM mon_incidents WHERE id = ?", (id_,)).fetchone()
        return json.loads(row[0]) if row else None

    @best_effort
    def add_metrics(self, minute, rows):
        self.db.executemany("INSERT OR REPLACE INTO mon_metrics (minute, key, body) VALUES (?, ?, ?)",
                            [(minute, k, json.dumps(v, separators=(",", ":"))) for k, v in rows.items()])

    def metrics_all(self, since_ms):
        """Every part's minute rows since then: (minute, key, state), for the Monitor tab's timeline."""
        return [(m, k, (json.loads(b) or {}).get("s")) for m, k, b in
                self.db.execute("SELECT minute, key, body FROM mon_metrics WHERE minute >= ? ORDER BY minute", (since_ms,))]

    def metrics(self, key, since_ms):
        return [{"minute": m, **json.loads(b)} for m, b in
                self.db.execute("SELECT minute, body FROM mon_metrics WHERE key = ? AND minute >= ? ORDER BY minute",
                                (key, since_ms))]

    # ---- AI assistant log ----
    def add_ai(self, entry, prompt, prompt_until, keep_prompt):
        body = {k: v for k, v in entry.items() if k != "prompt"}
        cur = self.db.execute("INSERT INTO ai_log (at, user, incident, body, prompt, prompt_until, keep_prompt) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (entry["at"], entry["user"], entry.get("incident"), json.dumps(body, separators=(",", ":")),
                               json.dumps(prompt, separators=(",", ":")) if prompt is not None else None,
                               prompt_until, 1 if keep_prompt else 0))
        return cur.lastrowid

    def ai_entries(self, incident=None, limit=20, general=False):
        """Newest first; "detail" says whether the full prompt is still kept. general: only questions not about an
        incident."""
        q = "SELECT id, rating, prompt IS NOT NULL, keep_prompt, body FROM ai_log"
        args = []
        if incident is not None:
            q += " WHERE incident = ?"; args.append(incident)
        elif general:
            q += " WHERE incident IS NULL"
        rows = self.db.execute(q + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        return [{**json.loads(b), "id": i, "rating": r, "detail": "full" if has else "summary", "kept": bool(k)}
                for i, r, has, k, b in rows]

    def clear_asks(self, incident=None):
        """Clear: delete the questions of one box (an incident's Ask box, or Ask Analyst for None); an incident's
        explain cards and wrap-ups stay. Returns how many."""
        if incident is None:
            return self.db.execute("DELETE FROM ai_log WHERE incident IS NULL").rowcount
        return self.db.execute("DELETE FROM ai_log WHERE incident = ? AND json_extract(body, '$.kind') = 'ask'",
                               (incident,)).rowcount

    def ai_asks(self, ids, incident):
        """Earlier questions of one conversation (kind ask, about the same incident or none), oldest first."""
        if not ids:
            return []
        rows = self.db.execute(f"SELECT id, body FROM ai_log WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id",
                               list(ids)).fetchall()
        out = [{**json.loads(b), "id": i} for i, b in rows]
        return [e for e in out if e.get("kind") == "ask" and e.get("incident") == incident]

    def rate_ai(self, id_, rating):
        """A rating; "not_helpful" also keeps the full prompt (if it is still there) for the retention period."""
        return self.db.execute("UPDATE ai_log SET rating = ?, keep_prompt = keep_prompt OR ? WHERE id = ?",
                               (rating, 1 if rating == "not_helpful" else 0, id_)).rowcount > 0

    # ---- API tokens ----
    def set_token(self, name, token_hash):
        self.db.execute("INSERT INTO api_tokens (name, hash, created) VALUES (?, ?, ?) "
                        "ON CONFLICT(name) DO UPDATE SET hash = excluded.hash, created = excluded.created, last_used = NULL",
                        (name, token_hash, now_ms()))

    def token_name(self, token_hash):
        row = self.db.execute("SELECT name FROM api_tokens WHERE hash = ?", (token_hash,)).fetchone()
        if row:
            try:
                self.db.execute("UPDATE api_tokens SET last_used = ? WHERE name = ?", (now_ms(), row[0]))
            except sqlite3.Error:
                pass                                       # a read must still work with a full disk
        return row[0] if row else None

    def tokens(self):
        return [dict(zip(("name", "created", "last_used"), r))
                for r in self.db.execute("SELECT name, created, last_used FROM api_tokens ORDER BY name")]

    def remove_token(self, name):
        return self.db.execute("DELETE FROM api_tokens WHERE name = ?", (name,)).rowcount > 0

    # ---- retention ----
    def cleanup(self):
        """Delete everything older than the retention period. Returns {table: rows deleted}."""
        c, done = self.cutoff(), {}
        for table, col in (("chat", "at"), ("mon_events", "at"), ("mon_link", "bucket")):
            done[table] = self.db.execute(f"DELETE FROM {table} WHERE {col} < ?", (c,)).rowcount
        done["mon_metrics"] = self.db.execute("DELETE FROM mon_metrics WHERE minute < ?",
                                              (now_ms() - METRICS_HOURS * 3600_000,)).rowcount
        done["ai_log"] = self.db.execute("DELETE FROM ai_log WHERE at < ?", (c,)).rowcount
        done["ai prompts"] = self.db.execute("UPDATE ai_log SET prompt = NULL WHERE prompt IS NOT NULL AND keep_prompt = 0 "
                                             "AND prompt_until < ?", (now_ms(),)).rowcount
        done["mon_incidents"] = self.db.execute("DELETE FROM mon_incidents WHERE closed < ?", (c,)).rowcount
        done["sessions"] = self.db.execute("DELETE FROM sessions WHERE expires < ?", (now_ms(),)).rowcount
        return done
