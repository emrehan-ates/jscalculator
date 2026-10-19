import unittest
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from orchestrator.chat_memory import (
    append_chat_turn,
    history_to_messages,
    normalize_chat_history,
    reconstruct_chat_history,
)
from orchestrator.nodes_common import (
    _current_request_authorizes_write,
    _intent_messages,
    recognize_intent,
)


class ChatMemoryTests(unittest.TestCase):
    def test_normalize_keeps_only_newest_complete_turns(self):
        history = [
            {"user": f"user {index}", "assistant": f"answer {index}"}
            for index in range(12)
        ]
        history.extend([
            {"user": "missing answer", "assistant": ""},
            {"user": "", "assistant": "missing user"},
        ])

        result = normalize_chat_history(history)

        self.assertEqual(10, len(result))
        self.assertEqual("user 2", result[0]["user"])
        self.assertEqual("answer 11", result[-1]["assistant"])

    def test_append_trims_and_normalizes_text(self):
        history = [
            {"user": f"user {index}", "assistant": f"answer {index}"}
            for index in range(10)
        ]

        result = append_chat_turn(history, "  newest question  ", " newest answer ")

        self.assertEqual(10, len(result))
        self.assertEqual("user 1", result[0]["user"])
        self.assertEqual(
            {"user": "newest question", "assistant": "newest answer"},
            result[-1],
        )

    def test_history_to_messages_preserves_role_order(self):
        messages = history_to_messages([
            {"user": "first question", "assistant": "first answer"},
            {"user": "second question", "assistant": "second answer"},
        ])

        self.assertEqual(4, len(messages))
        self.assertIsInstance(messages[0], HumanMessage)
        self.assertEqual("first question", messages[0].content)
        self.assertIsInstance(messages[1], AIMessage)
        self.assertEqual("first answer", messages[1].content)
        self.assertEqual("second question", messages[2].content)
        self.assertEqual("second answer", messages[3].content)

    def test_reconstruct_filters_commands_tools_and_incomplete_turns(self):
        steps = [
            {
                "type": "assistant_message",
                "output": "ACI Agent ready.",
                "start": "2026-08-04T10:00:00Z",
            },
            {
                "type": "user_message",
                "output": "show EPG PROD_WEB",
                "start": "2026-08-04T10:01:00Z",
            },
            {
                "type": "tool",
                "output": "internal trace",
                "parentId": "assistant-1",
                "start": "2026-08-04T10:01:01Z",
            },
            {
                "type": "assistant_message",
                "output": "intermediate answer",
                "start": "2026-08-04T10:01:02Z",
            },
            {
                "type": "assistant_message",
                "output": "PROD_WEB is in tenant PROD.",
                "start": "2026-08-04T10:01:03Z",
            },
            {
                "type": "user_message",
                "output": "my requests",
                "start": "2026-08-04T10:02:00Z",
            },
            {
                "type": "assistant_message",
                "output": "You have no requests yet.",
                "start": "2026-08-04T10:02:01Z",
            },
            {
                "type": "user_message",
                "output": "what ports is it on?",
                "start": "2026-08-04T10:03:00Z",
            },
        ]

        result = reconstruct_chat_history(
            list(reversed(steps)),
            excluded_user_messages={"my requests"},
        )

        self.assertEqual([
            {
                "user": "show EPG PROD_WEB",
                "assistant": "PROD_WEB is in tenant PROD.",
            }
        ], result)

    def test_reconstruct_does_not_cross_new_thread_boundaries(self):
        old_thread = [
            {"type": "user_message", "output": "old question"},
            {"type": "assistant_message", "output": "old answer"},
        ]

        self.assertEqual([], reconstruct_chat_history([]))
        self.assertEqual(
            [{"user": "old question", "assistant": "old answer"}],
            reconstruct_chat_history(old_thread),
        )

    def test_intent_messages_end_with_current_request(self):
        history = [
            {"user": f"question {index}", "assistant": f"answer {index}"}
            for index in range(12)
        ]

        messages = _intent_messages({
            "user_input": "bind that EPG to leaf 102 port eth1/20",
            "chat_history": history,
        })

        self.assertIsInstance(messages[-1], HumanMessage)
        self.assertEqual(
            "bind that EPG to leaf 102 port eth1/20",
            messages[-1].content,
        )
        historical_human_messages = [
            message for message in messages[:-1]
            if isinstance(message, HumanMessage)
        ]
        self.assertEqual(10, len(historical_human_messages))
        self.assertEqual("question 2", historical_human_messages[0].content)

    def test_only_explicit_current_write_language_authorizes_write(self):
        self.assertTrue(_current_request_authorizes_write(
            "bind that EPG to leaf 102 port eth1/20"
        ))
        self.assertTrue(_current_request_authorizes_write("create it in tenant PROD"))
        self.assertFalse(_current_request_authorizes_write("yes"))
        self.assertFalse(_current_request_authorizes_write("okay, do that"))
        self.assertFalse(_current_request_authorizes_write("evet yap"))
        self.assertFalse(_current_request_authorizes_write("tamam, devam et"))
        self.assertFalse(_current_request_authorizes_write("onaylıyorum"))
        self.assertFalse(_current_request_authorizes_write(""))

    def test_non_english_writes_with_history_are_not_demoted(self):
        # Regression: a Turkish create request with non-empty chat history used
        # to be silently rerouted to the read-only agentic flow because the old
        # guard required an English write verb.
        self.assertTrue(_current_request_authorizes_write(
            "EPG'yi eth1/20'ye bağla"
        ))
        self.assertTrue(_current_request_authorizes_write("tenant'a BD ekle"))
        self.assertTrue(_current_request_authorizes_write("102 eth1/20"))


class _CreateClassifier:
    async def ainvoke(self, messages):
        return SimpleNamespace(content=(
            '{"intent": "bind_epg", "flow": "create", '
            '"params": {"epgs": ["PROD_WEB"]}, '
            '"target_class": "fvRsPathAtt"}'
        ))


class IntentMemoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.history = [{
            "user": "bind PROD_WEB to leaf 102 port eth1/20",
            "assistant": "The binding plan was cancelled.",
        }]

    @patch("orchestrator.nodes_common.get_llm", return_value=_CreateClassifier())
    async def test_vague_current_turn_cannot_inherit_write_authorization(self, _):
        result = await recognize_intent({
            "user_input": "yes, do that",
            "chat_history": self.history,
        })

        self.assertEqual("agentic", result["flow"])
        self.assertEqual("agentic", result["intent"])

    @patch("orchestrator.nodes_common.get_llm", return_value=_CreateClassifier())
    async def test_explicit_current_write_can_use_history(self, _):
        result = await recognize_intent({
            "user_input": "bind that EPG to leaf 103 port eth1/21",
            "chat_history": self.history,
        })

        self.assertEqual("create", result["flow"])
        self.assertEqual("bind_epg", result["intent"])


if __name__ == "__main__":
    unittest.main()