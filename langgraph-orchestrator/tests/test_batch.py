"""Multi-task requests: decomposition, staging, merge, planned ports, execution."""
import asyncio
import json
import unittest
from unittest import mock

from langchain_core.messages import AIMessage

from orchestrator import batch, supervisor


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    async def ainvoke(self, messages):
        return AIMessage(content=self.reply)


def decompose(reply, user_input="do things", history=None):
    with mock.patch.object(batch, "get_llm", return_value=FakeLLM(json.dumps(reply))):
        return asyncio.run(batch.decompose_request(user_input, chat_history=history))


class DecomposeTests(unittest.TestCase):
    def test_tasks_are_numbered_and_staged(self):
        out = decompose({"tasks": [
            {"id": 1, "intent": "define_port", "flow": "create", "params": {"name": "CX1"},
             "summary": "port"},
            {"id": 2, "intent": "agentic", "question": "where is CX9?", "summary": "q"},
            {"id": 3, "intent": "bind_epg", "flow": "create", "params": {}, "depends_on": [1],
             "summary": "bind"},
            {"id": 4, "intent": "remove_from_dvs", "flow": "create", "params": {},
             "depends_on": [3], "summary": "dvs"},
            {"id": 5, "intent": "bind_epg", "flow": "create", "params": {},
             "depends_on": [2, 9], "summary": "bad deps"},
        ]})
        t = {x["id"]: x for x in out["tasks"]}
        self.assertEqual(t[2]["flow"], "agentic")
        self.assertEqual(t[2]["question"], "where is CX9?")
        self.assertEqual(t[3]["stage"], 0)          # known: define_port -> bind_epg
        self.assertEqual(t[4]["stage"], 1)          # unknown dependency -> next stage
        self.assertEqual(t[5]["depends_on"], [])    # agentic / unknown ids dropped

    def test_single_intent_reply_and_parse_failure(self):
        out = decompose({"intent": "create_tenant", "flow": "create", "params": {"name": "T"}})
        self.assertEqual([t["intent"] for t in out["tasks"]], ["create_tenant"])
        with mock.patch.object(batch, "get_llm", return_value=FakeLLM("not json")):
            out = asyncio.run(batch.decompose_request("hello"))
        self.assertEqual(out["tasks"][0]["flow"], "agentic")

    def test_limit_and_bare_affirmation(self):
        many = {"tasks": [{"intent": "agentic", "question": f"q{i}"} for i in range(21)]}
        self.assertIn("limit is 20", decompose(many)["errors"][0])

        out = decompose({"tasks": [{"intent": "create_tenant", "flow": "create"}]},
                        user_input="evet yap", history=[{"role": "user", "content": "x"}])
        self.assertEqual(out["tasks"][0]["flow"], "agentic")


class MergeTests(unittest.TestCase):
    def test_merge_combines_same_tenant_and_infra(self):
        bind = {"post_path": "/api/node/mo/uni.json", "post_body": {"polUni": {"children": [
            {"fvTenant": {"attributes": {"name": "T"}, "children": [
                {"fvAp": {"attributes": {"name": "AP"}, "children": [
                    {"fvAEPg": {"attributes": {"name": "E1"}, "children": [
                        {"fvRsPathAtt": {"attributes": {"tDn": "p1"}}}]}}]}}]}}]}}}
        bind2 = json.loads(json.dumps(bind).replace("E1", "E2"))
        tenant = {"target_class": "fvTenant", "payload": {"name": "T", "descr": "new"},
                  "parent_dn": ""}
        bd = {"target_class": "fvBD", "payload": {"name": "BD1"}, "parent_dn": "uni/tn-T"}
        port1 = {"post_path": "/api/node/mo/uni/infra.json",
                 "post_body": {"infraInfra": {"attributes": {}, "children": [
                     {"infraAccPortP": {"attributes": {"name": "P1"}}}]}}}
        port2 = json.loads(json.dumps(port1).replace("P1", "P2"))

        body = batch.merge_preps([tenant, bd, bind, bind2, port1, port2])
        top = body["polUni"]["children"]
        self.assertEqual(len(top), 2)  # one fvTenant + one infraInfra
        tn = top[0]["fvTenant"]
        self.assertEqual(tn["attributes"], {"name": "T", "descr": "new"})
        kinds = sorted(list(c)[0] for c in tn["children"])
        self.assertEqual(kinds, ["fvAp", "fvBD"])
        ap = [c for c in tn["children"] if "fvAp" in c][0]["fvAp"]
        self.assertEqual(len(ap["children"]), 2)
        self.assertEqual(len(top[1]["infraInfra"]["children"]), 2)

    def test_unmergeable_path_raises(self):
        with self.assertRaises(ValueError):
            batch.merge_preps([{"post_path": "/api/node/mo/uni/fabric.json",
                                "post_body": {"fabricInst": {}}}])


def run_async(coro):
    return asyncio.run(coro)


class PlannedPortTests(unittest.TestCase):
    """define_port + bind_epg to that port in one request, prepared together."""

    def patches(self):
        async def sw_profile(nodes):
            return f"SW-{'-'.join(nodes)}", None

        async def epg_location(name):
            return {"matches": [{"tenant": "T", "ap": "AP", "epg": name}]}

        async def epg_encap(t, a, e, enc):
            return "vlan-100", "test", None

        async def no_apic(ref):
            raise AssertionError(f"APIC should not be queried for {ref}")

        return [mock.patch.object(supervisor, "resolve_switch_profile", sw_profile),
                mock.patch.object(supervisor, "resolve_epg_location", epg_location),
                mock.patch.object(supervisor, "resolve_epg_encap", epg_encap),
                mock.patch("orchestrator.nodes_port._resolve_data_port_ref", no_apic)]

    def test_bind_uses_planned_vpc_path(self):
        tasks = [
            {"id": 1, "intent": "define_port", "flow": "create", "summary": "port", "stage": 0,
             "params": {"name": "CX1", "port_type": "vpc", "nodes": ["101", "102"],
                        "ports": ["eth1/5"], "aep": "AEP"}, "depends_on": []},
            {"id": 2, "intent": "bind_epg", "flow": "create", "summary": "bind", "stage": 0,
             "params": {"epgs": ["WEB_100"], "bindings": [{"serial": "CX1"}]}, "depends_on": [1]},
        ]
        ps = self.patches()
        for p in ps:
            p.start()
        try:
            prep = run_async(batch.prepare_create_batch(tasks, "req", team="MSY"))
        finally:
            for p in ps:
                p.stop()
        self.assertFalse(prep["errors"], prep.get("errors"))
        self.assertEqual(prep["intent"], "batch")
        body = json.dumps(prep["post_body"])
        self.assertIn("topology/pod-1/protpaths-101-102/pathep-[MSY_CX1]", body)
        self.assertIn("accbundle-MSY_CX1", body)
        self.assertEqual(prep["post_path"], "/api/node/mo/uni.json")

    def test_any_preparation_error_stops_everything(self):
        tasks = [
            {"id": 1, "intent": "define_port", "flow": "create", "summary": "bad port",
             "stage": 0, "params": {"name": "CX1", "nodes": ["101"]}, "depends_on": []},
            {"id": 2, "intent": "create_tenant", "flow": "create", "summary": "t", "stage": 0,
             "params": {"name": "T"}, "depends_on": []},
        ]
        good = {"flow": "create", "dn": "uni/tn-T", "target_class": "fvTenant",
                "payload": {"name": "T"}, "errors": []}

        async def fake_prepare(task, *a, **k):
            if task["intent"] == "create_tenant":
                return dict(good)
            return await supervisor._build_port(task["params"], "req")

        with mock.patch.object(supervisor, "prepare_create_task", fake_prepare):
            prep = run_async(batch.prepare_create_batch(tasks, "req"))
        self.assertTrue(prep["errors"][0].startswith("Task 1 (bad port)"))
        self.assertNotIn("post_body", prep)


class ExecuteTests(unittest.TestCase):
    def run_exec(self, plan, post_results, prepare=None):
        posts = []

        class Post:
            async def ainvoke(self, args):
                posts.append(args)
                return post_results[len(posts) - 1]

        class Login:
            async def ainvoke(self, args):
                return {}

        tools = {"apic_post": Post(), "apic_login": Login()}
        with mock.patch.object(batch, "get_tool", side_effect=lambda n: tools[n]), \
             mock.patch.object(supervisor, "prepare_create_task", prepare or mock.AsyncMock()):
            return run_async(batch.execute_batch(plan)), posts

    def plan(self):
        return {"intent": "batch", "user_input": "req", "post_path": "/api/node/mo/uni.json",
                "post_body": {"polUni": {"children": []}}, "trail": [],
                "batch": {"team": "", "convention": None,
                          "stage0_tasks": [{"id": 1, "summary": "tenant"}],
                          "pending": [{"id": 2, "summary": "later", "intent": "bind_epg",
                                       "flow": "create", "stage": 1, "params": {}}]}}

    def test_stages_run_in_order(self):
        async def prepare(task, *a, **k):
            return {"post_path": "/api/node/mo/uni.json",
                    "post_body": {"polUni": {"children": [{"fvTenant": {"attributes": {"name": "X"}}}]}},
                    "errors": []}
        out, posts = self.run_exec(self.plan(), [{"imdata": []}, {"imdata": []}], prepare)
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(r["ok"] for r in out["batch_results"]))
        self.assertIn("Stage 2 applied", out["batch_summary"])

    def test_stage1_failure_stops(self):
        err = {"imdata": [{"error": {"attributes": {"text": "bad"}}}]}
        out, posts = self.run_exec(self.plan(), [err])
        self.assertEqual(len(posts), 1)
        self.assertIn("nothing was applied", out["batch_summary"])

    def test_later_stage_prep_failure_reports_applied_stages(self):
        async def prepare(task, *a, **k):
            return {"errors": ["EPG not found"]}
        out, posts = self.run_exec(self.plan(), [{"imdata": []}], prepare)
        self.assertEqual(len(posts), 1)
        self.assertIn("Stage 2 NOT applied", out["batch_summary"])
        self.assertIn("Applied stage(s): [1]", out["batch_summary"])
        self.assertIn("error", out["final_result"])


if __name__ == "__main__":
    unittest.main()
