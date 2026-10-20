"""
User store — SQLite, bcrypt-hashed passwords.

- The admin account is bootstrapped from .env (ADMIN_USERNAME / ADMIN_PASSWORD /
  ADMIN_EMAIL / ADMIN_TEAM). .env is the source of truth for that password:
  changing ADMIN_PASSWORD and restarting updates the stored hash.
- New users sign up at /signup (status 'pending'); an admin approves or
  rejects them in the chat (`signups`). Only 'active' users can log in.
- token_version is bumped whenever role, team, status or password changes, so
  JWTs issued before the change stop validating (see tokens.py).

Roles:
- admin: approves write requests and signups
- user:  write requests go to the approval queue

CLI for manual changes: python manage_users.py --help
"""
import os
import re
import sqlite3
from datetime import datetime, timezone

import bcrypt

DB = os.environ.get("USERS_DB", "/root/langgraph-orchestrator/users.db")

ROLES = ("user", "admin")
STATUSES = ("pending", "active", "rejected", "disabled")
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TEAM_RE = re.compile(r"^[A-Za-z0-9_-]{2,16}$")
MIN_PASSWORD_LEN = 8

_USER_COLUMNS = ("username", "email", "team", "role", "status", "token_version",
                 "created_at", "decided_by", "decided_at", "last_login")


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(password):
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _check(password, password_hash):
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def _public(row):
    """User dict without the password hash."""
    return {k: row[k] for k in _USER_COLUMNS} if row else None


def init():
    """Create the table and bootstrap the admin from .env. Idempotent."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                email         TEXT,
                team          TEXT,
                role          TEXT NOT NULL DEFAULT 'user',
                status        TEXT NOT NULL DEFAULT 'pending',
                token_version INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                decided_by    TEXT,
                decided_at    TEXT,
                last_login    TEXT
            )
        """)
    _bootstrap_admin()


def _bootstrap_admin():
    username = os.environ.get("ADMIN_USERNAME", "admin").strip()
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        return
    email = os.environ.get("ADMIN_EMAIL", "").strip()
    team = os.environ.get("ADMIN_TEAM", "").strip()
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            c.execute(
                "INSERT INTO users (username,password_hash,email,team,role,status,created_at,decided_by,decided_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (username, _hash(password), email, team, "admin", "active", _now(), "env", _now()))
            return
        changed = (not _check(password, row["password_hash"]) or row["role"] != "admin"
                   or row["status"] != "active")
        c.execute(
            "UPDATE users SET password_hash=?, role='admin', status='active', "
            "email=COALESCE(NULLIF(?,''),email), team=COALESCE(NULLIF(?,''),team), "
            "token_version=token_version+? WHERE username=?",
            (row["password_hash"] if _check(password, row["password_hash"]) else _hash(password),
             email, team, 1 if changed else 0, username))


def validate_signup(username, password, email, team):
    """Return an error message, or None when the input is acceptable."""
    if not USERNAME_RE.match(username or ""):
        return "Username must be 3-32 characters: letters, digits, '.', '_' or '-'."
    if not EMAIL_RE.match(email or ""):
        return "Please enter a valid e-mail address."
    if not TEAM_RE.match(team or ""):
        return "Team must be 2-16 characters (e.g. MSY, CSY)."
    if len(password or "") < MIN_PASSWORD_LEN:
        return f"Password must be at least {MIN_PASSWORD_LEN} characters."
    return None


def create_signup(username, password, email, team):
    """Store a pending signup. Returns (user_dict, None) or (None, error)."""
    username, email, team = (username or "").strip(), (email or "").strip(), (team or "").strip().upper()
    err = validate_signup(username, password, email, team)
    if err:
        return None, err
    with _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return None, "This username is already taken."
        c.execute(
            "INSERT INTO users (username,password_hash,email,team,role,status,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (username, _hash(password), email, team, "user", "pending", _now()))
    return get_user(username), None


def add_user(username, password, email="", team="", role="user", status="active", by="cli"):
    """Create an already-approved user (CLI / admin). Returns (user, error)."""
    if role not in ROLES:
        return None, f"role must be one of {ROLES}"
    team = (team or "").strip().upper()
    err = validate_signup(username, password, email or "x@x.x", team or "XX")
    if err:
        return None, err
    with _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            return None, "This username is already taken."
        c.execute(
            "INSERT INTO users (username,password_hash,email,team,role,status,created_at,decided_by,decided_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (username, _hash(password), email, team, role, status, _now(), by, _now()))
    return get_user(username), None


def get_user(username):
    with _conn() as c:
        return _public(c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())


def verify_user(username: str, password: str):
    """Return the public user dict for valid credentials of an ACTIVE user."""
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
        if row is None:
            _check(password or "", _hash("timing-equaliser"))  # same cost as a real check
            return None
        if row["status"] != "active" or not _check(password or "", row["password_hash"]):
            return None
        c.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), row["id"]))
    return get_user(row["username"])


def list_users(status=None):
    with _conn() as c:
        if status:
            rows = c.execute("SELECT * FROM users WHERE status=? ORDER BY created_at", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [_public(r) for r in rows]


def decide_signup(username, approve, decided_by, role="user"):
    """Approve (-> active, given role) or reject a PENDING signup.
    Returns (user, None) or (None, error)."""
    if role not in ROLES:
        return None, f"role must be one of {ROLES}"
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            return None, f"user '{username}' not found"
        if row["status"] != "pending":
            return None, f"user '{username}' is already {row['status']}"
        c.execute(
            "UPDATE users SET status=?, role=?, decided_by=?, decided_at=?, "
            "token_version=token_version+1 WHERE id=?",
            ("active" if approve else "rejected", role if approve else row["role"],
             decided_by, _now(), row["id"]))
    return get_user(username), None


def update_user(username, *, role=None, team=None, status=None, password=None, email=None):
    """Change fields of an existing user; bumps token_version. Returns (user, error)."""
    sets, args = [], []
    if role is not None:
        if role not in ROLES:
            return None, f"role must be one of {ROLES}"
        sets.append("role=?"); args.append(role)
    if status is not None:
        if status not in STATUSES:
            return None, f"status must be one of {STATUSES}"
        sets.append("status=?"); args.append(status)
    if team is not None:
        sets.append("team=?"); args.append(team.strip().upper())
    if email is not None:
        sets.append("email=?"); args.append(email.strip())
    if password is not None:
        if len(password) < MIN_PASSWORD_LEN:
            return None, f"Password must be at least {MIN_PASSWORD_LEN} characters."
        sets.append("password_hash=?"); args.append(_hash(password))
    if not sets:
        return get_user(username), None
    with _conn() as c:
        cur = c.execute(
            f"UPDATE users SET {', '.join(sets)}, token_version=token_version+1 WHERE username=?",
            (*args, username))
        if cur.rowcount == 0:
            return None, f"user '{username}' not found"
    return get_user(username), None


def get_admin_emails():
    """E-mails of active admins."""
    return [u["email"] for u in list_users("active") if u["role"] == "admin" and u.get("email")]
