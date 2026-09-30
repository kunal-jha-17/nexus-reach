#!/usr/bin/env python3
"""
manage.py -- housekeeping from the command line (no browser needed).

  Accounts
    python3 manage.py list
    python3 manage.py create you@example.com            # asks for a password
    python3 manage.py reset-password you@example.com    # forgot your password? run this
    python3 manage.py make-admin you@example.com
    python3 manage.py delete you@example.com            # removes their database too

  Secret key
    python3 manage.py rotate-key       # after setting SECRET_KEY=<new> and SECRET_KEY_OLD=<previous>

  Outside storage (Supabase / S3 / SYNC_FOLDER -- only when configured)
    python3 manage.py sync-status      # what's stored, how big, last backup, any conflicts
    python3 manage.py backups          # dated backups available
    python3 manage.py restore-backup 2026-09-21   # write that day's backup into the data folder
    python3 manage.py push             # make storage match THIS computer (overwrites storage)
    python3 manage.py pull             # make THIS computer match storage (overwrites local)

Stop the app before running commands that change data: they and the running app
would otherwise both be writing. There's no "forgot password" email, so
reset-password is the recovery route -- it only works for someone who can run
commands on the server, which is the point.
"""
import getpass
import sys

from werkzeug.security import generate_password_hash

import accounts
import config
import persistence


def _find(email):
    row = accounts._find(email)
    if not row:
        sys.exit(f"No account for {email}")
    return row


def _ask_password(email=""):
    pw = getpass.getpass("New password (8+ characters): ")
    try:
        accounts.check_password(pw, email)
    except ValueError as e:
        sys.exit(str(e))
    if pw != getpass.getpass("Again: "):
        sys.exit("Passwords didn't match.")
    return pw


def _store():
    store = persistence.store_from_env()
    if store is None:
        sys.exit("Outside storage isn't configured (set S3_BUCKET + S3_ACCESS_KEY_ID, or SYNC_FOLDER).")
    return store


def _confirm(question, expected="yes"):
    if input(f"{question} Type '{expected}' to continue: ").strip() != expected:
        sys.exit("Cancelled.")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    storage_cmds = {"sync-status", "backups", "restore-backup", "push", "pull"}

    # Commands that change data run with the storage hooks on, so their changes are copied out too.
    live = None
    if cmd not in storage_cmds and persistence.store_from_env() is not None:
        live = persistence.start_if_configured()
    accounts.ensure()

    if cmd == "list":
        for u in accounts.admin_list_users():
            print(f"{u['id']:>3}  {u['email']:<34} {'admin' if u['is_admin'] else '     '}  "
                  f"{u['leads'] if u['leads'] is not None else '?':>6} leads   last sign-in: {u['last_login_at'] or 'never'}")
    elif cmd == "create" and len(argv) > 2:
        u = accounts.create_user(argv[2], _ask_password(argv[2]))
        print(f"Created {u['email']}" + (" (admin)" if u["is_admin"] else ""))
    elif cmd == "reset-password" and len(argv) > 2:
        row = _find(argv[2])
        with accounts._conn() as c:
            c.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                      (generate_password_hash(_ask_password(argv[2]), method=accounts.PW_METHOD), row["id"]))
        print("Password changed. Any browser they were signed in on is now signed out.")
    elif cmd == "make-admin" and len(argv) > 2:
        row = _find(argv[2])
        with accounts._conn() as c:
            c.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (row["id"],))
        print(f"{row['email']} is now an admin.")
    elif cmd == "delete" and len(argv) > 2:
        row = _find(argv[2])
        _confirm(f"Permanently delete {row['email']} and ALL their data?", row["email"])
        accounts.delete_user(row["id"])
        print("Deleted.")

    elif cmd == "rotate-key":
        import cryptobox
        if not cryptobox.fallback_keys():
            sys.exit("Set SECRET_KEY to the NEW key and SECRET_KEY_OLD to the previous one first, then run this again.")
        n = accounts.reencrypt_all()
        print(f"Re-encrypted the saved keys of {n} account(s) with the new SECRET_KEY.")
        if live is not None:
            live.push_all()
            live.backup_now()
            print("Re-uploaded every stored copy with the new key.")
            print(f"Older daily backups were encrypted with the old key and stay readable only while it is listed in\n"
                  f"SECRET_KEY_OLD -- keep it there for {live.keep_days} more days (until they age out), then remove it.")
        else:
            print("You can remove SECRET_KEY_OLD once you've restarted the app.")

    elif cmd == "sync-status":
        s = persistence.Sync(_store(), config._server_env("S3_PREFIX", "nexusreach/"))
        st = s.status()
        print(f"Storage: {st['kind']}   prefix: {st['prefix']}   used: {st['used_mb']} MB of {st['budget_mb']} MB budget")
        print(f"Last daily backup: {st['last_backup_day'] or 'none yet'}   (keeping {st['keep_days']} days)")
        for o in sorted(s.store.list(s._cur()), key=lambda o: o["key"]):
            print(f"  {o['key'][len(s._cur()):]:<20} {o['size'] / 1000:>9.1f} KB")
        held = s.store.list(f"{s.prefix}conflicts/")
        if held:
            print("\nCONFLICT copies saved (a second copy of the app changed stored data):")
            for o in held:
                print(f"  {o['key']}")
            print("Decide which version to keep, then run `push` (keep this computer's) or `pull` (keep storage's).")
    elif cmd == "backups":
        s = persistence.Sync(_store(), config._server_env("S3_PREFIX", "nexusreach/"))
        days = s._backups_by_day()
        for d in sorted(days):
            print(f"{d}   {len(days[d])} files   {sum(o['size'] for o in days[d]) / 1000:.0f} KB")
        if not days:
            print("No backups yet.")
    elif cmd == "restore-backup" and len(argv) > 2:
        s = persistence.Sync(_store(), config._server_env("S3_PREFIX", "nexusreach/"))
        _confirm(f"This overwrites the databases on this computer with the backup from {argv[2]}. Is the app stopped?")
        print(f"Restored {s.restore_backup(argv[2])} file(s). Start the app; it will copy them to storage.")
    elif cmd == "push":
        s = persistence.Sync(_store(), config._server_env("S3_PREFIX", "nexusreach/"))
        _confirm("This OVERWRITES the stored copies with the data on this computer.")
        print(f"Uploaded {s.push_all()} file(s).")
    elif cmd == "pull":
        s = persistence.Sync(_store(), config._server_env("S3_PREFIX", "nexusreach/"))
        _confirm("This OVERWRITES the data on this computer with the stored copies.")
        print(f"Downloaded {s.pull_all()} file(s).")
    else:
        sys.exit(__doc__)

    if live is not None:
        live.flush()


if __name__ == "__main__":
    main(sys.argv)
