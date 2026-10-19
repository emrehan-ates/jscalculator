"""
agentic_aci_kb.py

Agentic-flow integration for aci_kb. Two pieces:

  1. inject_class_hints
     A zero-LLM-call graph node. Given the user's question, retrieve the most
     relevant ACI classes (BM25 over the in-process aci_kb catalog) and append
     them to the agent context as a SystemMessage hint BEFORE its first LLM
     call. Pure Python retrieval — no extra model round-trip. This steers the
     agent to correct class names and query-target-filter targets so it doesn't
     guess (the #1 cause of bad APIC reads).

  2. aci_lookup_class
     A single fallback tool the agent calls ONLY when it needs full schema for a
     class the hints didn't cover. validate_payload / build_dn are deliberately
     NOT exposed here — they stay in the deterministic create flow.

aci_kb is imported in-process (no MCP hop needed for a local SQLite lookup).
Adapted to the orchestrator's custom agentic protocol: state carries
`agent_messages` (append-reducer) and `user_input`; tools are dispatched by
name and invoked via `.ainvoke(arguments)`.
"""
import json
import logging

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool

from aci_kb import get_mo_spec
from aci_kb.retrieve import search_classes

logger = logging.getLogger("aci_kb.retrieval")

# Number of candidate classes injected as hints per user turn.
HINT_K = 6


def _format_hints(hits: list[dict]) -> str:
    lines = []
    for h in hits:
        desc = (h.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 200:
            desc = desc[:200].rstrip() + "…"
        label = h.get("label") or ""
        head = h["class_name"] + (f" ({label})" if label else "")
        lines.append(f"- {head}" + (f": {desc}" if desc else ""))
    return (
        "ACI object-model classes likely relevant to this request. Use these "
        "exact class names when querying APIC; call aci_lookup_class for full "
        "schema, DN format, and filter targets:\n" + "\n".join(lines)
    )


def build_class_hint_message(user_text: str, k: int = HINT_K):
    """Return a (SystemMessage, hits) pair for the top-k relevant classes.

    Returns (None, []) when the query yields no matches.
    """
    hits = search_classes(user_text, k=k)
    if not hits:
        return None, []
    return SystemMessage(content=_format_hints(hits)), hits


def _last_user_text(messages) -> str:
    """The content of the LAST HumanMessage (not the whole history)."""
    for m in reversed(messages or []):
        if isinstance(m, HumanMessage):
            return m.content if isinstance(m.content, str) else str(m.content)
    return ""


def inject_class_hints(state: dict) -> dict:
    """Graph node: append one SystemMessage of relevant ACI classes to the
    agent context, built from the LAST user message only. Runs exactly once per
    user turn (between prepare_agent and the think/act loop); the loop never
    returns here, so hints are never re-injected. No LLM call.

    Always runs. Under progressive disclosure (see nodes_agentic._load_relevant_skills)
    no skill's full content is force-loaded before this point — only a
    name+summary list and a non-binding keyword hint are in context — so these
    aci_kb class hints stay useful even when a skill looks relevant; they are
    a small, orthogonal, always-correct (BM25 retrieval, not guesswork) signal
    that helps regardless of which skill the agent ends up reading.
    """
    user_text = state.get("user_input") or _last_user_text(state.get("agent_messages"))
    if not user_text:
        return {"trail": ["inject_class_hints: no user text; skipped"]}

    msg, hits = build_class_hint_message(user_text)

    # Task 6 — retrieval-quality logging for the future embedding decision.
    class_names = [h["class_name"] for h in hits]
    logger.info("aci_kb retrieval | query=%r | top%d=%s",
                user_text, len(class_names), class_names)

    if msg is None:
        return {"trail": [f"inject_class_hints: no class matches for {user_text!r}"]}

    return {
        "agent_messages": [msg],
        "trail": [f"inject_class_hints: injected {len(hits)} class hint(s): "
                  + ", ".join(class_names)],
    }


# --- The single fallback tool ------------------------------------------------
@tool
def aci_lookup_class(query: str) -> str:
    """Resolve an ACI object-model class. Pass an exact class name
    ('fvBD', 'bgpPeer', 'l1PhysIf') to get its full schema, or a plain-English
    description ('bridge domain', 'bgp neighbor state', 'physical interface
    status') to find the most likely classes. Returns class name(s), DN format,
    configurable properties, valid enum values, and query-target-filter targets.
    Use this before querying APIC whenever you are unsure which class or filter
    applies."""
    spec = get_mo_spec(query)
    if spec and not spec.get("error"):
        return json.dumps(spec, indent=2, ensure_ascii=False)
    hits = search_classes(query, k=5)
    if not hits:
        return f"No ACI class matched '{query}'. Try a different term."
    out = ["Closest classes (call again with an exact class_name for full schema):"]
    for h in hits:
        desc = (h.get("description") or "")[:160]
        out.append(f"- {h['class_name']} ({h.get('label') or ''}): {desc}")
    return "\n".join(out)
