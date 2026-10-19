"""Local login for the console: scrypt password hashes, session tokens, and a lock after repeated failures."""
import base64, hashlib, hmac, secrets, time

SCRYPT = dict(n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024)   # ~32 MB and ~0.1 s per check
SESSION_HOURS = 12
LOCK_AFTER, LOCK_SECONDS = 5, 300                           # 5 failures in a row -> 5 minutes locked


def _b64(b):
    return base64.b64encode(b).decode()


def hash_password(password):
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${_b64(salt)}${_b64(h)}"


def check_password(password, stored):
    try:
        alg, n, r, p, salt, want = stored.split("$")
        if alg != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), dklen=32,
                             n=int(n), r=int(r), p=int(p), maxmem=SCRYPT["maxmem"])
        return hmac.compare_digest(got, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


DUMMY = hash_password(secrets.token_urlsafe(16))   # checked for unknown users, so timing doesn't reveal who exists


def new_token():
    """(token for the cookie, its hash for the database)."""
    t = secrets.token_urlsafe(32)
    return t, token_hash(t)


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def random_password():
    return secrets.token_urlsafe(15)               # 20 characters


class Lockout:
    """Counts failed logins per (address, user name); locks that pair for LOCK_SECONDS after LOCK_AFTER failures."""
    def __init__(self):
        self.fails = {}                            # key -> [count, locked_until]

    def locked(self, key):
        n, until = self.fails.get(key, (0, 0))
        return until > time.time()

    def failed(self, key):
        n, until = self.fails.get(key, (0, 0))
        n += 1
        self.fails[key] = [0, time.time() + LOCK_SECONDS] if n >= LOCK_AFTER else [n, 0]
        if len(self.fails) > 10000:                # never grows without bound
            self.fails.clear()
        return n >= LOCK_AFTER

    def ok(self, key):
        self.fails.pop(key, None)
