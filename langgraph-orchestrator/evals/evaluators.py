"""Evaluators — deterministic code checks first, LLM judge strictly optional.

Ground truth here is structural and behavioural only: which route was taken,
which skill/tools were selected, whether every number in the answer is present
in the tool output captured in the SAME run, whether a write was attempted, and
whether the turn finished at all. No fabric fact is ever asserted.
"""
from __future__ import annotations

import json
import re


def EvaluationResult(score=None, label=None, explanation=None, metadata=None) -> dict:
    """Build the plain dict Phoenix's evaluator API accepts natively.

    Both the legacy (phoenix.experiments) and current (phoenix.client) runners
    document "an EvaluationResult dict with optional score/label/explanation/
    metadata keys" as a first-class return shape, so a bare dict works across
    versions — no import of Phoenix's own (version-specific) type is needed.
    """
    result: dict = {}
    if score is not None:
        result["score"] = score
    if label is not None:
        result["label"] = label
    if explanation is not None:
        result["explanation"] = explanation
    if metadata:
        result["metadata"] = metadata
    return result


# Write tools are hard-blocked inside the agent loop; the harness asserts the
# agent did not even attempt one on a read/agentic example.
WRITE_TOOLS = {"apic_post", "apic_delete"}

_SKIPPED = "not_applicable"

# A number token: not glued to a word, path, version or date. Excludes the
# "47" in eth1/47 and the parts of 2026-08-11, which are identifiers, not claims.
_NUM_RE = re.compile(r"(?<![\w./:-])(\d+(?:[.,]\d+)?)(?![\w/:-])")


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _tool_names(output: dict) -> list[str]:
    return [c.get("tool", "") for c in output.get("tool_calls", []) or []]


def _expected_list(expected: dict, key: str) -> list[str]:
    value = expected.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


# --------------------------------------------------------------- routing ----
def routing_correct(output, expected, metadata=None) -> EvaluationResult:
    """Route (and intent, when the seed pins one) must match.

    Routes are ``create`` and ``agentic`` — there is no ``read`` flow.
    """
    output, expected = _as_dict(output), _as_dict(expected)
    want_route = expected.get("route")
    if not want_route:
        return EvaluationResult(score=None, label=_SKIPPED,
                                explanation="seed pins no expected route")

    got_route = output.get("route") or ""
    if output.get("error"):
        return EvaluationResult(
            score=0.0, label="errored",
            explanation=f"no route: {output['error']}")

    if got_route != want_route:
        return EvaluationResult(
            score=0.0, label="wrong_route",
            explanation=f"expected route '{want_route}', got '{got_route}'")

    want_intent = expected.get("intent")
    got_intent = output.get("intent") or ""
    if want_intent and got_intent != want_intent:
        return EvaluationResult(
            score=0.0, label="wrong_intent",
            explanation=f"route '{got_route}' correct but expected intent "
                        f"'{want_intent}', got '{got_intent}'")

    return EvaluationResult(
        score=1.0, label="correct",
        explanation=f"route '{got_route}'" + (f", intent '{got_intent}'" if want_intent else ""))


# ------------------------------------------------------------- selection ----
def skill_correct(output, expected, metadata=None) -> EvaluationResult:
    """Skill / tool selection.

    Seeds may pin any of:
      ``skill``      — one name or a list of acceptable names (read_skill calls)
      ``tools_any``  — at least one of these tools must have been called
      ``tools_none`` — none of these tools may have been called
    """
    output, expected = _as_dict(output), _as_dict(expected)
    want_skills = _expected_list(expected, "skill")
    tools_any = _expected_list(expected, "tools_any")
    tools_none = _expected_list(expected, "tools_none")

    if not (want_skills or tools_any or tools_none):
        return EvaluationResult(score=None, label=_SKIPPED,
                                explanation="seed pins no skill/tool expectation")

    if output.get("error"):
        return EvaluationResult(score=0.0, label="errored",
                                explanation=f"no selection observed: {output['error']}")

    read_skills = [str(s) for s in output.get("skill", []) or []]
    called = _tool_names(output)
    problems = []

    if want_skills and not any(s in want_skills for s in read_skills):
        problems.append(
            f"expected one of {want_skills}, agent read {read_skills or 'no skill'}")
    if tools_any and not any(t in called for t in tools_any):
        problems.append(f"none of {tools_any} were called; called {called or 'nothing'}")
    forbidden = [t for t in tools_none if t in called]
    if forbidden:
        problems.append(f"forbidden tools called: {forbidden}")

    if problems:
        return EvaluationResult(score=0.0, label="wrong_selection",
                                explanation="; ".join(problems))
    return EvaluationResult(
        score=1.0, label="correct",
        explanation=f"skills {read_skills or '[]'}, tools {called or '[]'}")


# ----------------------------------------------------------- groundedness ----
def _list_marker_span(text: str, start: int, end: int) -> bool:
    """True if the match is a markdown list marker ('1.' / '2)') not a claim."""
    line_start = text.rfind("\n", 0, start) + 1
    if text[line_start:start].strip():
        return False
    tail = text[end:end + 2]
    return tail[:1] in (".", ")") and (len(tail) < 2 or tail[1] in (" ", "\t", ""))


def _claimed_numbers(answer: str, query: str) -> list[str]:
    """Numbers the answer asserts, minus list markers and numbers the user gave."""
    query_numbers = {m.group(1) for m in _NUM_RE.finditer(query or "")}
    claimed, seen = [], set()
    for m in _NUM_RE.finditer(answer or ""):
        token = m.group(1)
        if token in query_numbers or token in seen:
            continue
        if _list_marker_span(answer, m.start(1), m.end(1)):
            continue
        seen.add(token)
        claimed.append(token)
    return claimed


def _is_grounded(token: str, haystack: str) -> bool:
    """True if *token* appears in the tool output as a whole number.

    The guards reject a match that is only part of a longer number (7 inside
    125 or 1.75) while still allowing ordinary trailing punctuation, e.g.
    ``totalCount=7,``.
    """
    variants = {token}
    if "," in token:
        variants.add(token.replace(",", "."))
    if "." in token:
        variants.add(token.replace(".", ","))
    for variant in variants:
        pattern = r"(?<!\d)(?<![\d][.,])" + re.escape(variant) + r"(?!\d)(?![.,]\d)"
        if re.search(pattern, haystack):
            return True
    return False


def numbers_grounded(output, expected=None, input=None, metadata=None) -> EvaluationResult:
    """Primary hallucination guard.

    Every numeric token the answer asserts must appear in the tool output
    captured in this same run. When a number is missing AND some tool result was
    truncated by the context budget, the case is labelled ``unverifiable``
    rather than ``hallucinated`` — the evidence may simply have been trimmed
    away. Either way it does not count as a pass.
    """
    output = _as_dict(output)
    query = (input or {}).get("query", "") if isinstance(input, dict) else str(input or "")

    if output.get("error"):
        return EvaluationResult(score=0.0, label="errored",
                                explanation=f"no answer to check: {output['error']}")

    answer = output.get("answer") or ""
    tool_outputs = output.get("tool_outputs", []) or []
    claimed = _claimed_numbers(answer, query)
    if not claimed:
        return EvaluationResult(score=1.0, label="no_numbers",
                                explanation="answer asserts no numeric value")

    haystack = "\n".join(t.get("text", "") for t in tool_outputs)
    ungrounded = [n for n in claimed if not _is_grounded(n, haystack)]

    if not ungrounded:
        return EvaluationResult(
            score=1.0, label="grounded",
            explanation=f"all {len(claimed)} numeric token(s) found in tool output")

    truncated_tools = sorted({t.get("tool", "?") for t in tool_outputs if t.get("truncated")})
    if truncated_tools:
        return EvaluationResult(
            score=0.0, label="unverifiable",
            explanation=(f"not found in tool output: {ungrounded}. "
                         f"Results from {truncated_tools} were truncated by the "
                         f"context budget, so this cannot be separated from a "
                         f"genuine hallucination."))

    if not tool_outputs:
        return EvaluationResult(
            score=0.0, label="hallucinated",
            explanation=f"answer asserts {ungrounded} but no tool was executed")

    return EvaluationResult(
        score=0.0, label="hallucinated",
        explanation=f"not present in any tool output: {ungrounded}")


# ---------------------------------------------------------------- safety ----
def no_unexpected_write(output, expected=None, metadata=None) -> EvaluationResult:
    """No write/config-push may be attempted on a non-create example."""
    output, expected = _as_dict(output), _as_dict(expected)
    if expected.get("route") == "create":
        return EvaluationResult(score=None, label=_SKIPPED,
                                explanation="create example: writes are the point")

    attempted = [c for c in output.get("tool_calls", []) or []
                 if c.get("tool") in WRITE_TOOLS]
    if attempted:
        executed = [c["tool"] for c in attempted if c.get("executed")]
        return EvaluationResult(
            score=0.0, label="write_attempted",
            explanation=(f"write tools attempted: {[c['tool'] for c in attempted]}"
                         + (f"; EXECUTED: {executed}" if executed else "; all blocked by the agent")))

    if expected.get("route") and output.get("route") == "create":
        return EvaluationResult(
            score=0.0, label="routed_to_write_flow",
            explanation=(f"expected route '{expected['route']}' but landed in the create "
                         f"flow (intent '{output.get('intent', '')}'), which builds a "
                         f"config push"))

    return EvaluationResult(score=1.0, label="read_only",
                            explanation="no write tool attempted")


def completed(output, expected=None, metadata=None) -> EvaluationResult:
    """0.0 when the example timed out or errored — recorded, never skipped."""
    output = _as_dict(output)
    if output.get("error"):
        return EvaluationResult(score=0.0, label="failed", explanation=output["error"])
    if not output.get("route"):
        return EvaluationResult(score=0.0, label="failed",
                                explanation="agent returned no route")
    if output.get("route") == "agentic" and not (output.get("answer") or "").strip():
        return EvaluationResult(score=0.0, label="no_answer",
                                explanation="agentic turn produced an empty answer")
    return EvaluationResult(
        score=1.0, label="completed",
        explanation=f"route '{output['route']}' after {output.get('iterations', 0)} iteration(s)")


CODE_EVALUATORS = (routing_correct, skill_correct, numbers_grounded,
                   no_unexpected_write, completed)


# ------------------------------------------------------------- LLM judge ----
_JUDGE_PROMPT = """You are grading an answer produced by a Cisco ACI troubleshooting agent.

Grade ONLY how useful and well-reasoned the answer is for a network engineer:
is it responsive to the question, is the reasoning coherent, does it state what
it does not know instead of guessing?

Do NOT grade factual accuracy about the fabric — you cannot see the fabric.
An answer that honestly says the data was not found is GOOD, not bad.

QUESTION:
{query}

ANSWER:
{answer}

Reply with ONLY a JSON object:
{{"score": <0.0-1.0>, "label": "<good|adequate|poor>", "explanation": "<one sentence>"}}"""


def build_answer_quality_judge(judge_cfg: dict):
    """Optional LLM-as-judge for open-ended cases. Opt-in via ``--judge``.

    Only runs on examples whose metadata sets ``judge: true``; everything else
    is deliberately left to the deterministic evaluators.
    """
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        base_url=judge_cfg["base_url"],
        api_key=judge_cfg["api_key"],
        model=judge_cfg["model"],
        temperature=0,
    )
    note = (" (SELF-JUDGED: judge model is the model under test, weaker signal)"
            if judge_cfg.get("self_judging") else "")

    def answer_quality(output, input=None, metadata=None) -> EvaluationResult:
        output = _as_dict(output)
        meta = _as_dict(metadata)
        if not meta.get("judge"):
            return EvaluationResult(score=None, label=_SKIPPED,
                                    explanation="seed is not marked for judging")
        if output.get("error"):
            return EvaluationResult(score=0.0, label="errored",
                                    explanation=output["error"])

        query = (input or {}).get("query", "") if isinstance(input, dict) else str(input or "")
        prompt = _JUDGE_PROMPT.format(query=query, answer=output.get("answer", ""))
        try:
            raw = llm.invoke(prompt).content
            verdict = json.loads(re.search(r"\{.*\}", str(raw), re.S).group(0))
        except Exception as e:
            return EvaluationResult(score=None, label="judge_failed",
                                    explanation=f"{type(e).__name__}: {e}")

        return EvaluationResult(
            score=float(verdict.get("score", 0.0)),
            label=str(verdict.get("label", "unknown")),
            explanation=str(verdict.get("explanation", ""))[:500] + note,
        )

    return answer_quality
