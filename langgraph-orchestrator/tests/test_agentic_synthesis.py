"""Fail-fast / step-cap answers are synthesized from all evidence gathered."""
import asyncio
import json
import unittest
from unittest import mock

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from orchestrator import nodes_agentic


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return AIMessage(content=self.replies.pop(0))


class FakeTool:
    def __init__(self, result):
        self.result = result

    async def ainvoke(self, args):
        return self.result


def tool_call(flt):
    return json.dumps({"action": "tool_call", "tool": "apic_get", "arguments": {
        "path": "/api/node/class/infraRsAttEntP.json",
        "params": {"query-target-filter": flt}}})


class SynthesisTests(unittest.TestCase):
    def run_step(self, llm, state, tool_result):
        tools = {"apic_get": FakeTool(tool_result)}
        with mock.patch.object(nodes_agentic, "get_llm", return_value=llm), \
             mock.patch.object(nodes_agentic, "get_all_tools", return_value=tools), \
             mock.patch.object(nodes_agentic, "unwrap_mcp_result", side_effect=lambda r: r):
            return asyncio.run(nodes_agentic.think_and_act(state))

    def test_fail_fast_synthesizes_from_previous_evidence(self):
        final = json.dumps({"action": "final_answer",
                            "answer": "PG = MSY_X; AEP lookup returned nothing."})
        llm = FakeLLM([tool_call("eq(a,\"1\")"), final])
        state = {
            "agent_messages": [SystemMessage(content="sys"),
                               HumanMessage(content="Tool result: PG MSY_X")],
            "agent_iterations": 3, "agent_called": [], "agent_observations": [],
            "agent_empty_streak": nodes_agentic.FAIL_FAST_EMPTY - 1,
            "agent_had_data": False,
        }
        out = self.run_step(llm, state, {"totalCount": "0", "imdata": []})

        self.assertEqual(out["agent_final_answer"], "PG = MSY_X; AEP lookup returned nothing.")
        synth_input = llm.calls[1]
        self.assertTrue(any("PG MSY_X" in str(m.content) for m in synth_input))
        self.assertIn("STOP", synth_input[-1].content)

    def test_streak_limit_is_longer_once_data_was_found(self):
        llm = FakeLLM([tool_call("eq(a,\"2\")")])
        state = {
            "agent_messages": [SystemMessage(content="sys")],
            "agent_iterations": 3, "agent_called": [], "agent_observations": [],
            "agent_empty_streak": nodes_agentic.FAIL_FAST_EMPTY - 1,
            "agent_had_data": True,
        }
        out = self.run_step(llm, state, {"totalCount": "0", "imdata": []})

        self.assertNotIn("agent_final_answer", out)
        self.assertEqual(out["agent_empty_streak"], nodes_agentic.FAIL_FAST_EMPTY)

    def test_synthesis_falls_back_when_llm_fails(self):
        class Broken:
            async def ainvoke(self, messages):
                raise RuntimeError("down")

        answer = asyncio.run(nodes_agentic._synthesize_final_answer(
            Broken(), [], "test", lambda: "fallback"))
        self.assertEqual(answer, "fallback")

    def test_step_cap_synthesizes(self):
        llm = FakeLLM(["Bulduklarım: EPG X, 2 binding."])
        state = {"agent_messages": [SystemMessage(content="sys")],
                 "agent_iterations": nodes_agentic.MAX_AGENT_ITERATIONS - 1}
        out = self.run_step(llm, state, None)

        self.assertEqual(out["agent_final_answer"], "Bulduklarım: EPG X, 2 binding.")


if __name__ == "__main__":
    unittest.main()
