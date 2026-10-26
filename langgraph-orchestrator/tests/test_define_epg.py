"""define_epg: VLAN API naming, BD/EPG template, reservation at execution."""
import asyncio
import json
import unittest
from unittest import mock

from orchestrator import batch, epg_define, vlan_api


def run(coro):
    return asyncio.run(coro)


class FakeVlanApi:
    def __init__(self, free, taken=()):
        self.free = list(free)
        self.taken = set(taken)       # reserved by someone else since the request
        self.reserved, self.released = [], []

    async def resolve_scope(self, text=None):
        self.scope_calls = getattr(self, "scope_calls", []) + [text]
        pool = vlan_api.match_pool(text, ["Legacy_Vlans", "Static_Vlans", "DIB_VLAN"])[0] if text else None
        return ["APIC_EVM_BANKA", "ESY_DCI"], pool

    async def free_vlans(self, sources, pool=None, limit=None):
        self.queries = getattr(self, "queries", []) + [(tuple(sources), pool)]
        free = [v for v in self.free if v not in self.taken and v not in self.reserved]
        return free[:limit] if limit else free

    async def reserve(self, vlan, sources, pool, note):
        if vlan in self.taken or vlan in self.reserved:
            raise vlan_api.VlanConflict("taken")
        self.reserved.append(vlan)

    async def release(self, vlan):
        self.released.append(vlan)


def patch_vlan(fake):
    return [mock.patch.object(vlan_api, n, getattr(fake, n))
            for n in ("resolve_scope", "free_vlans", "reserve", "release")]


async def no_apic(path, params=None):
    return []


class PoolTests(unittest.TestCase):
    def test_match_pool(self):
        pools = ["APIC_Vlans", "Infra_Vlans", "Legacy_Vlans", "Static_Vlans", "DIB_VLAN"]
        self.assertEqual(vlan_api.match_pool("legacy", pools)[0], "Legacy_Vlans")
        self.assertEqual(vlan_api.match_pool("DIB", pools)[0], "DIB_VLAN")
        self.assertEqual(vlan_api.match_pool("Static_Vlans", pools)[0], "Static_Vlans")
        self.assertIn("unknown", vlan_api.match_pool("gold", pools)[1])


class PrepareTests(unittest.TestCase):
    def prepare(self, params, fake):
        ps = patch_vlan(fake) + [mock.patch.object(epg_define, "_apic_get", no_apic)]
        for p in ps:
            p.start()
        try:
            return run(epg_define.prepare_define_epg(params, "req"))
        finally:
            for p in ps:
                p.stop()

    def test_names_get_vlan_suffix(self):
        prep = self.prepare({"epgs": ["MSY_APP", "MSY_DB"], "pool": "legacy"},
                            FakeVlanApi([105, 118, 130]))
        self.assertFalse(prep["errors"])
        req = prep["epg_request"]
        self.assertEqual([e["name"] for e in req["epgs"]], ["MSY_APP_105", "MSY_DB_118"])
        self.assertEqual(req["pool"], "Legacy_Vlans")
        self.assertEqual(req["sources"], ["APIC_EVM_BANKA", "ESY_DCI"])

    def test_pool_is_optional(self):
        fake = FakeVlanApi([7, 8])
        prep = self.prepare({"epgs": ["A"]}, fake)
        self.assertIsNone(prep["epg_request"]["pool"])
        self.assertEqual(fake.queries, [(("APIC_EVM_BANKA", "ESY_DCI"), None)])

    def test_not_enough_vlans(self):
        prep = self.prepare({"epgs": ["A", "B"], "pool": "legacy"}, FakeVlanApi([105]))
        self.assertIn("only 1 VLAN(s) free on every environment", prep["errors"][0])

    def test_admin_changes_pool(self):
        fake = FakeVlanApi([300, 301])
        req = {"epgs": [{"base": "A", "vlan": 105, "name": "A_105"}], "pool": "Legacy_Vlans",
               "sources": ["X"], "domains": []}
        with patch_vlan(fake)[0], patch_vlan(fake)[1]:
            run(epg_define.change_pool(req, "static"))
        self.assertEqual(req["pool"], "Static_Vlans")
        self.assertEqual(req["epgs"][0]["name"], "A_300")


ADMIN = {"tenant": "Subsidiary", "ap": "ODM_CSY_LAN", "vrf": "LAN",
         "subnets": {"APP": {"ip": "10.1.1.1/24", "scope": "private"}},
         "domains": [{"name": "Legacy_Pdom", "dn": "uni/phys-Legacy_Pdom", "kind": "phys"},
                     {"name": "Sub-CSY", "dn": "uni/vmmp-VMware/dom-Sub-CSY", "kind": "vmm"}]}


class TreeTests(unittest.TestCase):
    def test_template_and_unicast_routing(self):
        async def lag(dn):
            return dn + "/vswitchpolcont/enlacplagp-lag1"
        with mock.patch.object(epg_define, "_vmm_lag_policy", lag):
            tree = run(epg_define.build_tree(
                [{"base": "APP", "vlan": 110, "name": "APP_110"},
                 {"base": "DB", "vlan": 111, "name": "DB_111"}], ADMIN))
        tn = tree[0]["fvTenant"]
        self.assertEqual(tn["attributes"]["name"], "Subsidiary")
        bds = {c["fvBD"]["attributes"]["name"]: c["fvBD"] for c in tn["children"] if "fvBD" in c}
        app, db = bds["APP_110"], bds["DB_111"]
        self.assertEqual((app["attributes"]["unicastRoute"], db["attributes"]["unicastRoute"]),
                         ("yes", "no"))
        self.assertEqual(app["attributes"]["arpFlood"], "yes")
        self.assertIn({"fvSubnet": {"attributes": {"ip": "10.1.1.1/24", "scope": "private"}}},
                      app["children"])
        self.assertIn({"fvRsCtx": {"attributes": {"tnFvCtxName": "LAN"}}}, db["children"])
        ap = [c for c in tn["children"] if "fvAp" in c][0]["fvAp"]
        epg = ap["children"][0]["fvAEPg"]
        self.assertEqual(epg["attributes"]["name"], "APP_110")
        doms = [c["fvRsDomAtt"] for c in epg["children"] if "fvRsDomAtt" in c]
        self.assertEqual(doms[0]["attributes"]["resImedcy"], "immediate")
        self.assertEqual(doms[1]["attributes"]["resImedcy"], "pre-provision")
        self.assertIn("enlacplagp-lag1", json.dumps(doms[1]))
        self.assertNotIn("fvRsPathAtt", json.dumps(tree))

    def test_validate_subnet(self):
        self.assertEqual(epg_define.validate_subnet("10.1.1.1/24"), "10.1.1.1/24")
        for bad in ("10.1.1.1", "10.1.1.0/24", "foo"):
            with self.assertRaises(ValueError):
                epg_define.validate_subnet(bad)


class ExecuteTests(unittest.TestCase):
    def plan(self, pending=()):
        return {"intent": "batch", "user_input": "req", "post_path": "/api/node/mo/uni.json",
                "post_body": {"polUni": {"attributes": {}, "children": []}}, "trail": [],
                "approval_ref": "#7", "epg_admin": dict(ADMIN, domains=[]),
                "epg_requests": [{"epgs": [{"base": "APP", "vlan": 105, "name": "APP_105"}],
                                  "pool": "Legacy_Vlans", "sources": ["APIC_EVM_BANKA"],
                                  "domains": [], "task": {"id": 1}}],
                "batch": {"stage0_tasks": [{"id": 1, "summary": "epg"}], "pending": list(pending)}}

    def execute(self, plan, fake, post_results, prepare=None):
        posts = []

        class Post:
            async def ainvoke(self, args):
                posts.append(args)
                return post_results[len(posts) - 1]

        class Login:
            async def ainvoke(self, args):
                return {}

        tools = {"apic_post": Post(), "apic_login": Login()}
        ps = patch_vlan(fake) + [
            mock.patch.object(batch, "get_tool", side_effect=lambda n: tools[n]),
            mock.patch("orchestrator.supervisor.prepare_create_task", prepare or mock.AsyncMock())]
        for p in ps:
            p.start()
        try:
            return run(batch.execute_batch(plan)), posts
        finally:
            for p in ps:
                p.stop()

    def test_vlan_reserved_and_pushed(self):
        fake = FakeVlanApi([105, 106])
        out, posts = self.execute(self.plan(), fake, [{"imdata": []}])
        self.assertEqual(fake.reserved, [105])
        self.assertIn("APP_105", json.dumps(posts[0]["payload"]))
        self.assertIn("marked in use", out["batch_summary"])

    def test_taken_vlan_is_replaced_and_bind_follows(self):
        seen = []

        async def prepare(task, *a, **k):
            seen.append(task["params"]["epgs"])
            return {"post_path": "/api/node/mo/uni.json",
                    "post_body": {"polUni": {"children": []}}, "errors": []}
        pending = [{"id": 2, "summary": "bind", "intent": "bind_epg", "flow": "create",
                    "stage": 1, "params": {"epgs": ["APP"], "bindings": []}}]
        fake = FakeVlanApi([105, 106], taken=[105])
        out, posts = self.execute(self.plan(pending), fake, [{"imdata": []}, {"imdata": []}], prepare)
        self.assertEqual(fake.reserved, [106])
        self.assertIn("APP_106", json.dumps(posts[0]["payload"]))
        self.assertNotIn("APP_105", json.dumps(posts[0]["payload"]))
        self.assertIn("APP_105 -> APP_106", out["batch_summary"])
        self.assertEqual(seen, [["APP_106"]])

    def test_apic_failure_releases_reservation(self):
        fake = FakeVlanApi([105])
        err = {"imdata": [{"error": {"attributes": {"text": "bad vrf"}}}]}
        out, _ = self.execute(self.plan(), fake, [err])
        self.assertEqual(fake.released, [105])
        self.assertIn("released", out["batch_summary"])

    def test_missing_admin_input_stops(self):
        plan = self.plan()
        plan.pop("epg_admin")
        out, posts = self.execute(plan, FakeVlanApi([105]), [])
        self.assertEqual(posts, [])
        self.assertIn("admin", out["final_result"]["error"])


if __name__ == "__main__":
    unittest.main()


class VlanApiClientTests(unittest.TestCase):
    """The real client against a mocked VLAN Desk HTTP server."""

    def setUp(self):
        import httpx
        self.calls = []

        def handler(request):
            self.calls.append((request.method, request.url.path, request.url.params.get("pool"),
                               request.content.decode() or None))
            if request.url.path == "/api/login":
                return httpx.Response(200, json={"ok": True},
                                      headers={"set-cookie": "session=abc; Path=/"})
            if request.headers.get("cookie") != "session=abc":
                return httpx.Response(401, json={"error": "login required"})
            if request.url.path == "/api/sources":
                return httpx.Response(200, json={"sources": [{"id": "APIC_EVM_BANKA"},
                                                             {"id": "APIC_EVM_ISTIRAK"},
                                                             {"id": "ESY_DCI"}],
                                                 "pools": [{"id": "Legacy_Vlans"}, {"id": "DIB_VLAN"}]})
            if request.url.path == "/api/vlans":
                return httpx.Response(200, json={"vlans": [105, 118]})
            if request.url.path == "/api/reservations":
                vlan = json.loads(request.content)["vlan_id"]
                return httpx.Response(409 if vlan == 118 else 200,
                                      json={"error": "not free"} if vlan == 118 else {"ok": True})
            return httpx.Response(200, json={"ok": True})

        real = httpx.AsyncClient
        self.patches = [
            mock.patch.object(vlan_api.httpx, "AsyncClient",
                              lambda **kw: real(transport=httpx.MockTransport(handler), **kw)),
            mock.patch.dict("os.environ", {"VLAN_API_URL": "http://vlan", "VLAN_API_USERNAME": "u",
                                           "VLAN_API_PASSWORD": "p"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_flow(self):
        everywhere = ["APIC_EVM_BANKA", "APIC_EVM_ISTIRAK", "ESY_DCI"]
        self.assertEqual(run(vlan_api.resolve_scope("legacy")), (everywhere, "Legacy_Vlans"))
        self.assertEqual(run(vlan_api.resolve_scope(None)), (everywhere, None))
        # DIB: the Banka APICs are left out of the query
        self.assertEqual(run(vlan_api.resolve_scope("dib")),
                         (["APIC_EVM_ISTIRAK", "ESY_DCI"], "DIB_VLAN"))
        self.assertEqual(run(vlan_api.free_vlans(everywhere, None, 2)), [105, 118])
        query = [c for c in self.calls if c[1] == "/api/vlans"][0]
        self.assertIsNone(query[2])  # no pool filter sent
        run(vlan_api.reserve(105, everywhere, None, "NetworkGPT #7 APP_105"))
        with self.assertRaises(vlan_api.VlanConflict):
            run(vlan_api.reserve(118, everywhere, "Legacy_Vlans", "x"))
        bodies = [json.loads(c[3]) for c in self.calls if c[1] == "/api/reservations"]
        self.assertEqual(bodies[0]["source_ids"], everywhere)
        self.assertNotIn("pool_ids", bodies[0])
        self.assertEqual(bodies[1]["pool_ids"], ["Legacy_Vlans"])

    def test_not_configured(self):
        with mock.patch.dict("os.environ", {"VLAN_API_URL": ""}):
            with self.assertRaises(vlan_api.VlanApiError):
                run(vlan_api.free_vlans("X", "Y"))


class BatchFormTests(unittest.TestCase):
    def test_single_define_epg_still_goes_through_batch(self):
        prep = {"flow": "create", "intent": "define_epg", "target_class": "EPG + BD (1)",
                "payload": {"epgs": ["APP_105"]}, "errors": [], "trail": [],
                "epg_request": {"epgs": [{"base": "APP", "vlan": 105, "name": "APP_105"}],
                                "pool": "Legacy_Vlans", "sources": ["S"], "domains": []}}

        async def fake_prepare(task, *a, **k):
            return dict(prep)
        tasks = [{"id": 1, "intent": "define_epg", "flow": "create", "summary": "epg",
                  "stage": 0, "params": {}, "depends_on": []}]
        with mock.patch("orchestrator.supervisor.prepare_create_task", fake_prepare):
            plan = run(batch.prepare_create_batch(tasks, "req"))
        self.assertEqual(plan["intent"], "batch")
        self.assertEqual(plan["epg_requests"][0]["epgs"][0]["name"], "APP_105")
        self.assertEqual(plan["post_body"]["polUni"]["children"], [])


class WizardListTests(unittest.TestCase):
    def test_application_profiles_use_fvAp_class(self):
        seen = []

        async def fake_get(path, params=None):
            seen.append((path, params))
            cls = params["target-subtree-class"]
            return [{cls: {"attributes": {"name": "ODM_CSY_LAN"}}}]

        with mock.patch.object(epg_define, "_apic_get", fake_get):
            aps = run(epg_define.list_aps("Subsidiary"))
        self.assertEqual(aps, ["ODM_CSY_LAN"])
        self.assertEqual(seen[0], ("/api/node/mo/uni/tn-Subsidiary.json",
                                   {"query-target": "children", "target-subtree-class": "fvAp"}))
