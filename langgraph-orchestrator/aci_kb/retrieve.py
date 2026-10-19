"""
aci_kb/retrieve.py

Lexical retrieval over the ACI class catalog (mo_classes in aci.db).

Builds an in-memory FTS5 index ONCE on first use by reading the catalog
read-only. No writes to aci.db, no new pip dependencies (FTS5 ships with the
stdlib sqlite3). BM25 ranking over class label + description + a de-camel-cased
class name so plain-English queries ("bridge domain", "bgp neighbor") match
packed class names ("fvBD", "bgpPeer").

Upgrade path: when you have an on-prem embedding endpoint, replace the body of
search_classes() with a vector search. Keep the (query, k) -> list[dict]
signature so nothing downstream changes.
"""
import re
import sqlite3
import threading

from . import store  # only used for store.DB_PATH

_lock = threading.Lock()
_fts = None  # in-memory FTS5 connection, built lazily


def _decamel(name: str) -> str:
    # fvBD -> "fv BD", l1PhysIf -> "l1 Phys If", bgpPeerEntry -> "bgp Peer Entry"
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return s.replace(":", " ")


def _load_catalog():
    """Read (class_name, label, description) once, read-only, from aci.db."""
    src = sqlite3.connect(f"file:{store.DB_PATH}?mode=ro", uri=True,
                          check_same_thread=False)
    try:
        # embed_worthy marks configurable, non-abstract, non-deprecated classes
        # worth surfacing to the agent. Fall back to all rows if absent/empty.
        try:
            rows = src.execute(
                "SELECT class_name, label, description FROM mo_classes "
                "WHERE embed_worthy = 1"
            ).fetchall()
            if not rows:
                raise sqlite3.OperationalError("no embed_worthy rows")
        except sqlite3.OperationalError:
            rows = src.execute(
                "SELECT class_name, label, description FROM mo_classes"
            ).fetchall()
        return rows
    finally:
        src.close()


def _build():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute(
        "CREATE VIRTUAL TABLE cls USING fts5("
        "class_name UNINDEXED, terms, label, description, "
        "tokenize='porter unicode61')"
    )
    with conn:
        for class_name, label, description in _load_catalog():
            terms = f"{class_name} {_decamel(class_name)} {label or ''}"
            conn.execute(
                "INSERT INTO cls (class_name, terms, label, description) "
                "VALUES (?, ?, ?, ?)",
                (class_name, terms, label or "", description or ""),
            )
    return conn


def _get():
    global _fts
    if _fts is None:
        with _lock:
            if _fts is None:
                _fts = _build()
    return _fts


def _match(text: str) -> str:
    """Free text -> a safe OR-of-terms FTS5 MATCH string (no syntax errors)."""
    toks = [t for t in re.findall(r"[A-Za-z0-9]+", text.lower()) if len(t) > 2]
    return " OR ".join(toks) if toks else '"zzzz_no_match"'


def search_classes(query: str, k: int = 5) -> list[dict]:
    """
    Up to k configurable ACI classes most relevant to a free-text query,
    BM25-ranked (best first). Each hit: {class_name, label, description, score}.

    The in-memory corpus is tiny (a few thousand classes), so queries are
    sub-millisecond; we serialize them under a lock because a single sqlite
    connection is not safe for concurrent cross-thread use.
    """
    conn = _get()
    with _lock:
        cur = conn.execute(
            "SELECT class_name, label, description, bm25(cls) AS score "
            "FROM cls WHERE cls MATCH ? ORDER BY score LIMIT ?",
            (_match(query), k),
        )
        rows = cur.fetchall()
    return [
        {"class_name": r[0], "label": r[1], "description": r[2], "score": r[3]}
        for r in rows
    ]
