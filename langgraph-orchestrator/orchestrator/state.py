"""Graph state — shared between create, query, and agentic flows."""
from typing import TypedDict, Optional, Annotated
from operator import add


class GraphState(TypedDict, total=False):
    # ---- Common ----
    user_input: str
    intent: str
    params: dict
    target_class: str
    flow: str

    # ---- Create flow ----
    schema: Optional[dict]
    payload: dict
    dn: str
    parent_dn: Optional[str]
    validation_result: Optional[dict]
    validation_attempts: int

    # ---- Static binding (explicit POST) ----
    post_path: str
    post_body: dict

    # ---- Agentic flow ----
    agent_messages: Annotated[list, add]
    chat_history: list
    agent_iterations: int
    agent_final_answer: str
    # Number of times this turn the model returned JSON-looking output that
    # failed to parse and was nudged to re-emit. Capped so a persistently
    # malformed model can't loop forever. Plain last-write int.
    agent_malformed_retries: int
    # Exact (tool, arguments) signatures already executed this turn. Plain
    # last-write list so think_and_act dedup accumulates across loop iterations
    # (must be declared here — undeclared keys are dropped by LangGraph).
    agent_called: list
    # Consecutive empty/failed tool results this turn; fail-fast when high.
    # Plain last-write int (same pattern as agent_malformed_retries).
    agent_empty_streak: int
    # Compact evidence lines captured from executed tool calls; used to build
    # a deterministic fallback final answer when the loop hits its max step cap.
    agent_observations: list
    # Query families (path + filter, page stripped) already sampled after a
    # large totalCount; further page-only advances are blocked.
    agent_large_query_families: list
    # Specific skills pre-loaded this turn (set by prepare_agent, read by the
    # plan node). Plain last-write field; only prepare_agent writes it.
    selected_skills: list

    # ---- Final ----
    final_result: Optional[dict]
    interpretation: str

    # ---- Cross-cutting ----
    errors: Annotated[list, add]
    trail: Annotated[list, add]
