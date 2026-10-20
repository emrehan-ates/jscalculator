"""
User administration CLI (replaces make_password.py).

  python manage_users.py list [--status pending|active|rejected|disabled]
  python manage_users.py add USERNAME --email E --team MSY [--role admin]
  python manage_users.py approve USERNAME [--role admin]
  python manage_users.py reject USERNAME
  python manage_users.py set USERNAME [--role R] [--team T] [--email E] [--status S]
  python manage_users.py passwd USERNAME

Passwords are always read with a hidden prompt, never from the command line.
The admin account itself comes from ADMIN_* in .env.
"""
import argparse
import getpass
import sys

from dotenv import load_dotenv

load_dotenv()

import users  # noqa: E402  (USERS_DB may come from .env)


def _password():
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("Passwords do not match.")
    return pw


def _done(user, err):
    if err:
        sys.exit(f"Error: {err}")
    print(f"{user['username']}: role={user['role']} team={user['team']} status={user['status']}")


def main():
    ap = argparse.ArgumentParser(description="NetworkGPT user administration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("--status", choices=users.STATUSES)
    p = sub.add_parser("add"); p.add_argument("username"); p.add_argument("--email", default="")
    p.add_argument("--team", default=""); p.add_argument("--role", choices=users.ROLES, default="user")
    p = sub.add_parser("approve"); p.add_argument("username"); p.add_argument("--role", choices=users.ROLES, default="user")
    p = sub.add_parser("reject"); p.add_argument("username")
    p = sub.add_parser("set"); p.add_argument("username"); p.add_argument("--role", choices=users.ROLES)
    p.add_argument("--team"); p.add_argument("--email"); p.add_argument("--status", choices=users.STATUSES)
    p = sub.add_parser("passwd"); p.add_argument("username")
    a = ap.parse_args()

    users.init()
    if a.cmd == "list":
        for u in users.list_users(a.status):
            print(f"{u['username']:<20} {u['role']:<6} {u['team'] or '-':<6} {u['status']:<9} {u['email'] or ''}")
    elif a.cmd == "add":
        _done(*users.add_user(a.username, _password(), a.email, a.team, a.role, by="cli"))
    elif a.cmd == "approve":
        _done(*users.decide_signup(a.username, True, "cli", a.role))
    elif a.cmd == "reject":
        _done(*users.decide_signup(a.username, False, "cli"))
    elif a.cmd == "set":
        _done(*users.update_user(a.username, role=a.role, team=a.team, email=a.email, status=a.status))
    elif a.cmd == "passwd":
        _done(*users.update_user(a.username, password=_password()))


if __name__ == "__main__":
    main()
