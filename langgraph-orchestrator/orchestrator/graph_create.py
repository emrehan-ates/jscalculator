"""
Create flow graphs (Solution A — no interrupt).

- build_create_graph(): full flow (CLI use)
- build_prepare_graph(): stops after build_dn (before POST) — for approval
- build_execute_graph(): login + POST only — after approval
"""
from langgraph.graph import StateGraph, END

from .state import GraphState
from .nodes_common import recognize_intent, login
from .nodes_create import (
    resolve_parent, get_schema, build_payload, validate_payload,
    fix_payload, build_dn, post_to_apic,
)


MAX_VALIDATION_ATTEMPTS = 3


def validation_router(state: dict) -> str:
    result = state.get("validation_result", {})
    attempts = state.get("validation_attempts", 0)
    if result.get("valid"):
        return "valid"
    if attempts >= MAX_VALIDATION_ATTEMPTS:
        return "give_up"
    return "fix"


def build_create_graph():
    """Full flow including POST — used by CLI (no human gate)."""
    b = StateGraph(GraphState)
    b.add_node("recognize_intent", recognize_intent)
    b.add_node("resolve_parent", resolve_parent)
    b.add_node("get_schema", get_schema)
    b.add_node("build_payload", build_payload)
    b.add_node("validate_payload", validate_payload)
    b.add_node("fix_payload", fix_payload)
    b.add_node("build_dn", build_dn)
    b.add_node("login", login)
    b.add_node("post_to_apic", post_to_apic)

    b.set_entry_point("recognize_intent")
    b.add_edge("recognize_intent", "resolve_parent")
    b.add_edge("resolve_parent", "get_schema")
    b.add_edge("get_schema", "build_payload")
    b.add_edge("build_payload", "validate_payload")
    b.add_conditional_edges("validate_payload", validation_router,
        {"valid": "build_dn", "fix": "fix_payload", "give_up": END})
    b.add_edge("fix_payload", "validate_payload")
    b.add_edge("build_dn", "login")
    b.add_edge("login", "post_to_apic")
    b.add_edge("post_to_apic", END)
    return b.compile()


def build_prepare_graph():
    """Stops after build_dn — no login/POST. For Chainlit approval gate."""
    b = StateGraph(GraphState)
    b.add_node("recognize_intent", recognize_intent)
    b.add_node("resolve_parent", resolve_parent)
    b.add_node("get_schema", get_schema)
    b.add_node("build_payload", build_payload)
    b.add_node("validate_payload", validate_payload)
    b.add_node("fix_payload", fix_payload)
    b.add_node("build_dn", build_dn)

    b.set_entry_point("recognize_intent")
    b.add_edge("recognize_intent", "resolve_parent")
    b.add_edge("resolve_parent", "get_schema")
    b.add_edge("get_schema", "build_payload")
    b.add_edge("build_payload", "validate_payload")
    b.add_conditional_edges("validate_payload", validation_router,
        {"valid": "build_dn", "fix": "fix_payload", "give_up": END})
    b.add_edge("fix_payload", "validate_payload")
    b.add_edge("build_dn", END)
    return b.compile()


def build_prepare_from_intent_graph():
    """Like build_prepare_graph, but starts from an already-classified task
    (intent/params/target_class set) — used for one task of a multi-task
    request, where re-classifying the whole message would be wrong."""
    b = StateGraph(GraphState)
    b.add_node("resolve_parent", resolve_parent)
    b.add_node("get_schema", get_schema)
    b.add_node("build_payload", build_payload)
    b.add_node("validate_payload", validate_payload)
    b.add_node("fix_payload", fix_payload)
    b.add_node("build_dn", build_dn)

    b.set_entry_point("resolve_parent")
    b.add_edge("resolve_parent", "get_schema")
    b.add_edge("get_schema", "build_payload")
    b.add_edge("build_payload", "validate_payload")
    b.add_conditional_edges("validate_payload", validation_router,
        {"valid": "build_dn", "fix": "fix_payload", "give_up": END})
    b.add_edge("fix_payload", "validate_payload")
    b.add_edge("build_dn", END)
    return b.compile()


def build_execute_graph():
    """login + POST only — after approval."""
    b = StateGraph(GraphState)
    b.add_node("login", login)
    b.add_node("post_to_apic", post_to_apic)
    b.set_entry_point("login")
    b.add_edge("login", "post_to_apic")
    b.add_edge("post_to_apic", END)
    return b.compile()
