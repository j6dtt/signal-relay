#!/usr/bin/env python3
"""Manage playback's local users (in the database, DB_PATH). Run inside the playback container:

    docker exec -it signal-playback python playback_users.py add NAME            # asks for the password
    docker exec -it signal-playback python playback_users.py add NAME --random   # makes one and shows it once
    docker exec -it signal-playback python playback_users.py passwd NAME [--random]
    docker exec -it signal-playback python playback_users.py remove NAME
    docker exec -it signal-playback python playback_users.py list

    docker exec -it signal-playback python playback_users.py token add NAME      # a token for a tool, shown once
    docker exec -it signal-playback python playback_users.py token remove NAME
    docker exec -it signal-playback python playback_users.py token list

A new password, or removing a user, logs that user out everywhere. A token (for a tool such as the AI assistant)
can only read the monitoring API (GET /api/monitor/...), sent as "Authorization: Bearer <token>". Adding a token
with an existing name replaces it (the old one stops working at once).
"""
import getpass, os, re, sys
from datetime import datetime, timezone
from auth import hash_password, random_password, new_token
from store import Store

MIN_LEN = 12


def ask():
    for _ in range(3):
        pw = getpass.getpass("New password: ")
        if len(pw) < MIN_LEN:
            print(f"Too short: at least {MIN_LEN} characters.")
            continue
        if getpass.getpass("Again: ") != pw:
            print("They don't match.")
            continue
        return pw
    sys.exit("No password set.")


def when(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ms else "never"


def tokens(store, argv):
    cmd, name = (argv[2] if len(argv) > 2 else None), (argv[3] if len(argv) > 3 else None)
    if cmd == "list":
        for t in store.tokens():
            print(f"{t['name']:20} created {when(t['created'])}, last used {when(t['last_used'])}")
        return
    if cmd not in ("add", "remove") or not name or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        sys.exit("Use: token add NAME | token remove NAME | token list  (NAME: letters, digits, . _ -)")
    if cmd == "remove":
        print(f"Removed token {name}." if store.remove_token(name) else f"There is no token {name}.")
        return
    token, h = new_token()
    store.set_token(name, h)
    print(f"Token {name} is ready. It can read the monitoring API only.")
    print(f"Token (shown once, store it now): {token}")


def main(argv):
    if len(argv) < 2 or argv[1] not in ("add", "passwd", "remove", "list", "token"):
        sys.exit(__doc__)
    store = Store(os.environ.get("DB_PATH", "relay.db"))
    if argv[1] == "token":
        return tokens(store, argv)
    cmd, name = argv[1], (argv[2] if len(argv) > 2 else None)
    if cmd == "list":
        for u in store.users():
            print(f"{u['name']:20} created {when(u['created'])}, last login {when(u['last_login'])}")
        return
    if not name or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name):
        sys.exit("Give a user name: letters, digits, . _ - (up to 64).")
    if cmd == "remove":
        print(f"Removed {name}." if store.remove_user(name) else f"There is no user {name}.")
        return
    exists = store.user(name) is not None
    if cmd == "add" and exists:
        sys.exit(f"{name} already exists. To change the password: passwd {name}")
    if cmd == "passwd" and not exists:
        sys.exit(f"There is no user {name}. To create it: add {name}")
    pw = random_password() if "--random" in argv else ask()
    store.set_user(name, hash_password(pw))
    print(f"{'Added' if cmd == 'add' else 'Changed the password of'} {name}.")
    if "--random" in argv:
        print(f"Password (shown once, store it now): {pw}")


if __name__ == "__main__":
    main(sys.argv)
