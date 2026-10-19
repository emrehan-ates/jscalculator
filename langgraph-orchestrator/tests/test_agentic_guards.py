"""Unit tests for large-result formatting and page-chase family signatures."""
import unittest

from orchestrator.nodes_agentic import (
    LARGE_RESULT_THRESHOLD,
    _format_tool_result,
    _query_family_signature,
)
from orchestrator.state import GraphState


class AgenticGuardTests(unittest.TestCase):
    def test_format_tool_result_includes_large_result_guidance(self):
        imdata = [
            {"faultRecord": {"attributes": {"code": f"F{i}", "descr": "x" * 40}}}
            for i in range(30)
        ]
        result = {"totalCount": "52000", "imdata": imdata}

        text = _format_tool_result(result)

        self.assertIn("totalCount=52000", text)
        self.assertIn("LARGE RESULT", text)
        self.assertIn("do NOT fetch remaining pages", text)

    def test_format_tool_result_skips_guidance_for_small_sets(self):
        result = {
            "totalCount": "3",
            "imdata": [{"fvBD": {"attributes": {"name": "Web-BD"}}}],
        }

        text = _format_tool_result(result)

        self.assertIn("totalCount=3", text)
        self.assertNotIn("LARGE RESULT", text)

    def test_format_tool_result_truncated_body_still_has_guidance(self):
        # Force whole-object truncation with a few huge objects.
        huge = "y" * 9000
        imdata = [
            {"faultRecord": {"attributes": {"code": f"F{i}", "descr": huge}}}
            for i in range(8)
        ]
        result = {
            "totalCount": str(LARGE_RESULT_THRESHOLD + 1),
            "imdata": imdata,
        }

        text = _format_tool_result(result)

        self.assertIn("NOTE: result too large for context", text)
        self.assertIn("LARGE RESULT", text)
        self.assertIn(f"totalCount={LARGE_RESULT_THRESHOLD + 1}", text)

    def test_query_family_signature_strips_page_keys(self):
        base = {
            "path": "/api/node/class/faultRecord.json",
            "params": {
                "query-target-filter": 'gt(faultRecord.created,"2026-01-01T00:00:00")',
                "order-by": "faultRecord.created|desc",
                "page-size": "500",
                "page": "0",
            },
        }
        next_page = {
            "path": base["path"],
            "params": {
                **base["params"],
                "page": "1",
                "page-size": "500",
            },
        }
        different_filter = {
            "path": base["path"],
            "params": {
                "query-target-filter": 'eq(faultRecord.severity,"critical")',
                "page": "0",
            },
        }

        fam0 = _query_family_signature("apic_get", base)
        fam1 = _query_family_signature("apic_get", next_page)
        fam_other = _query_family_signature("apic_get", different_filter)

        self.assertIsNotNone(fam0)
        self.assertEqual(fam0, fam1)
        self.assertNotEqual(fam0, fam_other)
        self.assertIsNone(_query_family_signature("read_skill", {"skill_name": "x"}))

    def test_graph_state_declares_loop_guard_fields(self):
        annotations = GraphState.__annotations__
        for name in (
            "agent_called",
            "agent_empty_streak",
            "agent_large_query_families",
            "agent_malformed_retries",
        ):
            self.assertIn(name, annotations)


if __name__ == "__main__":
    unittest.main()
