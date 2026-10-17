#!/usr/bin/env python3
"""Manage playback's local users (in the database, DB_PATH). Run inside the playback container:

    docker exec -it signal-playback python playback_users.py add NAME            # asks for the password
    docker exec -it signal-playback python playback_users.py add NAME --random   # makes one and shows it once
    docker exec -it signal-playback python playback_users.py passwd NAME [--random]
    docker exec -it signal-playback python playback_users.py remove NAME
    docker exec -it signal-playback python playback_users.py list

A new password, or removing a user, logs that user out everywhere.
"""
import getpass, os, re, sys
from datetime import datetime, timezone
from auth import hash_password, random_password
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


def main(argv):
    if len(argv) < 2 or argv[1] not in ("add", "passwd", "remove", "list"):
        sys.exit(__doc__)
    store = Store(os.environ.get("DB_PATH", "relay.db"))
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
