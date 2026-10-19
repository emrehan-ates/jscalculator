"""Task — wraps the real agent entry point for the experiment runner.

Entry point is ``orchestrator.supervisor.prepare``, never ``run``: for the
agentic flow ``prepare`` is a complete turn (identical to ``run``), and for the
create flow it stops BEFORE the APIC POST. A seed that misroutes to create can
therefore never write to a real fabric.

``prepare`` returns the raw LangGraph state. Route and intent are fields on it,
but the skill the agent chose, the tools it called and their outputs only exist
inside the ``agent_messages`` transcript, so they are parsed back out here.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading

# Markers _format_tool_result writes when it drops objects/characters from a
# tool result. A number can legitimately be missing from a truncated capture,
# so groundedness reports "unverifiable" instead of "hallucinated" in that case.
_TRUNCATION_MARKERS = (
    "NOTE: result too large for context",
    "LARGE RESULT: do NOT fetch remaining pages",
    "(truncated; structure may be incomplete)",
    "... (truncated)",
)

_TOOL_RESULT_RE = re.compile(r"Tool result for ([A-Za-z0-9_.\-]+):")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _parse_transcript(messages) -> dict:
    """Recover tool calls, tool outputs and skill reads from agent_messages.

    Pairing rule follows think_and_act: every executed call appends
    AIMessage(action json) then HumanMessage("Tool result for <tool>: ...").
    A call whose follow-up is an error/BLOCKED/ALREADY TRIED message is kept
    with ``executed=False`` — an attempted write still matters for safety.
    """
    from orchestrator.nodes_common import extract_json_from_llm

    calls: list[dict] = []
    outputs: list[dict] = []
    skills: list[str] = []
    pending: dict | None = None

    for msg in messages or []:
        role = getattr(msg, "type", "") or ""
        content = _text(getattr(msg, "content", ""))

        if role == "ai":
            try:
                action = extract_json_from_llm(content)
            except Exception:
                action = None
            if isinstance(action, dict) and action.get("action") == "tool_call":
                call = {
                    "tool": action.get("tool", "") or "",
                    "arguments": action.get("arguments", {}),
                    "executed": False,
                }
                calls.append(call)
                pending = call
                if call["tool"] == "read_skill" and isinstance(call["arguments"], dict):
                    name = call["arguments"].get("skill_name") or call["arguments"].get("name")
                    if name:
                        skills.append(str(name).replace(".md", "").strip())
            continue

        if role == "human" and "Tool result for" in content:
            m = _TOOL_RESULT_RE.search(content)
            tool = m.group(1) if m else (pending["tool"] if pending else "unknown")
            if pending is not None:
                pending["executed"] = True
            outputs.append({
                "tool": tool,
                "text": content,
                "truncated": any(mark in content for mark in _TRUNCATION_MARKERS),
            })
            pending = None

    return {"tool_calls": calls, "tool_outputs": outputs, "skills": skills}


def _state_to_output(state: dict) -> dict:
    parsed = _parse_transcript(state.get("agent_messages"))
    answer = state.get("agent_final_answer") or state.get("interpretation") or ""
    return {
        "answer": answer,
        "route": state.get("flow", ""),
        "intent": state.get("intent", ""),
        "skill": parsed["skills"],
        "tool_calls": parsed["tool_calls"],
        "tool_outputs": parsed["tool_outputs"],
        "iterations": state.get("agent_iterations", 0),
        "trail": list(state.get("trail", []) or []),
        "agent_errors": [str(e) for e in (state.get("errors") or [])],
        "error": None,
    }


def _failure_output(error: str, route: str = "") -> dict:
    return {
        "answer": "",
        "route": route,
        "intent": "",
        "skill": [],
        "tool_calls": [],
        "tool_outputs": [],
        "iterations": 0,
        "trail": [],
        "agent_errors": [],
        "error": error,
    }


class AgentRunner:
    """Runs agent turns on one dedicated event loop owned by one thread.

    The persistent MCP runtime keeps its stdio sessions inside a single owner
    task, and the tool registry lives in a task-local ContextVar. Both break if
    turns are dispatched across different event loops, so the loop is started
    once here and every call is submitted to it with run_coroutine_threadsafe.
    That also makes ``run`` safe to call from whatever thread the experiment
    runner uses.
    """

    def __init__(self, fabric: dict, team: str = "", profile: str = "",
                 timeout_s: float = 300.0):
        self.fabric = fabric
        self.team = team
        self.profile = profile
        self.timeout_s = timeout_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="eval-agent-loop", daemon=True)
        self._thread.start()
        self._submit(self._warm(), timeout=self.timeout_s)

    def close(self):
        if self._loop is None:
            return
        try:
            self._submit(self._aclose(), timeout=60)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=30)
            self._loop.close()
            self._loop = None

    def run(self, user_input: str, chat_history=None) -> dict:
        """One full agent turn. Never raises: failures come back as ``error``."""
        try:
            state = self._submit(
                self._turn(user_input, chat_history),
                timeout=self.timeout_s + 15,
            )
        except TimeoutError:
            return _failure_output(f"timeout after {self.timeout_s}s")
        except Exception as e:
            return _failure_output(f"{type(e).__name__}: {e}")

        if isinstance(state, dict) and "__error__" in state:
            return _failure_output(state["__error__"])
        if not isinstance(state, dict):
            return _failure_output(f"agent returned {type(state).__name__}, expected dict")
        return _state_to_output(state)

    # ---- internals -------------------------------------------------------
    def _submit(self, coro, timeout: float):
        import concurrent.futures

        assert self._loop is not None, "AgentRunner.start() was not called"
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as e:
            future.cancel()
            raise TimeoutError(str(e)) from e

    async def _warm(self):
        from orchestrator.mcp_runtime import get_runtime_manager

        await get_runtime_manager().warm(self.fabric)

    async def _aclose(self):
        from orchestrator.mcp_runtime import get_runtime_manager

        await get_runtime_manager().aclose()

    async def _turn(self, user_input: str, chat_history):
        from orchestrator.supervisor import prepare

        try:
            return await asyncio.wait_for(
                prepare(user_input, self.fabric, team=self.team,
                        profile=self.profile, chat_history=chat_history),
                timeout=self.timeout_s,
            )
        except asyncio.TimeoutError:
            return {"__error__": f"timeout after {self.timeout_s}s"}
        except Exception as e:
            return {"__error__": f"{type(e).__name__}: {e}"}


def build_task(runner: AgentRunner):
    """Return the callable the experiment runner invokes per dataset example."""

    def agent_task(input: dict) -> dict:
        query = input.get("query", "") if isinstance(input, dict) else str(input)
        if not query:
            return _failure_output("dataset example has no 'query' input")
        return runner.run(query)

    return agent_task
