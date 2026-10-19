"""
Agentic flow graph.

Flow: login → prepare_agent → think_and_act → [loop until final_answer or max] → END
"""
from langgraph.graph import StateGraph, END

from .state import GraphState
from .nodes_agentic import prepare_agent, think_and_act, agent_login, make_plan
from .agentic_aci_kb import inject_class_hints


def agent_router(state: dict) -> str:
    """After each think_and_act, decide: loop or finish."""
    if state.get("agent_final_answer"):
        return "done"
    return "continue"


def build_agentic_graph():
    b = StateGraph(GraphState)
    b.add_node("login", agent_login)
    b.add_node("prepare_agent", prepare_agent)
    b.add_node("class_hints", inject_class_hints)
    b.add_node("plan", make_plan)
    b.add_node("think_and_act", think_and_act)

    b.set_entry_point("login")
    b.add_edge("login", "prepare_agent")
    # Once-per-turn preamble: prepare_agent -> class_hints -> plan -> think_and_act.
    #   class_hints (zero LLM calls) injects relevant ACI class names.
    #   plan        (exactly ONE LLM call) injects a short investigation plan.
    # The think_and_act loop returns to itself (see below), NEVER to class_hints
    # or plan, so both are applied exactly once per user turn.
    b.add_edge("prepare_agent", "class_hints")
    #class hints are only injected when generic skill (fallback skill) is used
    #however this is not implemented via a conditional edge, because the graph is compiled once and reused for all turns. The conditional logic is inside the inject_class_hints node.
    b.add_edge("class_hints", "plan")
    b.add_edge("plan", "think_and_act")
    b.add_conditional_edges("think_and_act", agent_router, {
        "continue": "think_and_act",
        "done": END,
    })
    return b.compile()
