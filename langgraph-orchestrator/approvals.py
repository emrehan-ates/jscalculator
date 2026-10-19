"""Persistent approval queue (SQLite). Independent of Chainlit's data layer.

A write request from a 'user' is stored here as 'pending' and the turn ends.
An admin later approves/denies it from the UI; on approval it is executed.
Nothing waits inside a live prompt, so approvals can sit for days safely.
"""
import sqlite3
import json
import asyncio
from datetime import datetime, timezone

DB = "/root/langgraph-orchestrator/approvals.db"


def _conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def _now():
    return datetime.now(timezone.utc).isoformat()


def _init():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS approvals (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                requester       TEXT NOT NULL,
                requester_email TEXT,
                user_input      TEXT NOT NULL,
                intent          TEXT,
                summary         TEXT,
                plan_json       TEXT NOT NULL,
                status          TEXT NOT NULL DEFAULT 'pending',
                created_at      TEXT NOT NULL,
                decided_by      TEXT,
                decided_at      TEXT,
                result_json     TEXT,
                fabric          TEXT
            )
        """)
        # Migrate existing DBs that predate the requester_email column
        try:
            c.execute("ALTER TABLE approvals ADD COLUMN requester_email TEXT")
        except Exception:
            pass  # Column already exists
        
        try:
            c.execute("ALTER TABLE approvals ADD COLUMN fabric TEXT")
        except Exception:
            pass


def _create(requester, requester_email, user_input, intent, summary, plan, fabric):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO approvals (requester,requester_email,user_input,intent,summary,plan_json,fabric,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (requester, requester_email, user_input, intent, summary, json.dumps(plan), fabric, "pending", _now()),
        )
        return cur.lastrowid


def _list_pending():
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def _list_by_requester(username):
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM approvals WHERE requester=? ORDER BY created_at DESC LIMIT 50",
            (username,),
        ).fetchall()
        return [dict(r) for r in rows]


def _get(approval_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(r) if r else None


def _set_decision(approval_id, status, decided_by, result=None):
    with _conn() as c:
        c.execute(
            "UPDATE approvals SET status=?, decided_by=?, decided_at=?, result_json=? WHERE id=?",
            (status, decided_by, _now(), json.dumps(result) if result is not None else None, approval_id),
        )


# ---- async wrappers (sqlite runs in a thread so the event loop isn't blocked) ----
async def init():                         return await asyncio.to_thread(_init)
async def create(requester, requester_email, user_input, intent, summary, plan, fabric):
    return await asyncio.to_thread(_create, requester, requester_email, user_input, intent, summary, plan, fabric)
async def list_pending():                 return await asyncio.to_thread(_list_pending)
async def list_by_requester(username):    return await asyncio.to_thread(_list_by_requester, username)
async def get(approval_id):               return await asyncio.to_thread(_get, approval_id)
async def set_decision(approval_id, status, decided_by, result=None):
    return await asyncio.to_thread(_set_decision, approval_id, status, decided_by, result)
