"""
Read-only access to aci.db. Other modules in aci_kb/ go through this.
"""
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

# Path to the DB file. Override with env var ACI_DB_PATH if your deployment
# layout differs.
import os
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "aci.db"
DB_PATH = Path(os.environ.get("ACI_DB_PATH", _DEFAULT_DB))

# Use a thread-local connection so the MCP server can be multi-threaded
# without sqlite3 complaining about cross-thread reuse.
_local = threading.local()


def _conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn"):
        if not DB_PATH.exists():
            raise FileNotFoundError(
                f"aci.db not found at {DB_PATH}. Set ACI_DB_PATH or place the "
                f"file at the project root."
            )
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        _local.conn = c
    return _local.conn


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


# ---------- class lookups ----------

def get_class(class_name: str) -> Optional[dict]:
    """
    Fetch a single class row by class_name (e.g. 'fvBD').
    Returns None if not found. Performs a case-sensitive match first;
    if that fails, falls back to a case-insensitive match (handy because
    LLMs sometimes lowercase class names).
    """
    cur = _conn().cursor()
    row = cur.execute(
        "SELECT * FROM mo_classes WHERE class_name = ?", (class_name,)
    ).fetchone()
    if row is None:
        row = cur.execute(
            "SELECT * FROM mo_classes WHERE class_name = ? COLLATE NOCASE",
            (class_name,),
        ).fetchone()
    return _row_to_dict(row)


def get_class_by_colon(colon_name: str) -> Optional[dict]:
    """Fetch by colon-form name like 'fv:BD'."""
    cur = _conn().cursor()
    row = cur.execute(
        "SELECT * FROM mo_classes WHERE class_colon_name = ?", (colon_name,)
    ).fetchone()
    return _row_to_dict(row)


def resolve_class_name(name: str) -> Optional[str]:
    """
    Take any of: 'fvBD', 'fv:BD', 'FvBd' and return the canonical 'fvBD',
    or None if no such class.
    """
    if ":" in name:
        row = get_class_by_colon(name)
        return row["class_name"] if row else None
    row = get_class(name)
    return row["class_name"] if row else None


# ---------- properties ----------

def get_properties(class_name: str) -> list[dict]:
    """All configurable properties for the class, naming first then mandatory."""
    cur = _conn().cursor()
    rows = cur.execute(
        """
        SELECT prop_name, label, description, base_type, ui_type,
        default_value, mandatory, create_only, read_only, is_naming,
        valid_values_json, validators_json
        FROM mo_properties
        WHERE class_name = ?
        ORDER BY is_naming DESC, mandatory DESC, prop_name
        """,
        (class_name,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["valid_values"] = json.loads(d.pop("valid_values_json") or "[]")
        d["validators"] = json.loads(d.pop("validators_json") or "[]")
        out.append(d)
    return out


def get_property(class_name: str, prop_name: str) -> Optional[dict]:
    cur = _conn().cursor()
    # Property names in ACI are case-sensitive in the API, but try CI fallback
    # since LLMs may normalize casing.
    row = cur.execute(
        """
        SELECT prop_name, label, description, base_type, ui_type,
        default_value, mandatory, create_only, read_only, is_naming,
        valid_values_json, validators_json
        FROM mo_properties WHERE class_name = ? AND prop_name = ?
        """,
        (class_name, prop_name),
    ).fetchone()
    if row is None:
        row = cur.execute(
            """
            SELECT prop_name, label, description, base_type, ui_type,
            default_value, mandatory, create_only, read_only, is_naming,
            valid_values_json, validators_json
            FROM mo_properties
            WHERE class_name = ? AND prop_name = ? COLLATE NOCASE
            """,
            (class_name, prop_name),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["valid_values"] = json.loads(d.pop("valid_values_json") or "[]")
    d["validators"] = json.loads(d.pop("validators_json") or "[]")
    return d


# ---------- relations ----------

def get_parents(class_name: str) -> list[str]:
    """Parents in colon form, since that's how containedBy is stored."""
    cur = _conn().cursor()
    rows = cur.execute(
        """
        SELECT child_class FROM mo_relations
        WHERE parent_class = ? AND relation_type = 'contained_by'
        ORDER BY child_class
        """,
        (class_name,),
    ).fetchall()
    return [r["child_class"] for r in rows]


def get_children(class_name: str, configurable_only: bool = True) -> list[str]:
    """
    Children in colon form. configurable_only filters out stats/fault/health
    children that nobody POSTs explicitly.
    """
    cur = _conn().cursor()
    if configurable_only:
        rows = cur.execute(
            """
            SELECT r.child_class
            FROM mo_relations r
            JOIN mo_classes c ON c.class_colon_name = r.child_class
            WHERE r.parent_class = ?
            AND r.relation_type = 'contains'
            AND c.embed_worthy = 1
            ORDER BY r.child_class
            """,
            (class_name,),
        ).fetchall()
    else:
        rows = cur.execute(
            """
            SELECT child_class FROM mo_relations
            WHERE parent_class = ? AND relation_type = 'contains'
            ORDER BY child_class
            """,
            (class_name,),
        ).fetchall()
    return [r["child_class"] for r in rows]


def get_relations_to(class_name: str) -> list[str]:
    """Rs* relationship MOs this class owns (these are POSTed as children)."""
    cur = _conn().cursor()
    rows = cur.execute(
        """
        SELECT child_class FROM mo_relations
        WHERE parent_class = ? AND relation_type = 'relates_to'
        ORDER BY child_class
        """,
        (class_name,),
    ).fetchall()
    return [r["child_class"] for r in rows]


def get_dn_formats(class_name: str) -> list[str]:
    cur = _conn().cursor()
    rows = cur.execute(
        "SELECT dn_template FROM mo_dn_formats WHERE class_name = ?",
        (class_name,),
    ).fetchall()
    return [r["dn_template"] for r in rows]


def get_identified_by(class_name: str) -> list[str]:
    cur = _conn().cursor()
    row = cur.execute(
        "SELECT identified_by_json FROM mo_classes WHERE class_name = ?",
        (class_name,),
    ).fetchone()
    if not row:
        return []
    return json.loads(row["identified_by_json"] or "[]")
